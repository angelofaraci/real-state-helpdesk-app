# Terraform bootstrap config
#
# ============================================================================
# GUARDRAIL - READ BEFORE ADDING ANYTHING TO THIS FILE
#
# This configuration uses `backend "local"` (see versions.tf) and its state
# file IS committed to git. That is only safe because this config creates
# EXACTLY TWO resources, neither of which is sensitive:
#
#   1. An S3 bucket to hold the root config's Terraform state.
#   2. A DynamoDB table to hold the root config's state lock.
#
# DO NOT add an `aws_ssm_parameter`, any credential, secret, private key, or
# any other secret-bearing resource to this file (or anywhere else in
# `infra/terraform/bootstrap/`). If that constraint is ever violated, this
# config's backend MUST be migrated off `backend "local"` to a non-committed
# backend (e.g. the S3 backend it itself creates, once it exists) BEFORE the
# secret-bearing resource is applied - never commit a state file that has
# ever contained a secret.
#
# All SSM Parameter Store secrets live in the ROOT config's `ssm.tf`
# instead, which uses the remote S3 backend created here.
# ============================================================================

provider "aws" {
  region = var.aws_region
}

# --- Terraform state bucket ------------------------------------------------

resource "aws_s3_bucket" "terraform_state" {
  bucket = "${var.name_prefix}-terraform-state"

  # Deliberately no `force_destroy`: destroying the state bucket by accident
  # would be catastrophic, so this requires manually emptying it first.

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-terraform-state"
  })
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- Terraform state lock table ---------------------------------------------

resource "aws_dynamodb_table" "terraform_locks" {
  name         = "${var.name_prefix}-terraform-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-terraform-locks"
  })
}
