variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 instance type for the application server."
  type        = string
  default     = "t3.small"
}

variable "name_prefix" {
  description = "Prefix used to name all resources created by this configuration."
  type        = string
  default     = "real-state-helpdesk"
}

variable "ssm_parameter_path_prefix" {
  description = <<-EOT
    SSM Parameter Store path prefix used both by the `iam` module (to scope
    the instance role's read permissions) and by the `aws_ssm_parameter`
    resources in ssm.tf. Keep these in sync - see ssm.tf.
  EOT
  type        = string
  default     = "/helpdesk/prod"
}

variable "github_owner" {
  description = "GitHub organization/user that owns the deploy repository, used to scope the GitHub OIDC deploy role's trust policy."
  type        = string
  default     = "angelofaraci"
}

variable "github_repo" {
  description = "GitHub repository name allowed to assume the deploy role via OIDC."
  type        = string
  default     = "real-state-helpdesk"
}

variable "github_oidc_subject" {
  description = "Exact OIDC `sub` claim the GitHub Actions deploy role's trust policy requires. See modules/iam/variables.tf for why this is pinned to `ref:refs/heads/main`."
  type        = string
  default     = "repo:angelofaraci/real-state-helpdesk-app:ref:refs/heads/main"
}

variable "backup_s3_bucket_name" {
  description = "Name of the S3 bucket the nightly pg_dump backup job (app/workers/backup.py, via `settings.backup_s3_bucket`) uploads to. Not the same bucket as Terraform state - see s3-backup.tf."
  type        = string
  default     = "real-state-helpdesk-backups"
}

# --- SSM Parameter Store secrets (see ssm.tf) -----------------------------
#
# Every value below is supplied via a gitignored `-var-file` (see
# terraform.tfvars.example for the expected variable names) - NEVER as a
# literal default here, and never committed in plaintext anywhere.

variable "db_password" {
  description = "Application database password, stored as an SSM SecureString and assembled into DATABASE_URL at deploy time (PR12)."
  type        = string
  sensitive   = true
}

variable "jwt_secret" {
  description = "JWT signing key (overrides Settings.jwt_secret's insecure local-dev default in production)."
  type        = string
  sensitive   = true
}

variable "openai_api_key" {
  description = "LLM provider (OpenAI) API key (Settings.openai_api_key)."
  type        = string
  sensitive   = true
}

variable "secret_encryption_key" {
  description = "Fernet key used to encrypt WhatsApp/multichannel credentials at rest (Settings.secret_encryption_key)."
  type        = string
  sensitive   = true
}

variable "whatsapp_verify_token" {
  description = "Meta (WhatsApp Cloud API) webhook verify token (Settings.whatsapp_verify_token)."
  type        = string
  sensitive   = true
}

variable "mailgun_signing_key" {
  description = "Email provider (Mailgun) webhook signing key (Settings.mailgun_signing_key)."
  type        = string
  sensitive   = true
}

variable "tags" {
  description = "Common tags applied to all resources."
  type        = map(string)
  default = {
    Project   = "real-state-helpdesk"
    ManagedBy = "terraform"
  }
}
