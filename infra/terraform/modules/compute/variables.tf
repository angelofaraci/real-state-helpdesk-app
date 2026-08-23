variable "name_prefix" {
  description = "Prefix used to name resources created by this module."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type."
  type        = string
  default     = "t3.small"
}

variable "security_group_id" {
  description = "ID of the security group to attach to the instance (from the network module)."
  type        = string
}

variable "subnet_id" {
  description = "ID of the subnet to launch the instance in. Leave null to let AWS pick a default subnet in the default VPC."
  type        = string
  default     = null
}

variable "ami_id" {
  description = "AMI ID to use for the instance. Leave null to auto-select the latest Ubuntu 22.04 LTS AMI (see data source in main.tf)."
  type        = string
  default     = null
}

# SEAM FOR PR10:
# This PR intentionally does NOT create the IAM role/policies that back the
# instance profile (that's PR10's scope: ssm:GetParameters access, the SSM
# Managed Instance Core policy, etc.). Instead, this module only *attaches*
# an instance profile by name, via this required variable with no default.
# PR10's stacked branch is expected to add an `iam` module that creates the
# real `aws_iam_instance_profile` and pass its `.name` output into this
# variable from the root config. Until PR10 lands, the root config must
# supply a placeholder value (see root variables.tf) to keep this PR
# self-contained and independently plannable.
variable "iam_instance_profile_name" {
  description = "Name of the IAM instance profile to attach to the EC2 instance. The instance profile itself (IAM role + policies for SSM access) is created by PR10; this module only references it by name."
  type        = string
}

variable "root_volume_size_gb" {
  description = "Size (in GB) of the root EBS volume."
  type        = number
  default     = 20
}

variable "tags" {
  description = "Common tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}
