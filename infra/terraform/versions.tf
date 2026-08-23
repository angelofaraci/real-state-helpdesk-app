terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state backend.
  #
  # SEAM FOR PR10: This backend configuration references an S3 bucket and
  # DynamoDB lock table that do NOT exist yet. They are created by a
  # separate *bootstrap* Terraform configuration (local state, scope of
  # PR10) that is intentionally kept outside this root config to avoid a
  # chicken-and-egg problem (you cannot use an S3 backend to create the S3
  # bucket that backend depends on).
  #
  # Until PR10's bootstrap config has been applied, `terraform init` on
  # this root config will fail to configure the backend - that is
  # expected. Once the bootstrap resources exist, run:
  #   terraform init \
  #     -backend-config="bucket=<bootstrap-created-bucket-name>" \
  #     -backend-config="dynamodb_table=<bootstrap-created-table-name>"
  # or fill in the values below directly.
  backend "s3" {
    bucket         = "real-state-helpdesk-terraform-state" # created by PR10's bootstrap config
    key            = "real-state-helpdesk/main.tfstate"
    region         = "us-east-1"
    dynamodb_table = "real-state-helpdesk-terraform-locks" # created by PR10's bootstrap config
    encrypt        = true
  }
}
