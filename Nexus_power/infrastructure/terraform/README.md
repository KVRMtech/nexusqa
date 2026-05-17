# Terraform — Cluster IaC (Reference Cloud: AWS)

This tree provisions every piece of cloud-side infrastructure the
platform expects: VPC, EKS, KMS, Secrets Manager placeholders, S3
artifact bucket, IRSA roles, and the Argo CD cluster registration
Secret. After `terraform apply`, the GitOps tree in
[`../gitops/`](../gitops/) takes over via Argo CD.

## Tree layout

```
infrastructure/terraform/
├── README.md
├── modules/
│   ├── network/          # VPC, subnets, NAT, route tables, flow logs
│   ├── eks/              # EKS cluster + general + GPU node groups
│   ├── kms-and-secrets/  # Platform CMK + Secrets Manager placeholders
│   ├── artifacts/        # S3 bucket (M0.4 stateless-engine target)
│   ├── irsa/             # IAM roles for ESO / engines / argo-rollouts
│   └── argocd-cluster/   # Renders Argo CD cluster-registration Secret
└── envs/
    ├── dev/              # Single-NAT, no GPU by default
    ├── staging/          # Single-NAT, 1 GPU node
    └── production/       # Multi-AZ NAT, 6+ general nodes, 2+ GPU
```

The modules are vendor-neutral in spirit but AWS-specific in
implementation. Each env overlay composes the same module set with
env-tuned variables — there is no env-specific module.

## Bootstrapping order (greenfield)

This sequence assumes:
- A separate, **out-of-tree** bootstrap stack provisioned the Terraform
  state S3 bucket and DynamoDB lock table. That stack is one-time and
  per AWS account; it intentionally lives outside the platform tree so
  the platform never tries to manage its own state backend.
- Argo CD is running in a hub cluster (which itself was provisioned via
  a similar process). The hub cluster's argocd-server pod has an IRSA
  role authorised to assume `var.argocd_role_arn` in this account.

### 1. Initialise state backend

```
cd envs/production
terraform init \
  -backend-config="bucket=tfstate-${ACCOUNT_ID}" \
  -backend-config="key=nexus-platform/production/terraform.tfstate" \
  -backend-config="region=us-east-1" \
  -backend-config="dynamodb_table=tfstate-lock-${ACCOUNT_ID}" \
  -backend-config="encrypt=true"
```

### 2. Plan + apply

```
terraform plan \
  -var="argocd_role_arn=arn:aws:iam::${ACCOUNT_ID}:role/argocd-cluster-mgmt" \
  -out=plan
terraform apply plan
```

First-apply takes ~25 minutes — EKS cluster creation dominates.

### 3. Register the cluster with Argo CD

```
# From a machine with kubeconfig pointing at the Argo CD hub cluster:
kubectl apply -f $(terraform output -raw argocd_cluster_manifest)
```

Argo CD picks up the new cluster Secret within ~10 s and starts
generating Applications from the ApplicationSets at
[`../gitops/applicationsets/`](../gitops/applicationsets/).

### 4. Annotate the ESO ServiceAccount with the IRSA role

ESO is installed by `infra-controllers` ApplicationSet but its SA needs
the role ARN. Patch the env overlay at
[`../gitops/envs/production/values/external-secrets.yaml`](../gitops/envs/production/values/external-secrets.yaml)
with the Terraform output:

```
ESO_ROLE_ARN=$(terraform output -raw eso_role_arn)
yq -i '.serviceAccount.annotations."eks.amazonaws.com/role-arn" = strenv(ESO_ROLE_ARN)' \
  ../../gitops/envs/production/values/external-secrets.yaml
git commit -am "production: wire ESO IRSA role"
```

Argo CD picks the commit up and re-syncs ESO with the new annotation.

### 5. Populate the Secrets Manager placeholders

The `kms-and-secrets` module created every secret name listed in
[`modules/kms-and-secrets/variables.tf`](modules/kms-and-secrets/variables.tf)
but with empty values. Operators populate them via:

- **dev/staging** — flip `externalSecrets.bootstrap.enabled: true` in
  the chart's env values, push the initial values via Helm, then flip
  it back to false. See [`../gitops/SECRETS_ROTATION.md`](../gitops/SECRETS_ROTATION.md).
- **production** — out-of-band only. Use the AWS console or a vetted
  one-shot CI job; never commit values to git.

### 6. Verify

```
kubectl --context=prod-us-east -n external-secrets \
  get clustersecretstore nexus-platform-production
kubectl --context=prod-us-east -n nexus-qa \
  get externalsecret nexus-qa-secrets
```

Both should report `Ready=True`.

## What this tree does NOT do

- Provision the Terraform state backend itself — see
  [Bootstrapping order](#bootstrapping-order-greenfield) note above.
- Provision Argo CD — that's a one-time hub-cluster install with its
  own root-of-trust.
- Set Secrets Manager values — only names. Values are operator-side.
- Set up cross-account / cross-region replication for the artifacts
  bucket. Add when multi-region failover is on the roadmap.
- Manage DNS / external load balancers — the ingress is created by the
  cluster's nginx-ingress controller (installed via Argo CD).
- Pin EKS add-on versions — relies on the EKS-managed update behaviour.
  Pin via per-env tfvars if a specific version is required.

## Per-env defaults summary

| Variable                   | dev          | staging        | production     |
|----------------------------|--------------|----------------|----------------|
| VPC CIDR                   | 10.20.0.0/16 | 10.30.0.0/16   | 10.40.0.0/16   |
| AZ count                   | 2            | 2              | 3              |
| NAT GW per AZ              | no           | no             | yes            |
| EKS public endpoint        | private      | VPN allowlist  | private        |
| General node desired/max   | 2/4          | 3/8            | 6/30           |
| GPU node group             | disabled     | 1× g5.xlarge   | 2-8× g5.2xlarge|
| Artifact retention         | 7 days       | 30 days        | 1 year         |

## Adapting to GCP / Azure

The module shape (network → cluster → kms-and-secrets → artifacts →
irsa → argocd-cluster) carries over verbatim. Each provider needs its
own module implementations (GKE / AKS, GCS / Blob, Secret Manager /
Key Vault, Workload Identity / Workload Identity Federation). The env
overlays remain a thin composition layer over identical module names —
the gitops tree at [`../gitops/resources/external-secrets/<cloud>/`](../gitops/resources/external-secrets/)
is already cloud-aware (M0.5).
