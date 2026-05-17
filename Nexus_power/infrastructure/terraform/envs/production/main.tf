# Production env composition — wires every module with production
# defaults: multi-AZ NAT, larger node groups, public API endpoint
# allowlisted to corporate VPN egress, 90-day artifact retention.
#
# Apply order:
#   1. terraform init           (with the backend config below)
#   2. terraform plan -out=plan
#   3. terraform apply plan
#   4. kubectl apply -f $(terraform output -raw argocd_cluster_manifest)
#      against the Argo CD hub cluster to register this cluster.

locals {
  env          = "production"
  cluster_name = "nexus-production"
  region       = "us-east-1"

  common_tags = {
    "nexus-platform/env"      = local.env
    "nexus-platform/owner"    = "platform-eng"
    "nexus-platform/managed"  = "terraform"
    "nexus-platform/version"  = "M0.7"
  }
}

provider "aws" {
  region = local.region

  default_tags {
    tags = local.common_tags
  }
}

# ── Network ──────────────────────────────────────────────────────

module "network" {
  source = "../../modules/network"

  env                = local.env
  cluster_name       = local.cluster_name
  cidr_block         = "10.40.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b", "us-east-1c"]
  nat_gateway_per_az = true     # HA: one NAT per AZ
  tags               = local.common_tags
}

# ── KMS + Secrets Manager ────────────────────────────────────────

module "secrets" {
  source = "../../modules/kms-and-secrets"

  env  = local.env
  tags = local.common_tags
  # secret_keys uses the module default — keep that as the canonical
  # list. Add new keys via PR to the module's variables.tf default.
}

# ── EKS ──────────────────────────────────────────────────────────

module "eks" {
  source = "../../modules/eks"

  env                  = local.env
  cluster_name         = local.cluster_name
  kubernetes_version   = "1.30"
  subnet_ids           = module.network.private_subnet_ids
  secrets_kms_key_arn  = module.secrets.kms_key_arn

  # Public endpoint with a tight allowlist. Empty list = fully private;
  # in production we keep it private + require VPN for kubectl access.
  public_access_cidrs  = []

  general_instance_types = ["m6i.xlarge", "m6i.2xlarge"]
  general_desired_size   = 6
  general_min_size       = 6
  general_max_size       = 30

  gpu_enabled         = true
  gpu_instance_types  = ["g5.2xlarge"]
  gpu_desired_size    = 2
  gpu_min_size        = 2
  gpu_max_size        = 8

  tags = local.common_tags
}

# ── Artifacts S3 ─────────────────────────────────────────────────

module "artifacts" {
  source = "../../modules/artifacts"

  env         = local.env
  bucket_name = "nexus-platform-${local.env}"
  kms_key_arn = module.secrets.kms_key_arn
  tags        = local.common_tags

  lifecycle_rules = [
    {
      id     = "intermediate-frames"
      prefix = ""
      # Frames carry the namespace "eyes" — these are reproducible from
      # the source video, so we expire them aggressively.
      transitions = [
        { days = 30, storage_class = "STANDARD_IA" },
        { days = 90, storage_class = "GLACIER_IR" },
      ]
      expiration_days            = 365
      noncurrent_expiration_days = 30
    },
    {
      id     = "abort-incomplete-multipart"
      prefix = ""
      transitions             = []
      expiration_days         = null
      noncurrent_expiration_days = 30
      abort_multipart_days    = 7
    },
  ]
}

# ── IRSA roles ───────────────────────────────────────────────────

module "irsa" {
  source = "../../modules/irsa"

  env                  = local.env
  oidc_issuer_url      = module.eks.oidc_issuer_url
  oidc_provider_arn    = module.eks.oidc_provider_arn
  kms_key_arn          = module.secrets.kms_key_arn
  secrets_arn_prefix   = module.secrets.secret_prefix
  artifacts_bucket_arn = module.artifacts.bucket_arn
  tags                 = local.common_tags
}

# ── Argo CD cluster Secret (output only) ─────────────────────────

module "argocd_cluster" {
  source = "../../modules/argocd-cluster"

  env                = local.env
  region             = local.region
  cluster_name       = module.eks.cluster_name
  cluster_short_name = "prod-us-east"
  cluster_endpoint   = module.eks.cluster_endpoint
  cluster_ca_data    = module.eks.cluster_certificate_authority
  # Argo CD uses its own IRSA role (lives in the hub-cluster's
  # Terraform, not this one). Pass it in via tfvars or a remote state
  # reference.
  argocd_role_arn    = var.argocd_role_arn
}
