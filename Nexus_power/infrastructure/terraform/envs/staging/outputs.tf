output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value     = module.eks.cluster_endpoint
  sensitive = true
}

output "oidc_provider_arn" {
  value = module.eks.oidc_provider_arn
}

output "artifacts_bucket" {
  value = module.artifacts.bucket_name
}

output "kms_key_arn" {
  value = module.secrets.kms_key_arn
}

output "eso_role_arn" {
  value = module.irsa.eso_role_arn
}

output "engine_role_arn" {
  value = module.irsa.engine_role_arn
}

output "argocd_cluster_manifest" {
  value = module.argocd_cluster.manifest_path
}
