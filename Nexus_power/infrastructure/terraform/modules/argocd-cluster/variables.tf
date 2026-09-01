variable "env" {
  type = string
}

variable "region" {
  type = string
}

variable "cluster_name" {
  description = "EKS cluster name (used in awsAuthConfig)."
  type        = string
}

variable "cluster_short_name" {
  description = "Short display name in Argo CD (e.g. prod-us-east)."
  type        = string
}

variable "cluster_endpoint" {
  description = "EKS cluster API endpoint."
  type        = string
}

variable "cluster_ca_data" {
  description = "Base64-encoded cluster CA certificate (output of eks module: cluster_certificate_authority)."
  type        = string
}

variable "argocd_role_arn" {
  description = <<EOT
IAM role Argo CD assumes to talk to this cluster. The role's trust
policy must allow assumption from the IRSA role of the argocd-server
ServiceAccount in the hub cluster.
EOT
  type = string
}
