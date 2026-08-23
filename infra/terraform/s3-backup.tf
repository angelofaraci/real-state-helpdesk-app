# Application backup bucket
#
# NOT the Terraform-state bucket (that one is created by
# infra/terraform/bootstrap/ and referenced by this config's `backend "s3"`
# block in versions.tf). This is a separate, application-level bucket that
# `app/workers/backup.py`'s nightly `pg_dump` job uploads to, via
# `settings.backup_s3_bucket` (see PR8), under the `pg_dump/` key prefix.
#
# Retention: a 14-day expiration lifecycle rule on `pg_dump/` enforces
# "14 most recent daily backups" without any application-side cleanup code.

resource "aws_s3_bucket" "backup" {
  bucket = var.backup_s3_bucket_name

  tags = merge(var.tags, {
    Name = var.backup_s3_bucket_name
  })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "backup" {
  bucket = aws_s3_bucket.backup.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id

  rule {
    id     = "expire-pg-dump-backups"
    status = "Enabled"

    filter {
      prefix = "pg_dump/"
    }

    expiration {
      days = 14
    }
  }
}
