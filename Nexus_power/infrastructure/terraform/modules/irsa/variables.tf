variable "env" {
  type = string
}

variable "oidc_issuer_url" {
  description = "Cluster OIDC issuer URL (output of the eks module)."
  type        = string
}

variable "oidc_provider_arn" {
  description = "Cluster OIDC provider ARN (output of the eks module)."
  type        = string
}

variable "kms_key_arn" {
  description = "Platform CMK ARN (output of kms-and-secrets)."
  type        = string
}

variable "secrets_arn_prefix" {
  description = "Wildcard ARN scoping ESO's secretsmanager:GetSecretValue (output of kms-and-secrets)."
  type        = string
}

variable "artifacts_bucket_arn" {
  description = "S3 bucket ARN (output of artifacts)."
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
