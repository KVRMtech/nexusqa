variable "argocd_role_arn" {
  type = string
}

variable "public_access_cidrs" {
  description = "Allowlist for the EKS public API endpoint. Empty list = fully private."
  type        = list(string)
  default     = []
}
