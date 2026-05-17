# S3 bucket holding the platform's durable artifacts (videos, audio,
# frames, evidence, reports, documents). Aligns with the M0.4
# stateless-engine model: this bucket is the canonical store, engines
# write here via the StorageBackend abstraction.
#
# Security baseline:
#   - SSE-KMS using the platform CMK passed in.
#   - Public access fully blocked at the account-level and bucket-level.
#   - Versioning on (recovery from accidental delete).
#   - Object Ownership: BucketOwnerEnforced (no ACLs).
#   - TLS-only access enforced via bucket policy.
#
# Lifecycle:
#   - var.lifecycle_rules drives retention. The default 90-day TTL on
#     intermediate artifacts (frames, evidence dirs) limits storage cost;
#     the canonical source-of-truth objects (video, audio, documents)
#     stay until explicit deletion.

data "aws_caller_identity" "current" {}

locals {
  common_tags = merge(var.tags, {
    "nexus-platform/env" = var.env
  })
}

resource "aws_s3_bucket" "this" {
  bucket = var.bucket_name

  force_destroy = false

  tags = merge(local.common_tags, { Name = var.bucket_name })
}

resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

# ── TLS-only bucket policy ───────────────────────────────────────

data "aws_iam_policy_document" "tls_only" {
  statement {
    sid     = "DenyInsecureConnections"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = [
      aws_s3_bucket.this.arn,
      "${aws_s3_bucket.this.arn}/*",
    ]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "tls_only" {
  bucket = aws_s3_bucket.this.id
  policy = data.aws_iam_policy_document.tls_only.json
}

# ── Lifecycle ────────────────────────────────────────────────────

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  count  = length(var.lifecycle_rules) == 0 ? 0 : 1
  bucket = aws_s3_bucket.this.id

  dynamic "rule" {
    for_each = var.lifecycle_rules
    content {
      id     = rule.value.id
      status = lookup(rule.value, "enabled", true) ? "Enabled" : "Disabled"

      filter {
        prefix = rule.value.prefix
      }

      # Transitions: move to cheaper tiers.
      dynamic "transition" {
        for_each = lookup(rule.value, "transitions", [])
        content {
          days          = transition.value.days
          storage_class = transition.value.storage_class
        }
      }

      # Expiration: delete after N days.
      dynamic "expiration" {
        for_each = lookup(rule.value, "expiration_days", null) == null ? [] : [rule.value.expiration_days]
        content {
          days = expiration.value
        }
      }

      # Noncurrent (versioned) cleanup.
      noncurrent_version_expiration {
        noncurrent_days = lookup(rule.value, "noncurrent_expiration_days", 30)
      }

      abort_incomplete_multipart_upload {
        days_after_initiation = lookup(rule.value, "abort_multipart_days", 7)
      }
    }
  }
}
