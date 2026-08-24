#!/usr/bin/env bash
#
# smoke_test.sh — PR12's CD pipeline. Runs ON THE INSTANCE, called by
# `deploy.sh` immediately after `docker compose ... up -d`.
#
# Hits the API directly on `http://localhost:8000` (not through Caddy on
# https://localhost) for both endpoints below. This is a deliberate choice:
# going through Caddy would add TLS/cert-issuance timing into the smoke
# test's critical path (Caddy's automatic HTTPS cert issuance is not
# guaranteed to be instant on every deploy), which is unrelated to whether
# the newly deployed API container itself is healthy. Hitting the api
# service directly on its published/internal port keeps this check focused
# on exactly what it needs to verify and avoids that TLS complexity.
#
# Exits 0 only if BOTH `/health` and `/ready` return HTTP 200 within the
# retry budget below. Exits non-zero otherwise (deploy.sh treats that as a
# failed deploy and triggers rollback).

set -euo pipefail

BASE_URL="${SMOKE_TEST_BASE_URL:-http://localhost:8000}"
MAX_ATTEMPTS="${SMOKE_TEST_MAX_ATTEMPTS:-10}"

# Fixed 2s interval between attempts — simple and sufficient for this check
# (not a fancy exponential backoff; up to 10 attempts x 2s = 20s budget,
# comfortably longer than the container healthcheck's own start_period).
RETRY_INTERVAL_SECONDS="${SMOKE_TEST_RETRY_INTERVAL_SECONDS:-2}"

# check_endpoint <path>
# Retries GET <BASE_URL><path> up to MAX_ATTEMPTS times, sleeping
# RETRY_INTERVAL_SECONDS between attempts, until it returns HTTP 200.
check_endpoint() {
  local path="$1"
  local attempt
  local status

  for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
    status="$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}${path}" || true)"
    if [[ "$status" == "200" ]]; then
      echo "smoke_test.sh: ${path} returned 200 (attempt ${attempt}/${MAX_ATTEMPTS})"
      return 0
    fi
    echo "smoke_test.sh: ${path} returned '${status}' (attempt ${attempt}/${MAX_ATTEMPTS}), retrying in ${RETRY_INTERVAL_SECONDS}s..." >&2
    sleep "$RETRY_INTERVAL_SECONDS"
  done

  echo "smoke_test.sh: ${path} did not return 200 within ${MAX_ATTEMPTS} attempts." >&2
  return 1
}

main() {
  check_endpoint "/health"
  check_endpoint "/ready"
}

main "$@"
