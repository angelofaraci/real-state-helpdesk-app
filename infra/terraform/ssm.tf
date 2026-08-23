# SSM Parameter Store secrets
#
# Flat list of SecureString parameters, deliberately kept at the ROOT (not
# wrapped in a module) - there is exactly one application, one environment
# (`prod`), and no reuse case that would justify a module here.
#
# Every parameter's `value` is sourced from a `sensitive = true` Terraform
# variable (see variables.tf) - NEVER a literal string - and those
# variables must be supplied via a gitignored `-var-file`
# (see terraform.tfvars.example for the expected shape; `*.tfvars` is
# gitignored except `*.tfvars.example`).
#
# Path prefix (`/helpdesk/prod/`) matches `var.ssm_parameter_path_prefix`,
# which also scopes the `iam` module's instance-role read policy - keep
# these in sync if the prefix ever changes.
#
# Names below are enumerated from the real Settings fields in
# app/core/config.py that hold secrets:
#   - DB_PASSWORD            -> assembled into DATABASE_URL at deploy time
#   - JWT_SECRET              -> Settings.jwt_secret
#   - OPENAI_API_KEY          -> Settings.openai_api_key (LLM provider)
#   - SECRET_ENCRYPTION_KEY   -> Settings.secret_encryption_key (WhatsApp/
#                                multichannel credential Fernet key)
#   - WHATSAPP_VERIFY_TOKEN   -> Settings.whatsapp_verify_token (Meta
#                                webhook verify token)
#   - MAILGUN_SIGNING_KEY     -> Settings.mailgun_signing_key (email
#                                provider webhook signing key)

resource "aws_ssm_parameter" "db_password" {
  name      = "${var.ssm_parameter_path_prefix}/DB_PASSWORD"
  type      = "SecureString"
  value     = var.db_password
  overwrite = true

  tags = var.tags
}

resource "aws_ssm_parameter" "jwt_secret" {
  name      = "${var.ssm_parameter_path_prefix}/JWT_SECRET"
  type      = "SecureString"
  value     = var.jwt_secret
  overwrite = true

  tags = var.tags
}

resource "aws_ssm_parameter" "openai_api_key" {
  name      = "${var.ssm_parameter_path_prefix}/OPENAI_API_KEY"
  type      = "SecureString"
  value     = var.openai_api_key
  overwrite = true

  tags = var.tags
}

resource "aws_ssm_parameter" "secret_encryption_key" {
  name      = "${var.ssm_parameter_path_prefix}/SECRET_ENCRYPTION_KEY"
  type      = "SecureString"
  value     = var.secret_encryption_key
  overwrite = true

  tags = var.tags
}

resource "aws_ssm_parameter" "whatsapp_verify_token" {
  name      = "${var.ssm_parameter_path_prefix}/WHATSAPP_VERIFY_TOKEN"
  type      = "SecureString"
  value     = var.whatsapp_verify_token
  overwrite = true

  tags = var.tags
}

resource "aws_ssm_parameter" "mailgun_signing_key" {
  name      = "${var.ssm_parameter_path_prefix}/MAILGUN_SIGNING_KEY"
  type      = "SecureString"
  value     = var.mailgun_signing_key
  overwrite = true

  tags = var.tags
}
