provider "aws" {
  region = var.aws_region
}

module "network" {
  source = "./modules/network"

  name_prefix = var.name_prefix
  tags        = var.tags
}

module "iam" {
  source = "./modules/iam"

  name_prefix               = var.name_prefix
  ssm_parameter_path_prefix = var.ssm_parameter_path_prefix
  instance_name_tag         = "${var.name_prefix}-app"
  github_owner              = var.github_owner
  github_repo               = var.github_repo
  github_oidc_subject       = var.github_oidc_subject

  tags = var.tags
}

module "compute" {
  source = "./modules/compute"

  name_prefix   = var.name_prefix
  instance_type = var.instance_type

  security_group_id = module.network.security_group_id

  # Seam closed in PR10: the instance profile now comes from the `iam`
  # module created above, instead of the placeholder `var.iam_instance_profile_name`
  # PR9 required callers to supply manually.
  iam_instance_profile_name = module.iam.instance_profile_name

  tags = var.tags
}
