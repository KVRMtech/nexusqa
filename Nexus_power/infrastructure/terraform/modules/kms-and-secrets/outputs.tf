output "kms_key_id" {
  value = aws_kms_key.this.id
}

output "kms_key_arn" {
  value = aws_kms_key.this.arn
}

output "kms_alias" {
  value = aws_kms_alias.this.name
}

output "secret_arns" {
  description = "Map of secret key → Secrets Manager ARN. Used by IRSA policies to scope read access."
  value       = { for k, s in aws_secretsmanager_secret.platform : k => s.arn }
}

output "secret_prefix" {
  description = "ARN prefix that covers every platform secret in this env."
  value       = "arn:aws:secretsmanager:*:*:secret:nexus-platform/${var.env}/*"
}
