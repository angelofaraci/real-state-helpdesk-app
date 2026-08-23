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

# SEAM FOR PR10:
# The IAM role + policies backing this instance profile (ssm:GetParameters,
# AmazonSSMManagedInstanceCore, etc.) are created by PR10's `iam` module,
# which is stacked on top of this PR's branch. This PR has no default here
# on purpose: it forces whoever runs `terraform plan`/`apply` on this
# config today to explicitly supply a placeholder (e.g. a manually created
# profile, or the literal string "TODO-pr10-iam-instance-profile") until
# PR10 lands and the root config is updated to wire in the real
# `module.iam.instance_profile_name` output instead.
variable "iam_instance_profile_name" {
  description = "Name of the IAM instance profile to attach to the EC2 instance. Supplied by PR10's IAM module once it lands; no default is provided here so this requirement is explicit."
  type        = string
}

variable "tags" {
  description = "Common tags applied to all resources."
  type        = map(string)
  default = {
    Project   = "real-state-helpdesk"
    ManagedBy = "terraform"
  }
}
