#!/usr/bin/env bash
#
# render_env_from_ssm.sh — PR12's CD pipeline. Runs ON THE INSTANCE, called
# by `deploy.sh` before every deploy to refresh secrets in case any SSM
# parameter changed since the last deploy.
#
# Fetches every SecureString parameter under the application's SSM path
# prefix (`/helpdesk/prod/` by default — PR10's `infra/terraform/ssm.tf`
# currently defines 6: DB_PASSWORD, JWT_SECRET, OPENAI_API_KEY,
# SECRET_ENCRYPTION_KEY, WHATSAPP_VERIFY_TOKEN, MAILGUN_SIGNING_KEY) and
# merges them into the instance's `.env` file, OVERWRITING only those
# SSM-sourced keys. Every other key already present in `.env`
# (DOMAIN, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD,
# GRAFANA_ADMIN_PASSWORD, IMAGE_TAG, ...) is preserved untouched — those are
# operator/deploy-set values that do not live in SSM.
#
# Security invariants (load-bearing — do not relax these):
#   1. Secret VALUES are never echoed, printed, or logged anywhere,
#      including in error messages. `set -x` / command tracing is never
#      enabled in this script.
#   2. The new `.env` content is rendered into a temp file that is
#      `chmod 600` BEFORE any secret content is written to it, so there is
#      never a moment where a world-readable file on disk contains secrets.
#   3. On any SSM fetch failure, the script aborts (`set -e`) WITHOUT ever
#      touching the existing working `.env` file — the temp file is
#      discarded and the real `.env` is left exactly as it was.
#   4. The temp file is atomically `mv`'d into place only after a fully
#      successful fetch + render, so a reader never observes a partially
#      written `.env`.
#
# Env vars (override for testing):
#   SSM_PARAMETER_PATH — SSM path prefix to fetch (default /helpdesk/prod/)
#   ENV_FILE            — target env file to render/merge into (default .env)

set -euo pipefail

SSM_PARAMETER_PATH="${SSM_PARAMETER_PATH:-/helpdesk/prod/}"
ENV_FILE="${ENV_FILE:-.env}"

main() {
  local tmp_file
  tmp_file="$(mktemp "${ENV_FILE}.XXXXXX")"

  # chmod 600 immediately, before any content (secret or otherwise) is
  # written — never depend on umask alone for this.
  chmod 600 "$tmp_file"

  # 6 parameters today, well under any single-page limit — a single
  # `get-parameters-by-path` call is sufficient (no pagination needed). If
  # the parameter count ever grows past a page, this call must be replaced
  # with a paginated loop that keeps merging `NextToken` pages before any
  # temp-file content is written.
  local raw_json
  if ! raw_json="$(aws ssm get-parameters-by-path \
        --path "$SSM_PARAMETER_PATH" \
        --with-decryption \
        --recursive \
        --output json)"; then
    rm -f "$tmp_file"
    echo "render_env_from_ssm.sh: failed to fetch parameters from SSM under '${SSM_PARAMETER_PATH}'; existing '${ENV_FILE}' left untouched." >&2
    exit 1
  fi

  # Render+merge happens entirely inside python (never through a shell
  # variable that could be echoed/traced) — parameter values only ever
  # flow: aws stdout -> raw_json var -> python stdin -> temp file.
  #
  # NOTE: the render logic is written to its own temp script file (rather
  # than piped via `python3 - <<EOF`) because `python3 -` consumes ALL of
  # stdin as the program source, leaving nothing for `json.load(sys.stdin)`
  # to read — the JSON payload needs a stdin of its own, supplied via a
  # here-string on the actual python invocation below.
  local render_script
  render_script="$(mktemp)"

  cat > "$render_script" <<'PYEOF'
import json
import os
import sys

tmp_file, env_file = sys.argv[1], sys.argv[2]

data = json.load(sys.stdin)
params = {}
for p in data.get("Parameters", []):
    key = p["Name"].rstrip("/").rsplit("/", 1)[-1]
    params[key] = p["Value"]

existing = {}
order = []
if os.path.exists(env_file):
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            existing[key] = value
            order.append(key)

merged = dict(existing)
merged.update(params)

lines = [f"{key}={merged[key]}" for key in order]
lines.extend(f"{key}={merged[key]}" for key in params if key not in existing)

with open(tmp_file, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
PYEOF

  if ! python3 "$render_script" "$tmp_file" "$ENV_FILE" <<<"$raw_json"; then
    rm -f "$render_script" "$tmp_file"
    echo "render_env_from_ssm.sh: failed to render '${ENV_FILE}' from fetched parameters; existing '${ENV_FILE}' left untouched." >&2
    exit 1
  fi
  rm -f "$render_script"

  mv -f "$tmp_file" "$ENV_FILE"
}

main "$@"
