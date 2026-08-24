variable "name_prefix" {
  description = "Prefix used to name all resources created by this module."
  type        = string
}

variable "ssm_parameter_path_prefix" {
  description = <<-EOT
    SSM Parameter Store path prefix that the instance role is allowed to
    read (via ssm:GetParameter/GetParameters/GetParametersByPath). Must
    match the prefix used by the `aws_ssm_parameter` resources defined in
    the root `ssm.tf`, e.g. "/helpdesk/prod". Deliberately does NOT include
    a trailing "/*" here - it is appended where needed for ARN scoping.
  EOT
  type        = string
  default     = "/helpdesk/prod"
}

variable "instance_name_tag" {
  description = <<-EOT
    Value of the `Name` tag on the application EC2 instance (see
    `modules/compute`'s `Name = "$${var.name_prefix}-app"` tag). Used to
    scope the GitHub deploy role's `ssm:SendCommand` permission to only
    that specific tagged instance, via an `aws:ResourceTag` condition,
    without needing the instance's ARN (which isn't known yet - the iam
    module is created before/independently of compute, and passing the
    instance ARN in would create a circular module dependency since compute
    itself depends on this module's instance profile output).
  EOT
  type        = string
  default     = null
}

variable "github_owner" {
  description = "GitHub organization/user that owns the deploy repository."
  type        = string
  default     = "angelofaraci"
}

variable "github_repo" {
  description = "GitHub repository name allowed to assume the deploy role via OIDC."
  type        = string
  default     = "real-state-helpdesk"
}

variable "github_oidc_subject" {
  description = <<-EOT
    Exact `sub` claim the GitHub Actions OIDC token must present to assume
    the deploy role, e.g. "repo:<owner>/<repo>:ref:refs/heads/main" to
    scope deploys to pushes/merges on `main` only (chosen over the broader
    "repo:<owner>/<repo>:*", which would also allow arbitrary PR branches
    and other workflow triggers to assume a role that can run commands on
    the production EC2 instance - the extra safety of pinning to `main`
    outweighs the minor inconvenience of updating this if the deploy
    workflow's trigger ever changes).
  EOT
  type        = string
  default     = "repo:angelofaraci/real-state-helpdesk-app:ref:refs/heads/main"
}

variable "create_github_oidc_provider" {
  description = <<-EOT
    Whether to create the `token.actions.githubusercontent.com` OIDC
    identity provider. GitHub's OIDC provider is a single, account-wide
    resource; if this AWS account already has one registered (e.g. from
    the "URL shortener" project mentioned in the design doc), set this to
    `false` and instead `terraform import` the existing provider into this
    module's `aws_iam_openid_connect_provider.github[0]` address so
    Terraform manages it going forward instead of trying to recreate it
    (AWS rejects a second provider for the same URL). Defaults to `true`
    for a fresh account; this is intentionally NOT a data-source-based
    conditional-create pattern - keeping this a plain boolean toggle avoids
    over-engineering a "does it already exist" auto-detection for a
    resource that only changes at apply time, on a per-account basis, by a
    human who already knows the answer.
  EOT
  type        = bool
  default     = true
}

variable "tags" {
  description = "Common tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}
