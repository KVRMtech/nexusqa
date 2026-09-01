# KMS CMK + Secrets Manager placeholder secrets.
#
# The CMK is used to envelope-encrypt:
#   - EKS Kubernetes Secrets (consumed by the eks module)
#   - Secrets Manager payloads (consumed by ESO via the AWS provider)
#   - S3 artifacts (consumed by the artifacts module)
#
# Secrets Manager records are created EMPTY here so that ESO finds them
# and the per-key bootstrap (M0.5) or out-of-band update can put values
# without an "ARN not found" race. Empty secrets are not a security risk
# — the chart's ExternalSecret would surface "missing value" errors.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  partition   = data.aws_partition.current.partition
  account_id  = data.aws_caller_identity.current.account_id
  common_tags = merge(var.tags, {
    "nexus-platform/env" = var.env
  })
}

# ── CMK ──────────────────────────────────────────────────────────

resource "aws_kms_key" "this" {
  description             = "nexus-platform-${var.env} envelope key (EKS secrets, Secrets Manager, S3)"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  multi_region            = false

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Root account always retains administrative access — without this
      # the key becomes unmanageable on accidental policy mistake.
      {
        Sid       = "RootAccountFullControl"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      # AWS services that use the key on the platform's behalf.
      {
        Sid       = "AllowEKSServiceUse"
        Effect    = "Allow"
        Principal = { Service = "eks.amazonaws.com" }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = "*"
      },
      {
        Sid       = "AllowSecretsManagerUse"
        Effect    = "Allow"
        Principal = { Service = "secretsmanager.amazonaws.com" }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = "*"
      },
      {
        Sid       = "AllowS3ServiceUse"
        Effect    = "Allow"
        Principal = { Service = "s3.amazonaws.com" }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
        ]
        Resource = "*"
      },
    ]
  })

  tags = local.common_tags
}

resource "aws_kms_alias" "this" {
  name          = "alias/nexus-platform-${var.env}-secrets"
  target_key_id = aws_kms_key.this.id
}

# ── Secrets Manager placeholders ─────────────────────────────────
#
# `var.secret_keys` is the canonical list of secrets the chart's
# ExternalSecret references. Every name MUST resolve at sync time, so we
# pre-create them empty here. Operators populate the value via the
# bootstrap PushSecret (dev/staging) or out-of-band (production).

resource "aws_secretsmanager_secret" "platform" {
  for_each = toset(var.secret_keys)

  name        = "nexus-platform/${var.env}/${each.key}"
  description = "Platform secret '${each.key}' for the ${var.env} environment."
  kms_key_id  = aws_kms_key.this.id

  recovery_window_in_days = 30

  tags = merge(local.common_tags, {
    "nexus:env" = var.env
    "nexus:key" = each.key
  })
}

# Per-secret rotation lambdas are out of scope here — the M0.5 runbook
# describes the manual rotation flow for each secret class. Adding
# rotation lambdas later only requires aws_secretsmanager_secret_rotation
# blocks referencing the placeholders above.
