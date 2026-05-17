variable "env" {
  type = string
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name. Conventionally nexus-platform-<env>-<aws-account-suffix>."
  type        = string
}

variable "kms_key_arn" {
  description = "ARN of the CMK used for SSE-KMS on this bucket."
  type        = string
}

variable "lifecycle_rules" {
  description = <<EOT
List of lifecycle-rule objects. Each rule:
  - id (string)
  - prefix (string)
  - enabled (bool, default true)
  - transitions: list({days, storage_class})
  - expiration_days (number, null disables)
  - noncurrent_expiration_days (number, default 30)
  - abort_multipart_days (number, default 7)
EOT
  type    = any
  default = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
