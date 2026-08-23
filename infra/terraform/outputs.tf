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
