# Terraform bootstrap config

Creates the two resources the root config (`infra/terraform/`) needs before
it can even run `terraform init`: the S3 bucket + DynamoDB table backing its
`backend "s3"` block.

## Why this is a separate config

A `backend "s3"` block cannot be used to create the very S3 bucket it
depends on - `terraform init` needs the backend's bucket to already exist.
This config solves that chicken-and-egg problem by living outside the root
config, with its own `backend "local"` (see `versions.tf`), and creating
only the bucket + lock table the root config's backend then references.

## What this creates

1. **S3 bucket** (`aws_s3_bucket.terraform_state`): versioned, SSE (AES256)
   encrypted, all public access blocked. Holds the root config's
   `main.tfstate`.
2. **DynamoDB table** (`aws_dynamodb_table.terraform_locks`): a standard
   Terraform S3-backend lock table, with a `LockID` string primary key and
   pay-per-request billing (this table sees near-zero traffic - one item
   write per `terraform plan`/`apply`).

## Guardrail: this state file IS committed to git

Unlike the root config (remote S3 state), this config uses `backend
"local"` and its `terraform.tfstate` is committed. That is safe ONLY
because this config is restricted to the two non-sensitive resources
above. **Never add an `aws_ssm_parameter`, credential, or any other
secret-bearing resource here** - see the guardrail comment at the top of
`main.tf` for what to do if that constraint is ever violated (migrate off
`backend "local"` immediately, before applying the change).

All application secrets (DB password, JWT signing key, API keys, etc.) live
in the root config's `ssm.tf` instead, which runs on the remote S3 backend
this bootstrap config creates - never in local, committed state.

## Bootstrap-then-init flow

Run this config exactly once per AWS account (or environment), before the
root config can be initialized:

```sh
cd infra/terraform/bootstrap
terraform init
terraform validate
terraform plan
terraform apply
```

After `apply` succeeds, note the outputs (`state_bucket_name`,
`lock_table_name`) - they should already match the values hardcoded in the
root config's `backend "s3"` block (`infra/terraform/versions.tf`), since
both configs derive the same names from the same `name_prefix` convention
(`<name_prefix>-terraform-state` / `<name_prefix>-terraform-locks`). If you
changed `var.name_prefix` from its default, update the root config's
backend block to match before running `terraform init` there.

Then, and only then, initialize and apply the root config:

```sh
cd ../
terraform init
terraform validate
terraform plan
terraform apply
```

This bootstrap config never needs to be re-run except when re-bootstrapping
a brand-new AWS account/environment from scratch; it is not part of normal
day-to-day `terraform apply` workflows on the root config.
