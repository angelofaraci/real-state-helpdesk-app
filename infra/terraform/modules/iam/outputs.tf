output "instance_profile_name" {
  description = "Name of the IAM instance profile, wired into `modules/compute`'s `iam_instance_profile_name` variable."
  value       = aws_iam_instance_profile.instance.name
}

output "instance_role_arn" {
  description = "ARN of the EC2 instance's IAM role."
  value       = aws_iam_role.instance.arn
}

output "github_deploy_role_arn" {
  description = "ARN of the GitHub Actions OIDC deploy role, used by PR12's CD workflow to assume via `aws-actions/configure-aws-credentials`."
  value       = aws_iam_role.github_deploy.arn
}

output "github_oidc_provider_arn" {
  description = "ARN of the GitHub Actions OIDC identity provider (created or referenced by this module)."
  value       = local.github_oidc_provider_arn
}
