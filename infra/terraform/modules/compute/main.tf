# Compute module
#
# Creates the single application EC2 instance, an Elastic IP so its public
# address is stable across stop/start (needed for DNS + Caddy TLS in
# PR11), and attaches an IAM instance profile (created elsewhere, see the
# seam note on `var.iam_instance_profile_name` in variables.tf).
#
# Base AMI choice: Ubuntu 22.04 LTS (Jammy), amd64, published by Canonical
# (owner ID 099720109477). Chosen over Amazon Linux 2023 for broader
# familiarity/documentation, first-class Docker support, and long-term
# support through April 2027. Either distribution would work fine for a
# Docker-based deployment; Ubuntu LTS was the simplest default.

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  ami_id = coalesce(var.ami_id, data.aws_ami.ubuntu.id)

  # Minimal first-boot bootstrapping: install Docker Engine + the Docker
  # Compose plugin so the instance is ready to receive deploy commands via
  # SSM (PR12's CD pipeline). Deliberately does NOT pull/run the
  # application itself - that is PR12's job, not this PR's.
  user_data = <<-EOF
    #!/bin/bash
    set -euxo pipefail

    # Install Docker Engine + Compose plugin (official convenience script).
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh

    # Allow the default cloud-init user to run docker without sudo.
    usermod -aG docker ubuntu || true

    systemctl enable docker
    systemctl start docker
  EOF
}

resource "aws_instance" "app" {
  ami                    = local.ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [var.security_group_id]
  iam_instance_profile   = var.iam_instance_profile_name

  # No `key_name` is set: port 22 is closed at the security group level and
  # SSM Session Manager is used for administrative access instead, so no
  # SSH key pair is required.

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens   = "required" # enforce IMDSv2
    http_endpoint = "enabled"
  }

  user_data                   = local.user_data
  user_data_replace_on_change = false

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-app"
  })
}

resource "aws_eip" "app" {
  instance = aws_instance.app.id
  domain   = "vpc"

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-app-eip"
  })
}
