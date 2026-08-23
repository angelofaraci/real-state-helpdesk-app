"""Unit tests for the nightly database backup job (stage 8 — devops, PR8):
`app.workers.backup.backup_database_to_s3`.

Threat-matrix requirement under test: `pg_dump` MUST be invoked via
`asyncio.create_subprocess_exec` with an explicit argv list — never
`asyncio.create_subprocess_shell` and never a formatted/f-string command
string — with the DSN/credentials passed via the standard libpq
environment variables (`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/
`PGDATABASE`) rather than as command-line arguments, so a database name or
password containing shell metacharacters is passed through *literally* and
is never interpreted by a shell (there is no shell in this execution
path).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest

from app.workers import backup as backup_module


def _fake_settings(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        backup_s3_bucket="helpdesk-backups",
        database_url="postgresql+asyncpg://helpdesk:helpdesk@localhost:5432/helpdesk",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeProcess:
    def __init__(self, returncode: int, stdout: bytes = b"dump-bytes", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_pg_dump_invoked_via_argv_list_with_malicious_credentials_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malicious_password = "p@ss;rm -rf /$(whoami)"
    malicious_db = "help;rm -rf /desk"
    database_url = (
        f"postgresql+asyncpg://ro$bin:{quote(malicious_password, safe='')}"
        f"@dbhost:5432/{malicious_db}"
    )
    settings = _fake_settings(database_url=database_url)
    monkeypatch.setattr(backup_module, "get_settings", lambda: settings)

    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(
        backup_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(backup_module, "_upload_to_s3", MagicMock())
    monkeypatch.setattr(backup_module.asyncio, "to_thread", AsyncMock())

    await backup_module.backup_database_to_s3({})

    argv = captured["args"]
    # Every argv element is a plain string, never a shell command string
    # and `shell=True`/`create_subprocess_shell` is never used.
    assert all(isinstance(part, str) for part in argv)
    assert not any(";" in part or "rm -rf" in part for part in argv)
    assert "shell" not in captured["kwargs"]

    env = captured["kwargs"]["env"]
    # The malicious db name/password are passed through LITERALLY via
    # env vars — inert because there is no shell to interpret them.
    assert env["PGPASSWORD"] == malicious_password
    assert env["PGDATABASE"] == malicious_db
    assert env["PGHOST"] == "dbhost"
    assert env["PGUSER"] == "ro$bin"


@pytest.mark.asyncio
async def test_no_op_when_backup_bucket_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _fake_settings(backup_s3_bucket=None)
    monkeypatch.setattr(backup_module, "get_settings", lambda: settings)

    create_subprocess_exec = AsyncMock()
    monkeypatch.setattr(backup_module.asyncio, "create_subprocess_exec", create_subprocess_exec)

    await backup_module.backup_database_to_s3({})

    create_subprocess_exec.assert_not_called()


@pytest.mark.asyncio
async def test_nonzero_pg_dump_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _fake_settings()
    monkeypatch.setattr(backup_module, "get_settings", lambda: settings)

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(returncode=1, stdout=b"", stderr=b"pg_dump: error: connection failed")

    monkeypatch.setattr(
        backup_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    upload_mock = MagicMock()
    monkeypatch.setattr(backup_module, "_upload_to_s3", upload_mock)
    monkeypatch.setattr(backup_module.asyncio, "to_thread", AsyncMock())

    with pytest.raises(RuntimeError, match="pg_dump exited with code 1"):
        await backup_module.backup_database_to_s3({})

    # Never upload a failed/partial dump.
    upload_mock.assert_not_called()


@pytest.mark.asyncio
async def test_uploads_gzip_compressed_dump_to_s3_with_pg_dump_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _fake_settings()
    monkeypatch.setattr(backup_module, "get_settings", lambda: settings)

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(returncode=0, stdout=b"raw-dump-bytes")

    monkeypatch.setattr(
        backup_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    captured: dict[str, Any] = {}

    async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        captured["fn"] = fn
        captured["args"] = args
        return fn(*args, **kwargs)

    upload_mock = MagicMock()
    monkeypatch.setattr(backup_module, "_upload_to_s3", upload_mock)
    monkeypatch.setattr(backup_module.asyncio, "to_thread", fake_to_thread)

    await backup_module.backup_database_to_s3({})

    upload_mock.assert_called_once()
    bucket, key, body = upload_mock.call_args.args
    assert bucket == "helpdesk-backups"
    assert key.startswith("pg_dump/helpdesk-")
    assert key.endswith(".sql.gz")
    import gzip

    assert gzip.decompress(body) == b"raw-dump-bytes"


def test_upload_to_s3_uses_default_credential_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = MagicMock()
    fake_boto3_client = MagicMock(return_value=fake_client)
    monkeypatch.setattr(backup_module.boto3, "client", fake_boto3_client)

    backup_module._upload_to_s3("my-bucket", "pg_dump/foo.sql.gz", b"body")

    fake_boto3_client.assert_called_once_with("s3")
    fake_client.put_object.assert_called_once_with(
        Bucket="my-bucket", Key="pg_dump/foo.sql.gz", Body=b"body"
    )
