locals {
  env          = "staging"
  cluster_name = "nexus-staging"
  region       = "us-east-1"

  common_tags = {
    "nexus-platform/env"      = local.env
    "nexus-platform/owner"    = "platform-eng"
    "nexus-platform/managed"  = "terraform"
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
  cidr_block         = "10.30.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b"]
  nat_gateway_per_az = false   # single NAT — cost-optimised
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
  public_access_cidrs  = var.public_access_cidrs

  general_instance_types = ["m6i.large", "m6i.xlarge"]
  general_desired_size   = 3
  general_max_size       = 8

  gpu_enabled         = true
  gpu_instance_types  = ["g5.xlarge"]
  gpu_desired_size    = 1
  gpu_max_size        = 2

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
      id     = "shortlived-staging"
      prefix = ""
      transitions             = []
      expiration_days         = 30      # nuke after 30 days in staging
      noncurrent_expiration_days = 7
      abort_multipart_days    = 3
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
  cluster_short_name = "staging-us-east"
  cluster_endpoint   = module.eks.cluster_endpoint
  cluster_ca_data    = module.eks.cluster_certificate_authority
  argocd_role_arn    = var.argocd_role_arn
}
