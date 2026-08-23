output "instance_id" {
  description = "ID of the application EC2 instance."
  value       = module.compute.instance_id
}

output "elastic_ip" {
  description = "Stable public Elastic IP address of the application instance."
  value       = module.compute.elastic_ip
}

output "security_group_id" {
  description = "ID of the application security group."
  value       = module.network.security_group_id
}

output "instance_role_arn" {
  description = "ARN of the EC2 instance's IAM role."
  value       = module.iam.instance_role_arn
}

output "github_deploy_role_arn" {
  description = "ARN of the GitHub Actions OIDC deploy role, used by PR12's CD workflow (aws-actions/configure-aws-credentials `role-to-assume`)."
  value       = module.iam.github_deploy_role_arn
}

output "backup_s3_bucket_name" {
  description = "Name of the application backup bucket, to be set as `BACKUP_S3_BUCKET` for app/workers/backup.py."
  value       = aws_s3_bucket.backup.bucket
}
