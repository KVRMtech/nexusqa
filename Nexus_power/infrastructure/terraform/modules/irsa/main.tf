# IRSA — IAM Roles for Service Accounts.
#
# Each Kubernetes ServiceAccount that needs AWS access has a dedicated
# IAM role here, trust-policied to the cluster's OIDC provider with a
# `sub` condition pinning it to one (namespace, SA) pair. There is no
# wildcard trust — a compromised pod in one namespace cannot assume any
# other role.
#
# Roles minted:
#   1. external-secrets/external-secrets — Secrets Manager + KMS Decrypt
#      scoped to the platform's secret prefix.
#   2. nexus-qa/nexus-qa            — engine SA, S3 R/W on the artifacts
#      bucket + KMS Encrypt/Decrypt for SSE-KMS.
#   3. argo-rollouts/argo-rollouts  — no cloud perms today; trust-policy
#      anchor for future cross-account or sigstore work.

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

locals {
  partition   = data.aws_partition.current.partition
  account_id  = data.aws_caller_identity.current.account_id
  oidc_host   = replace(var.oidc_issuer_url, "https://", "")
  common_tags = merge(var.tags, {
    "nexus-platform/env" = var.env
  })
}

# ── ESO role ─────────────────────────────────────────────────────

data "aws_iam_policy_document" "eso_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["system:serviceaccount:external-secrets:external-secrets"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "eso_policy" {
  # Read every platform secret in this env (scoped via the env-prefixed
  # ARN passed in). The store layer's tag scoping enforces the same
  # boundary at fetch time.
  statement {
    sid    = "ReadPlatformSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
      "secretsmanager:ListSecretVersionIds",
    ]
    resources = [var.secrets_arn_prefix]
  }
  statement {
    sid    = "DescribeAllSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:ListSecrets",
    ]
    resources = ["*"]
  }
  # Decrypt with the platform CMK.
  statement {
    sid       = "UseCMK"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role" "eso" {
  name               = "nexus-${var.env}-external-secrets"
  assume_role_policy = data.aws_iam_policy_document.eso_trust.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy" "eso" {
  role   = aws_iam_role.eso.id
  name   = "eso-secrets-and-kms"
  policy = data.aws_iam_policy_document.eso_policy.json
}

# ── Engine role (nexus-qa/nexus-qa SA) ───────────────────────────

data "aws_iam_policy_document" "engine_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      # Single SA used by every nexus-qa pod (chart's serviceAccount.name
      # default). If finer-grained roles are ever needed, this module
      # supports adding (name, sa) pairs in a future iteration.
      values   = ["system:serviceaccount:nexus-qa:nexus-qa"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "engine_policy" {
  # S3 R/W on the artifacts bucket. Engines write originals + extracted
  # frames; the platform serve endpoint presigns reads.
  statement {
    sid    = "ArtifactsBucketAccess"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = [
      var.artifacts_bucket_arn,
      "${var.artifacts_bucket_arn}/*",
    ]
  }
  # Presigned-URL signing.
  statement {
    sid    = "PresignSupport"
    effect = "Allow"
    actions = [
      "s3:GetObject",
    ]
    resources = ["${var.artifacts_bucket_arn}/*"]
  }
  # Use the CMK behind SSE-KMS.
  statement {
    sid    = "UseCMKForBucket"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role" "engine" {
  name               = "nexus-${var.env}-engines"
  assume_role_policy = data.aws_iam_policy_document.engine_trust.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy" "engine" {
  role   = aws_iam_role.engine.id
  name   = "engine-s3-and-kms"
  policy = data.aws_iam_policy_document.engine_policy.json
}

# ── argo-rollouts role (anchor only, no perms yet) ──────────────

data "aws_iam_policy_document" "rollouts_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["system:serviceaccount:argo-rollouts:argo-rollouts"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rollouts" {
  name               = "nexus-${var.env}-argo-rollouts"
  assume_role_policy = data.aws_iam_policy_document.rollouts_trust.json
  tags               = local.common_tags
}
