# Terraform infrastructure

This directory manages the AWS infrastructure for the real-state-helpdesk
application: a single EC2 instance, its Elastic IP, and its security group.

## Two-config structure: bootstrap + main

This configuration is split into two independent Terraform configs, to
avoid a chicken-and-egg problem where the S3 backend used for remote state
would itself need to be created by a Terraform run that has nowhere to
store its state:

1. **`bootstrap/`**: a small, separately-applied config using **local
   state** (committed to git — see `bootstrap/README.md`) that creates the
   S3 bucket and DynamoDB lock table referenced by this config's `backend
   "s3"` block in `versions.tf`.
2. **This directory (`infra/terraform/`, the "main" config)**: uses the
   `backend "s3"` created by the bootstrap config for its remote state,
   and defines the actual application infrastructure (network, compute,
   and iam modules, plus the flat `ssm.tf` secrets and the backup bucket).

`terraform init` in this directory requires `bootstrap/` to have been
applied first, so the referenced bucket/table already exist. See
`bootstrap/README.md` for the full bootstrap-then-init flow. Neither
config is applied in CI or by this repository's automation — both are
written and validated only (no AWS credentials available in that
context); `terraform apply` is a manual, human-run operation.

## Modules

- `modules/network`: the application security group. Ports 80/443 open to
  the world, port 22 CLOSED (no SSH ingress at all — SSM Session Manager
  is used for admin access instead), all outbound traffic allowed.
- `modules/compute`: the EC2 instance (`t3.small`, Ubuntu 22.04 LTS),
  an Elastic IP for a stable public address, and an attached IAM instance
  profile. `user_data` only installs Docker + the Compose plugin on first
  boot — it does **not** deploy the application (that's PR12's CD
  pipeline, via SSM `send-command`).
- `modules/iam`: the EC2 instance role (SSM Session Manager/Run Command
  access via `AmazonSSMManagedInstanceCore`, plus read-only access to this
  application's SSM Parameter Store secrets) and the GitHub Actions OIDC
  deploy role used by PR12's CD pipeline to run `ssm:SendCommand` against
  the instance. See `modules/iam/main.tf` for the full rationale.

## Secrets (`ssm.tf`) and the backup bucket (`s3-backup.tf`)

- `ssm.tf` defines `SecureString` SSM parameters (DB password, JWT
  signing key, LLM/email/WhatsApp provider secrets) under the
  `/helpdesk/prod/` path prefix, each sourced from a `sensitive = true`
  variable — see `terraform.tfvars.example` for the expected `-var-file`
  shape. Real values live only in a local, gitignored `*.tfvars` file,
  never committed.
- `s3-backup.tf` defines the **application** backup bucket that
  `app/workers/backup.py` uploads nightly `pg_dump` archives to (via
  `settings.backup_s3_bucket`) — a different bucket from the
  Terraform-state bucket created in `bootstrap/`. A lifecycle rule expires
  objects under the `pg_dump/` prefix after 14 days.

## Running this

```sh
# 1. Bootstrap once per AWS account/environment (see bootstrap/README.md).
cd bootstrap && terraform init && terraform apply && cd ..

# 2. Then the main config, supplying secrets via a gitignored tfvars file.
cp terraform.tfvars.example terraform.tfvars   # fill in real values
terraform init
terraform validate
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

`terraform validate` and `terraform fmt -check` can be run without AWS
credentials and without the backend being configured (`terraform init
-backend=false`), and are suitable as a CI check. Wiring an automated
`terraform validate` step into CI is PR12's scope.
