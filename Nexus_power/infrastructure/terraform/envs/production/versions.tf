terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.50.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0.0"
    }
    local = {
      source  = "hashicorp/local"
      version = ">= 2.4.0"
    }
  }

  # Remote state — fill in via init -backend-config. Bucket + table are
  # provisioned by a separate bootstrap stack (one-time, not in tree)
  # so the platform stack never tries to manage its own state backend.
  backend "s3" {
    # bucket         = "tfstate-<acct>"
    # key            = "nexus-platform/production/terraform.tfstate"
    # region         = "us-east-1"
    # dynamodb_table = "tfstate-lock-<acct>"
    # encrypt        = true
  }
}
