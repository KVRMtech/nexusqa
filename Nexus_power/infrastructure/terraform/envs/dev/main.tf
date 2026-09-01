locals {
  env          = "dev"
  cluster_name = "nexus-dev"
  region       = "us-east-1"

  common_tags = {
    "nexus-platform/env"     = local.env
    "nexus-platform/owner"   = "platform-eng"
    "nexus-platform/managed" = "terraform"
  }
}

provider "aws" {
  region = local.region
  default_tags { tags = local.common_tags }
}

module "network" {
  source = "../../modules/network"

  env                = local.env
  cluster_name       = local.cluster_name
  cidr_block         = "10.20.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b"]
  nat_gateway_per_az = false
  tags               = local.common_tags
}

module "secrets" {
  source = "../../modules/kms-and-secrets"
  env    = local.env
  tags   = local.common_tags
}

module "eks" {
  source = "../../modules/eks"

  env                  = local.env
  cluster_name         = local.cluster_name
  kubernetes_version   = "1.30"
  subnet_ids           = module.network.private_subnet_ids
  secrets_kms_key_arn  = module.secrets.kms_key_arn
  # Dev: corporate VPN egress allowlist; default deny.
  public_access_cidrs  = var.public_access_cidrs

  general_instance_types = ["m6i.large"]
  general_desired_size   = 2
  general_min_size       = 2
  general_max_size       = 4

  # No GPU by default in dev — flip via tfvars for vision-engine work.
  gpu_enabled         = false

  tags = local.common_tags
}

module "artifacts" {
  source = "../../modules/artifacts"

  env         = local.env
  bucket_name = "nexus-platform-${local.env}"
  kms_key_arn = module.secrets.kms_key_arn
  tags        = local.common_tags

  lifecycle_rules = [
    {
      id     = "dev-purge"
      prefix = ""
      transitions             = []
      expiration_days         = 7
      noncurrent_expiration_days = 3
      abort_multipart_days    = 1
    },
  ]
}

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

module "argocd_cluster" {
  source = "../../modules/argocd-cluster"

  env                = local.env
  region             = local.region
  cluster_name       = module.eks.cluster_name
  cluster_short_name = "dev-us-east"
  cluster_endpoint   = module.eks.cluster_endpoint
  cluster_ca_data    = module.eks.cluster_certificate_authority
  argocd_role_arn    = var.argocd_role_arn
}
