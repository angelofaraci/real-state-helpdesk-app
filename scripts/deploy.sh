#!/usr/bin/env bash
#
# deploy.sh — PR12's CD pipeline main sequence. Runs ON THE INSTANCE,
# triggered by the GitHub Actions `cd.yml` workflow via
# `aws ssm send-command`.
#
# Load-bearing design decision (this PR's rollback mechanism is built on
# top of it): the instance's own `.env` file is the single source of truth
# for what it is CURRENTLY RUNNING. `.env.previous` is a snapshot of the
# last-known-good `.env`, taken right before this run's changes are
# applied. If the new deploy fails its smoke test, restoring
# `.env.previous` over `.env` and re-running `docker compose up -d` is
# sufficient to roll the running stack back to the previous image tag and
# previous rendered secrets, with no separate state store needed.
#
# IMPORTANT caveat this rollback mechanism depends on: rolling back `.env`
# rolls back the IMAGE_TAG, NOT the database schema — Alembic migrations
# already applied by step 5 are NOT undone by a rollback. This is only
# safe because every migration shipped in this stage is written to be
# additive/backward-compatible (see `app/alembic/versions/0011_invite_token_revoked_at.py`'s
# own docstring, re-confirmed here for PR12 since this is the PR where the
# rollback mechanism that actually depends on that guarantee gets
# implemented: 0011 adds a nullable column and widens a partial unique
# index's predicate to a strict subset of what it indexed before — the
# OLD, rolled-back application code observes identical behavior against
# the NEW schema, so a rollback to the previous image tag is safe to run
# without also reverting the migration).
#
# Sequence:
#   1. Snapshot current .env -> .env.previous (rollback basis).
#   2. Refresh secrets from SSM (render_env_from_ssm.sh) — in case any SSM
#      parameter changed since the last deploy.
#   3. Set/overwrite IMAGE_TAG in the resulting .env to the new tag.
#   4. `docker compose pull` the new image.
#   5. Run pending Alembic migrations using the NEW image, before bringing
#      up the full stack, so migrations apply against the new code's
#      schema expectations.
#   6. `docker compose up -d`.
#   7. Smoke test. On failure: restore .env.previous, re-run `up -d`, and
#      exit non-zero — a rolled-back deploy is still a FAILED deploy
#      attempt that needs investigation, not a silent success.
#
# Usage: deploy.sh <IMAGE_TAG>
#   (or set IMAGE_TAG as an env var, e.g. when invoked via SSM
#   RunShellScript with `parameters`/environment injection.)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env}"
ENV_PREVIOUS_FILE="${ENV_PREVIOUS_FILE:-.env.previous}"
# Overridable (tests substitute fakes here) — default to the real sibling
# scripts next to this one.
RENDER_ENV_SCRIPT="${RENDER_ENV_SCRIPT:-${SCRIPT_DIR}/render_env_from_ssm.sh}"
SMOKE_TEST_SCRIPT="${SMOKE_TEST_SCRIPT:-${SCRIPT_DIR}/smoke_test.sh}"

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

# set_image_tag <env_file> <tag>
# Sets/overwrites the IMAGE_TAG=... line in <env_file> in place (adds it if
# absent). No secret values pass through here — IMAGE_TAG is a public git
# SHA, not a secret.
set_image_tag() {
  local env_file="$1"
  local tag="$2"
  local tmp_file
  tmp_file="$(mktemp "${env_file}.XXXXXX")"
  chmod 600 "$tmp_file"

  if [[ -f "$env_file" ]] && grep -q '^IMAGE_TAG=' "$env_file"; then
    sed "s/^IMAGE_TAG=.*/IMAGE_TAG=${tag}/" "$env_file" > "$tmp_file"
  else
    if [[ -f "$env_file" ]]; then
      cp "$env_file" "$tmp_file"
    fi
    echo "IMAGE_TAG=${tag}" >> "$tmp_file"
  fi

  mv -f "$tmp_file" "$env_file"
}

rollback() {
  echo "deploy.sh: smoke test failed — rolling back to '${ENV_PREVIOUS_FILE}'." >&2
  if [[ ! -f "$ENV_PREVIOUS_FILE" ]]; then
    echo "deploy.sh: no '${ENV_PREVIOUS_FILE}' to roll back to — cannot roll back automatically." >&2
    return 1
  fi
  cp -f "$ENV_PREVIOUS_FILE" "$ENV_FILE"
  compose up -d
  echo "deploy.sh: rollback to previous .env completed. This deploy attempt is still a FAILURE and needs investigation." >&2
  return 0
}

main() {
  local image_tag="${1:-${IMAGE_TAG:-}}"
  if [[ -z "$image_tag" ]]; then
    echo "deploy.sh: usage: deploy.sh <IMAGE_TAG> (or set IMAGE_TAG env var)" >&2
    exit 1
  fi

  # 1. Snapshot current .env as the rollback basis, BEFORE any rewrite.
  if [[ -f "$ENV_FILE" ]]; then
    cp -f "$ENV_FILE" "$ENV_PREVIOUS_FILE"
  else
    echo "deploy.sh: warning: no existing '${ENV_FILE}' found — first deploy on this instance, rollback will be unavailable until a successful deploy completes." >&2
  fi

  # 2. Refresh secrets from SSM (may update SSM-sourced keys only).
  "$RENDER_ENV_SCRIPT"

  # 3. Set the new image tag.
  set_image_tag "$ENV_FILE" "$image_tag"

  # 4. Pull the new image.
  compose pull

  # 5. Run pending migrations using the NEW image, before bringing up the
  #    full stack.
  compose run --rm api alembic upgrade head

  # 6. Bring the full stack up.
  compose up -d

  # 7. Smoke test; roll back and fail loudly on failure.
  if ! "$SMOKE_TEST_SCRIPT"; then
    rollback
    exit 1
  fi

  echo "deploy.sh: deploy of IMAGE_TAG=${image_tag} succeeded."
}

main "$@"
