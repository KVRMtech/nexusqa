variable "argocd_role_arn" {
  type = string
}

variable "public_access_cidrs" {
  description = "Allowlist for the EKS public API endpoint. Staging defaults to corporate VPN egress; tighten via tfvars."
  type        = list(string)
  default     = []
}
