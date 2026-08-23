variable "name_prefix" {
  description = "Prefix used to name resources created by this module."
  type        = string
}

variable "vpc_id" {
  description = "ID of the VPC in which the security group is created. Leave null to use the default VPC (looked up automatically)."
  type        = string
  default     = null
}

variable "tags" {
  description = "Common tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}
