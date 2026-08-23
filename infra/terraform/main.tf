provider "aws" {
  region = var.aws_region
}

module "network" {
  source = "./modules/network"

  name_prefix = var.name_prefix
  tags        = var.tags
}

module "compute" {
  source = "./modules/compute"

  name_prefix   = var.name_prefix
  instance_type = var.instance_type

  security_group_id = module.network.security_group_id

  # SEAM FOR PR10: see the comment on var.iam_instance_profile_name in
  # variables.tf. Once PR10's iam module lands, replace this with
  # `module.iam.instance_profile_name`.
  iam_instance_profile_name = var.iam_instance_profile_name

  tags = var.tags
}
