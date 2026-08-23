variable "aws_region" {
  description = "AWS region to create the Terraform state bucket/lock table in. Must match the root config's `aws_region`."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix used to name the state bucket and lock table. Must stay consistent with the bucket/table names referenced in the root config's `backend \"s3\"` block (infra/terraform/versions.tf)."
  type        = string
  default     = "real-state-helpdesk"
}

variable "tags" {
  description = "Common tags applied to all resources created by this configuration."
  type        = map(string)
  default = {
    Project   = "real-state-helpdesk"
    ManagedBy = "terraform"
    Purpose   = "terraform-bootstrap"
  }
}
