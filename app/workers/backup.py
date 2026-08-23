"""Nightly database backup job (stage 8 — devops, PR8).

`backup_database_to_s3` runs nightly at 03:00 UTC (see
`app.workers.settings.WorkerSettings.cron_jobs`), `pg_dump`s the
application database, gzip-compresses the output, and uploads it to the
S3 bucket configured via `settings.backup_s3_bucket`
(`pg_dump/<database>-<timestamp>.sql.gz`). When `backup_s3_bucket` is
unset (e.g. local dev), the job logs and returns without raising — not
every environment needs backups configured.

Retention (14 most recent daily backups) is enforced by an S3 lifecycle
rule on the `pg_dump/` key prefix (Terraform, PR9/10) — NOT application
code; this job only uploads.

Threat-matrix requirement: `pg_dump` is invoked via
`asyncio.create_subprocess_exec` with an explicit argv list — never
`asyncio.create_subprocess_shell` and never a formatted/f-string command
string. The DSN/credentials are passed via the standard libpq environment
variables (`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE`) rather
than as command-line arguments, so they never appear in `ps` output and a
database name or password containing shell metacharacters is passed
through literally — there is no shell in this execution path to
interpret them.

A non-zero `pg_dump` exit code raises `RuntimeError`, so arq's own retry
mechanism (`WorkerSettings.max_tries = 3`) retries the job rather than
this job swallowing the failure.

`boto3` is synchronous; its blocking, potentially multi-MB `put_object`
call runs via `asyncio.to_thread` so it never blocks the arq worker's
event loop. `boto3.client("s3")` uses boto3's DEFAULT credential chain
only (instance IAM role via IMDSv2 in production) — no explicit
`aws_access_key_id`/`aws_secret_access_key` is ever passed.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
from datetime import UTC, datetime
from typing import Any

import boto3
from sqlalchemy.engine import make_url

from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def backup_database_to_s3(ctx: dict[str, Any]) -> None:
    """`pg_dump` the application database, gzip it, and upload it to
    `settings.backup_s3_bucket` under the `pg_dump/` key prefix. No-ops
    (logs and returns) when `backup_s3_bucket` is unset."""
    settings = get_settings()
    if not settings.backup_s3_bucket:
        logger.info(
            "backup_s3_bucket is not configured; skipping the nightly database backup."
        )
        return

    url = make_url(settings.database_url)
    env = {
        "PGHOST": url.host or "localhost",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "",
        "PGPASSWORD": url.password or "",
        "PGDATABASE": url.database or "",
    }

    argv = ["pg_dump", "--no-password"]

    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"pg_dump exited with code {proc.returncode}: "
            f"{stderr.decode(errors='replace')}"
        )

    compressed = gzip.compress(stdout)

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    key = f"pg_dump/{url.database}-{timestamp}.sql.gz"

    await asyncio.to_thread(_upload_to_s3, settings.backup_s3_bucket, key, compressed)
    logger.info("Uploaded database backup to s3://%s/%s", settings.backup_s3_bucket, key)


def _upload_to_s3(bucket: str, key: str, body: bytes) -> None:
    """Blocking `boto3` upload; always called via `asyncio.to_thread` so
    it never blocks the arq worker's event loop. Uses boto3's DEFAULT
    credential chain (no explicit access key/secret passed here)."""
    client = boto3.client("s3")
    client.put_object(Bucket=bucket, Key=key, Body=body)
