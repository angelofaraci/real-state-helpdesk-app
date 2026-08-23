output "instance_id" {
  description = "ID of the application EC2 instance."
  value       = aws_instance.app.id
}

output "elastic_ip" {
  description = "Stable public IP address (Elastic IP) associated with the instance."
  value       = aws_eip.app.public_ip
}

output "ami_id" {
  description = "AMI ID actually used to launch the instance."
  value       = local.ami_id
}
