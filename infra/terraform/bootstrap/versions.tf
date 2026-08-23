terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Local state, intentionally. See README.md: this config creates ONLY the
  # S3 bucket + DynamoDB table backing the root config's `backend "s3"`
  # (infra/terraform/versions.tf) - a genuinely chicken-and-egg problem,
  # since a remote backend can't be used to create the bucket it depends
  # on. Its state file (`terraform.tfstate`) is deliberately committed to
  # git: it contains only non-sensitive resource metadata (bucket/table
  # names, ARNs, ARNs of the encryption config), never secrets.
  backend "local" {
    path = "terraform.tfstate"
  }
}
