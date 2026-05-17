# First Production Deploy Runbook

This is the step-by-step procedure for the **first** real deployment
of the Nexus canonical processing platform to a cloud account. Every
step is reversible until step 8; nothing in steps 1–7 affects paying
customers.

The runbook is written for AWS (the reference cloud); the GCP and
Azure flows reuse the same shape with module-specific commands.

## Prerequisites

- AWS CLI configured with credentials for the target account
- Terraform ≥ 1.6.0
- kubectl ≥ 1.30
- helm ≥ 3.14
- argocd CLI ≥ 2.10 (only for step 7)
- An empty S3 bucket + DynamoDB table for Terraform state (created
  once per account by a separate bootstrap stack; see appendix)
- AWS Secrets Manager populated with the platform secret keys (the
  Terraform `kms-and-secrets` module creates the empty placeholders;
  you populate them after step 3)

## Static gates — run BEFORE touching the cloud

Both gates are zero-network, zero-cost, and CI-enforced:

```bash
python infrastructure/helm/nexus-qa/scripts/chart_lint.py --env production
python infrastructure/terraform/scripts/tf_lint.py --env production
```

Both must report `0 errors`. If either fails, the deploy stops here.

## Step 1 — Validate Terraform plan against staging

```bash
cd infrastructure/terraform/envs/staging
terraform init \
  -backend-config="bucket=tfstate-${ACCOUNT_ID}" \
  -backend-config="key=nexus-platform/staging/terraform.tfstate" \
  -backend-config="region=us-east-1" \
  -backend-config="dynamodb_table=tfstate-lock-${ACCOUNT_ID}" \
  -backend-config="encrypt=true"

terraform validate
terraform plan \
  -var="argocd_role_arn=arn:aws:iam::${ACCOUNT_ID}:role/argocd-cluster-mgmt" \
  -out=staging.plan
```

Inspect the plan. Expected counts (approximate): ~110 resources to
create on a greenfield apply. Net-new IAM policies, VPC subnets,
node groups, KMS keys, S3 bucket, Secrets Manager placeholders.

**Rollback at this point**: delete `staging.plan`. Nothing applied.

## Step 2 — Apply Terraform to staging

```bash
terraform apply staging.plan
```

First apply takes ~25 minutes. EKS cluster creation dominates.

Capture outputs you'll need downstream:

```bash
terraform output -raw cluster_name           > /tmp/cluster_name
terraform output -raw cluster_endpoint       > /tmp/cluster_endpoint
terraform output -raw artifacts_bucket       > /tmp/bucket
terraform output -raw kms_key_arn            > /tmp/kms_key_arn
terraform output -raw eso_role_arn           > /tmp/eso_role_arn
terraform output -raw engine_role_arn        > /tmp/engine_role_arn
terraform output -raw argocd_cluster_manifest > /tmp/argocd_cluster_manifest_path
```

**Rollback**: `terraform destroy` (only safe before any client data
has been uploaded — i.e. before step 8). After step 8, destroy
becomes a customer-impact event.

## Step 3 — Populate Secrets Manager

The Terraform `kms-and-secrets` module created empty placeholders for
every key the chart's ExternalSecret references. Populate them
out-of-band:

```bash
# Generate a strong JWT key and stash it.
NEW_JWT=$(openssl rand -hex 64)
aws secretsmanager put-secret-value \
  --secret-id nexus-platform/staging/jwt-secret \
  --secret-string "$NEW_JWT"

# Repeat for every secret the platform needs:
#   postgres-password, redis-password, neo4j-password,
#   minio-access-key, minio-secret-key, grafana-password,
#   shield-encryption-key, ears-hf-token (optional),
#   heart-tier1-api-key, heart-tier2-api-key (if LLM tiering enabled),
#   brain-tier1-api-key, brain-tier2-api-key (if LLM tiering enabled),
#   s3-access-key, s3-secret-key,
#   argo-rollouts-slack-token, argo-rollouts-pagerduty-token
```

The full list lives in
[`infrastructure/terraform/modules/kms-and-secrets/variables.tf`](../infrastructure/terraform/modules/kms-and-secrets/variables.tf)
under `var.secret_keys.default`.

## Step 4 — Connect kubectl to the new cluster

```bash
aws eks update-kubeconfig \
  --name $(cat /tmp/cluster_name) \
  --region us-east-1

kubectl get nodes      # should show 3 general + 1 GPU node ready
kubectl get pods -A    # only kube-system, aws-node, coredns, etc.
```

## Step 5 — Apply GitOps bootstrap

```bash
kubectl apply -f infrastructure/gitops/bootstrap/00-namespaces.yaml
kubectl apply -f infrastructure/gitops/bootstrap/10-argocd.yaml

# Wait for Argo CD to come up.
kubectl -n argocd wait --for=condition=Available \
  deploy/argocd-server --timeout=10m
```

Then apply the AppProjects + root app:

```bash
kubectl apply -f infrastructure/gitops/bootstrap/20-argocd-projects.yaml
kubectl apply -f infrastructure/gitops/bootstrap/30-root-app.yaml
```

Watch Argo CD reconcile every infra controller:

```bash
argocd app list --grpc-web
# expected: cert-manager, external-secrets, linkerd-crds,
#           linkerd-control-plane, argo-rollouts, kyverno, keda
```

Each app should reach `Synced + Healthy` within ~5–10 minutes. If
not, check controller logs:

```bash
kubectl -n external-secrets logs deploy/external-secrets --tail=200
kubectl -n keda logs deploy/keda-operator --tail=200
```

## Step 6 — Register the cluster with Argo CD

Argo CD must be told about this new cluster (a Secret in the argocd
namespace pointing at the EKS API). The Terraform module rendered
the manifest:

```bash
kubectl --context argocd-hub-cluster apply \
  -f "$(cat /tmp/argocd_cluster_manifest_path)"
```

Argo CD picks it up within ~10s and starts generating Applications
from the ApplicationSets.

## Step 7 — Annotate ESO ServiceAccount with the IRSA role

```bash
ESO_ROLE_ARN=$(cat /tmp/eso_role_arn)

# Edit the env overlay:
yq -i ".serviceAccount.annotations.\"eks.amazonaws.com/role-arn\" = \"$ESO_ROLE_ARN\"" \
  infrastructure/gitops/envs/staging/values/external-secrets.yaml

git add infrastructure/gitops/envs/staging/values/external-secrets.yaml
git commit -m "staging: wire ESO IRSA role"
git push origin develop
```

Argo CD reconciles the change within ~1 min. Verify ESO can now
read Secrets Manager:

```bash
kubectl --context staging-us-east -n external-secrets \
  describe clustersecretstore nexus-platform-staging
# Look for: Status: Ready=True
```

## Step 8 — First canonical workflow

The platform is now live. Push one upload through to validate the
end-to-end chain:

```bash
# Get a JWT via the platform's auth-service.
TOKEN=$(curl -X POST https://staging.platform.example.com/api/v1/auth/login \
  -d '{"email":"smoketest@nexus.io","password":"$SMOKE_PW"}' \
  -H "Content-Type: application/json" | jq -r .access_token)

# Create a session.
SESSION=$(curl -X POST https://staging.platform.example.com/api/v1/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"smoke-test","session_type":"video"}' | jq -r .session_id)

# Upload a sample video. (1-2 minute Zoom recording, < 50MB.)
curl -X POST "https://staging.platform.example.com/api/v1/orchestrator/process" \
  -H "Authorization: Bearer $TOKEN" \
  -F "session_id=$SESSION" \
  -F "video=@./fixtures/smoke-test.mp4" \
  -F "processing_profile=fast"

# Poll workflow status — expect terminal within ~5 minutes.
curl -H "Authorization: Bearer $TOKEN" \
  "https://staging.platform.example.com/api/v1/canonical-workflows/$WORKFLOW_ID" \
  | jq .status
```

If status reaches `completed` and the client UI's
`CanonicalResultPage` renders a quality score, the deploy is real.

## Step 9 — Promote to production

Identical sequence with `envs/production` instead of `envs/staging`.
Differences:

- `terraform apply` plans larger node groups (6+ general, 2+ GPU)
- Argo CD production AppProject requires manual sync approval on
  every change (see `envs/production/config.yaml: syncPolicy`)
- ESO must be wired to the production ClusterSecretStore (different
  AWS account or at least different KMS key)
- KEDA + Argo Rollouts both ENABLED in `envs/production/values/platform.yaml`
  by default (vs. disabled in staging)

The static gates from the preamble run identically against the
production overlay — both must report `0 errors` before step 1.

## What to do if a step fails

| Failure | Recovery |
|---------|----------|
| `terraform plan` errors | Re-run `python infrastructure/terraform/scripts/tf_lint.py --env staging` — surfaces module-output mismatches before terraform sees them |
| `terraform apply` errors | Read the error; common: IAM quota exceeded → file a support ticket and wait. EKS subnet conflicts → adjust `var.cidr_block` |
| Argo CD app stuck `Progressing` | `argocd app sync <name> --force --grpc-web`. If still stuck, `kubectl describe app <name> -n argocd` |
| ESO `ClusterSecretStore` not Ready | IRSA role annotation missing or wrong. Re-check step 7 output |
| First upload's workflow stuck | `kubectl logs deploy/nexus-staging-orchestrator -n nexus-qa`, look for `workflow_sweeper.deadline` events. DLQ admin endpoint: `POST /api/v1/canonical-admin/dlq/workflows/{id}/replay` |
| Quality gate fails | Quality score < threshold. Inspect the workflow's `canonical_kt_session_node_id` knowledge node in Neo4j; spine's `_persist_artifact_to_db` will mark `quality_outcome=needs_review` |

## Appendix — bootstrap stack (one-time per account)

The Terraform state backend (S3 bucket + DynamoDB table) is created
**outside** this repo by a separate stack. That stack is one-time per
AWS account; once it exists, every env's `terraform init` references
it via `-backend-config`. The minimal Terraform for that stack is in
[infrastructure/terraform/bootstrap/](../infrastructure/terraform/bootstrap/)
(create-if-missing; reach out to platform-eng for the actual code).

## Appendix — Static gates in CI

Both gates run on every PR via
[`.github/workflows/deploy-validation.yml`](../.github/workflows/deploy-validation.yml).
A PR cannot merge if either gate fails. The same workflow also runs
`helm template` (when helm is available in the runner) and
`terraform validate` (when terraform is available), so the real tools
catch anything the static gates miss.
