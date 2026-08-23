# IAM module
#
# Creates two independent IAM identities:
#
#   1. The EC2 instance role/profile (`aws_iam_role.instance` +
#      `aws_iam_instance_profile.instance`), attached to the application
#      instance created by `modules/compute`. Grants SSM Session Manager /
#      Run Command access (`AmazonSSMManagedInstanceCore`) plus read-only
#      access to this application's SSM Parameter Store secrets.
#
#   2. The GitHub Actions OIDC deploy role (`aws_iam_role.github_deploy`),
#      assumable only by GitHub Actions workflows running in this specific
#      repository (see `var.github_oidc_subject`), used by PR12's CD
#      pipeline to trigger `ssm:SendCommand` deploys against the instance.
#      Note that GHCR (GitHub Container Registry) push authentication does
#      NOT go through AWS IAM at all - it uses a GitHub token
#      (`GITHUB_TOKEN` or a PAT), so there is no AWS-side permission to
#      grant for that part of the deploy flow. The only AWS permission this
#      role needs is the ability to command the deploy instance via SSM.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_partition" "current" {}

locals {
  ssm_parameter_arn_prefix = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_parameter_path_prefix}"
}

# --- 1. EC2 instance role -----------------------------------------------

data "aws_iam_policy_document" "instance_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.name_prefix}-instance-role"
  assume_role_policy = data.aws_iam_policy_document.instance_assume_role.json

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-instance-role"
  })
}

# SSM Session Manager / Run Command access - lets admins connect without an
# open SSH port, and lets PR12's CD pipeline push deploy commands.
resource "aws_iam_role_policy_attachment" "instance_ssm_core" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "instance_ssm_parameters" {
  statement {
    sid = "ReadApplicationSecrets"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    # Scoped to this application's own parameter path prefix only - the
    # instance role cannot read any other SSM parameter in the account.
    resources = [
      local.ssm_parameter_arn_prefix,
      "${local.ssm_parameter_arn_prefix}/*",
    ]
  }
}

resource "aws_iam_policy" "instance_ssm_parameters" {
  name        = "${var.name_prefix}-instance-ssm-parameters"
  description = "Allows the application instance to read its own SSM Parameter Store secrets under ${var.ssm_parameter_path_prefix}."
  policy      = data.aws_iam_policy_document.instance_ssm_parameters.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "instance_ssm_parameters" {
  role       = aws_iam_role.instance.name
  policy_arn = aws_iam_policy.instance_ssm_parameters.arn
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.name_prefix}-instance-profile"
  role = aws_iam_role.instance.name

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-instance-profile"
  })
}

# --- 2. GitHub OIDC deploy role -------------------------------------------

# GitHub's OIDC provider well-known thumbprint. GitHub rotates its
# intermediate CA and publishes/updates this value; AWS also now supports
# validating solely via the audience without pinning a thumbprint, but we
# keep an explicit thumbprint list for older provider compatibility.
# See: https://github.blog/changelog/2023-06-27-github-actions-update-on-oidc-integration-with-aws/
locals {
  github_oidc_thumbprints = ["1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
  github_oidc_audience    = "sts.amazonaws.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = [local.github_oidc_audience]
  thumbprint_list = local.github_oidc_thumbprints

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-github-oidc"
  })
}

locals {
  # ARN of the OIDC provider this role trusts. When
  # `create_github_oidc_provider = false` (the provider already exists in
  # this AWS account from another project), this must be supplied via
  # `terraform import` against `aws_iam_openid_connect_provider.github[0]`
  # rather than referenced as a separate data source, to keep this module's
  # interface simple (a single toggle instead of an extra "existing
  # provider ARN" input).
  github_oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_deploy_assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = [local.github_oidc_audience]
    }

    # Pinned to the exact repo + ref (see var.github_oidc_subject docstring
    # for why `ref:refs/heads/main` was chosen over a broader `:*` match).
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [var.github_oidc_subject]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = "${var.name_prefix}-github-deploy-role"
  assume_role_policy = data.aws_iam_policy_document.github_deploy_assume_role.json

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-github-deploy-role"
  })
}

data "aws_iam_policy_document" "github_deploy_ssm" {
  statement {
    sid = "SendDeployCommand"
    actions = [
      "ssm:SendCommand",
    ]
    # ssm:SendCommand needs permission on both the target instance and the
    # SSM document it runs. The instance is scoped by its Name tag (not by
    # ARN, since this module has no dependency on `modules/compute` and
    # therefore cannot reference the instance's concrete ARN without
    # creating a circular module dependency - compute depends on this
    # module's instance profile output).
    resources = [
      "arn:${data.aws_partition.current.partition}:ec2:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:instance/*",
      "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.name}::document/AWS-RunShellScript",
    ]

    dynamic "condition" {
      for_each = var.instance_name_tag == null ? [] : [1]
      content {
        test     = "StringEquals"
        variable = "ssm:resourceTag/Name"
        values   = [var.instance_name_tag]
      }
    }
  }

  statement {
    sid = "ReadDeployCommandStatus"
    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ssm:ListCommands",
    ]
    # These SSM read APIs are not resource-scopable to a single instance in
    # IAM (they take a command ID, not an instance ARN, as their primary
    # identifier) - allowed account-wide for this role, which itself is
    # already tightly scoped to this one repository via the OIDC trust
    # policy above.
    resources = ["*"]
  }
}

resource "aws_iam_policy" "github_deploy_ssm" {
  name        = "${var.name_prefix}-github-deploy-ssm"
  description = "Allows the GitHub Actions deploy workflow to run and poll SSM Run Command against the application instance."
  policy      = data.aws_iam_policy_document.github_deploy_ssm.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "github_deploy_ssm" {
  role       = aws_iam_role.github_deploy.name
  policy_arn = aws_iam_policy.github_deploy_ssm.arn
}
