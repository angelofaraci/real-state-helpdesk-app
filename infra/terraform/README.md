# Terraform infrastructure

This directory manages the AWS infrastructure for the real-state-helpdesk
application: a single EC2 instance, its Elastic IP, and its security group.

## Two-config structure: bootstrap + main

This configuration is split into two independent Terraform configs, to
avoid a chicken-and-egg problem where the S3 backend used for remote state
would itself need to be created by a Terraform run that has nowhere to
store its state:

1. **`bootstrap/`** (not yet written — this is PR10's scope): a small,
   separately-applied config using **local state** that creates the S3
   bucket and DynamoDB lock table referenced by this config's `backend
   "s3"` block in `versions.tf`.
2. **This directory (`infra/terraform/`, the "main" config)**: uses the
   `backend "s3"` created by the bootstrap config for its remote state,
   and defines the actual application infrastructure (network + compute
   modules).

Until PR10 adds `bootstrap/`, `terraform init` in this directory will fail
to configure its S3 backend, because the referenced bucket/table do not
exist yet. That is expected for this PR, which only writes the Terraform
code — it is not applied here.

## Modules

- `modules/network`: the application security group. Ports 80/443 open to
  the world, port 22 CLOSED (no SSH ingress at all — SSM Session Manager
  is used for admin access instead), all outbound traffic allowed.
- `modules/compute`: the EC2 instance (`t3.small`, Ubuntu 22.04 LTS),
  an Elastic IP for a stable public address, and an attached IAM instance
  profile. `user_data` only installs Docker + the Compose plugin on first
  boot — it does **not** deploy the application (that's PR12's CD
  pipeline, via SSM `send-command`).

## IAM instance profile seam (PR10)

This PR intentionally does **not** create the IAM role or policies backing
the EC2 instance profile (`ssm:GetParameters` access, the SSM Managed
Instance Core managed policy, etc.) — that is PR10's scope. Instead:

- `modules/compute` accepts a required `iam_instance_profile_name` string
  variable and only attaches a profile by name.
- The root config exposes this as `var.iam_instance_profile_name` with
  **no default**, forcing an explicit value (a placeholder today, or
  `module.iam.instance_profile_name` once PR10's `iam` module lands and is
  wired in).

This keeps PR9 self-contained and independently plannable/validatable
without depending on PR10 landing first, even though PR10 is stacked on
top of this branch.

## Running this

```sh
# Requires AWS credentials and the bootstrap config (PR10) having been
# applied first, so the S3 backend bucket/table referenced in versions.tf
# actually exist.
terraform init
terraform validate
terraform plan -var="iam_instance_profile_name=<profile-name>"
terraform apply -var="iam_instance_profile_name=<profile-name>"
```

`terraform validate` and `terraform fmt -check` can be run without AWS
credentials and without the backend being configured (`terraform init
-backend=false`), and are suitable as a CI check. Wiring an automated
`terraform validate` step into CI is PR12's scope.
