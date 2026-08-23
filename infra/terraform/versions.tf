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
  # This bucket and lock table are created by the separate *bootstrap*
  # Terraform configuration in infra/terraform/bootstrap/ (local state,
  # committed to git - see its README.md), kept outside this root config to
  # avoid a chicken-and-egg problem (you cannot use an S3 backend to create
  # the S3 bucket that backend depends on).
  #
  # `terraform init` on this root config requires the bootstrap config to
  # have been applied first, so these exact resources already exist. The
  # names below MUST stay consistent with `bootstrap/main.tf`'s
  # `"${var.name_prefix}-terraform-state"` / `"${var.name_prefix}-terraform-locks"`
  # resource names (both default to the `real-state-helpdesk` prefix).
  backend "s3" {
    bucket         = "real-state-helpdesk-terraform-state" # created by infra/terraform/bootstrap/
    key            = "real-state-helpdesk/main.tfstate"
    region         = "us-east-1"
    dynamodb_table = "real-state-helpdesk-terraform-locks" # created by infra/terraform/bootstrap/
    encrypt        = true
  }
}
