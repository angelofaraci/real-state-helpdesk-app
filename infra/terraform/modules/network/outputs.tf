output "security_group_id" {
  description = "ID of the application security group."
  value       = aws_security_group.app.id
}

output "vpc_id" {
  description = "ID of the VPC used by the security group."
  value       = data.aws_vpc.selected.id
}
