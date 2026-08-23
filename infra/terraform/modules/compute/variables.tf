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

# This module intentionally does NOT create the IAM role/policies that back
# the instance profile (ssm:GetParameters access, the SSM Managed Instance
# Core policy, etc.) - that lives in `modules/iam`. This module only
# *attaches* an instance profile by name, via this required variable. The
# root config wires in `module.iam.instance_profile_name` (see
# infra/terraform/main.tf).
variable "iam_instance_profile_name" {
  description = "Name of the IAM instance profile to attach to the EC2 instance. The instance profile itself (IAM role + policies for SSM access) is created by modules/iam; this module only references it by name."
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
