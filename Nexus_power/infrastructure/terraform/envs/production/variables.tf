variable "argocd_role_arn" {
  description = <<EOT
ARN of the IAM role Argo CD's argocd-server pods assume to talk to this
cluster. Set via tfvars or a remote-state reference to the hub-cluster
Terraform.
EOT
  type = string
}
