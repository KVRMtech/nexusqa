output "manifest_path" {
  description = "Local path to the rendered Argo CD cluster Secret YAML."
  value       = local_file.cluster_secret.filename
}

output "manifest_content" {
  description = "The cluster Secret YAML as a string (sensitive — contains the cluster CA but no static token)."
  value       = local_file.cluster_secret.content
  sensitive   = true
}
