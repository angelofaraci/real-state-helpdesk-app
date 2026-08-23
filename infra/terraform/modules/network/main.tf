# Network module
#
# Creates the security group used by the application EC2 instance.
#
# Security posture:
#   - Port 80 (HTTP) and 443 (HTTPS) are open to the world (0.0.0.0/0) so
#     Caddy (see PR11) can terminate TLS and serve the app.
#   - Port 22 (SSH) is intentionally NOT opened. There is no ingress rule
#     for it at all. Remote access is done exclusively via AWS SSM Session
#     Manager (IAM role wired in PR10), so no SSH ingress and no bastion
#     host are needed.
#   - All outbound traffic is allowed, since the instance needs to reach
#     GHCR (pull images), AWS SSM endpoints, S3 (backups, see PR8), and the
#     OpenAI API, among others.

data "aws_vpc" "selected" {
  id      = var.vpc_id
  default = var.vpc_id == null ? true : null
}

resource "aws_security_group" "app" {
  name        = "${var.name_prefix}-app-sg"
  description = "Security group for the application EC2 instance (HTTP/HTTPS in, SSH closed, SSM for admin access)"
  vpc_id      = data.aws_vpc.selected.id

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-app-sg"
  })
}

resource "aws_security_group_rule" "ingress_http" {
  type              = "ingress"
  from_port         = 80
  to_port           = 80
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.app.id
  description       = "Allow inbound HTTP"
}

resource "aws_security_group_rule" "ingress_https" {
  type              = "ingress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.app.id
  description       = "Allow inbound HTTPS"
}

# NOTE: There is deliberately no ingress rule for port 22 (SSH). Access to
# the instance is via AWS SSM Session Manager only.

resource "aws_security_group_rule" "egress_all" {
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.app.id
  description       = "Allow all outbound traffic"
}
