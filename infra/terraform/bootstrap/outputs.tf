output "state_bucket_name" {
  description = "Name of the S3 bucket holding the root config's Terraform state. Must match the `bucket` value in the root config's `backend \"s3\"` block (infra/terraform/versions.tf)."
  value       = aws_s3_bucket.terraform_state.bucket
}

output "lock_table_name" {
  description = "Name of the DynamoDB table holding the root config's state lock. Must match the `dynamodb_table` value in the root config's `backend \"s3\"` block (infra/terraform/versions.tf)."
  value       = aws_dynamodb_table.terraform_locks.name
}
