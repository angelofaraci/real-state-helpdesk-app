# Homelab staging environment (Proxmox + Cloudflare Tunnel)

This is a documentation-only note. Nothing here is implemented, provisioned,
or wired into `infra/terraform/`, CI, or any deploy pipeline — it exists
because the idea is worth writing down even though it is not currently
used, per the earlier design discussion for this stage.

## The idea

The real production deploy target is a single EC2 instance, managed by
Terraform (`infra/terraform/`) and deployed to via SSM Run Command
(PR12's CD pipeline). Every change to that instance costs real AWS spend
and touches the actual production environment directly — there is no
staging tier in between.

A self-hosted Proxmox VE homelab could fill that gap at effectively zero
marginal cost:

1. **Proxmox VM as the staging host.** A VM (or LXC container) on a
   Proxmox node runs the same Docker Compose stack (`docker-compose.yml`)
   the production EC2 instance would run, using the same container images
   published to GHCR. This gives a realistic pre-production smoke-test
   environment — full stack, same images, same compose file — without
   touching AWS at all.
2. **Cloudflare Tunnel for public reachability.** Rather than exposing the
   homelab's IP directly (dynamic residential IP, no public inbound
   firewall rules desired), a `cloudflared` tunnel running as a container
   or systemd service on the Proxmox VM exposes the staging stack under a
   subdomain (e.g. `staging.<domain>`) through Cloudflare's edge, with TLS
   handled by Cloudflare rather than needing Caddy/Let's Encrypt on the
   homelab side. No open ports on the home router are required.
3. **Promotion flow.** A build that passes smoke tests against the homelab
   staging environment is the one promoted to production: the same image
   tag pushed to GHCR gets deployed to the real EC2 instance via the
   existing SSM-based CD pipeline. The homelab never touches production
   credentials, SSM, or AWS IAM in any way — it is a fully separate,
   disposable environment that only shares the container images and
   compose file shape.

## Why this is worth documenting even if unused

- **Zero incremental cost.** Reuses hardware that already exists for other
  purposes; no additional AWS spend for a staging tier.
- **Realistic pre-prod testing.** Running the actual Docker images against
  a full stack (Postgres, Redis, the API, the worker) catches integration
  issues that unit/integration tests in CI cannot.
- **No blast radius on production.** Since it is a wholly separate host
  with no AWS credentials, no IAM role, and no access to production SSM
  parameters or the production database, mistakes made against staging
  cannot reach production.
- **Optional, not required.** The production deploy pipeline (PR12) does
  not depend on this existing. It's a nice-to-have that a future PR could
  pick up if/when a staging tier becomes valuable enough to justify the
  homelab maintenance burden — it is explicitly out of scope for this
  stage's Terraform/CI work.

## What would be needed to actually build this (not done here)

- A Proxmox VM/LXC template with Docker + Compose installed (mirroring
  `modules/compute`'s `user_data` bootstrap script).
- A `docker-compose.staging.yml` override (or reused compose file with a
  staging `.env`) pointing at non-production database/Redis instances
  local to the homelab.
- A `cloudflared` tunnel configuration mapping a staging subdomain to the
  VM's local Compose stack, with a Cloudflare Zero Trust access policy if
  the staging environment should not be publicly world-readable.
- A manual or lightly-automated promotion step (e.g. a script or GitHub
  Actions workflow dispatch) that re-tags a smoke-tested staging image and
  triggers the existing production SSM deploy for it.

None of the above is implemented as part of this change.
