"""Tests for `scripts/render_env_from_ssm.sh` (PR12 — CD pipeline).

No live AWS access: because this is a shell script (not Python), the
project's usual `unittest.mock`/`monkeypatch` approach for stubbing the AWS
SDK (see `test_worker_backup.py`) does not apply here — instead, a fake
`aws` executable is placed first on `PATH` so the script's
`aws ssm get-parameters-by-path ...` invocation is fully controlled by the
test (success returns canned JSON, failure exits non-zero).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "render_env_from_ssm.sh"

FAKE_PARAMS = [
    {"Name": "/helpdesk/prod/DB_PASSWORD", "Value": "super-secret-db-pw"},
    {"Name": "/helpdesk/prod/JWT_SECRET", "Value": "super-secret-jwt"},
    {"Name": "/helpdesk/prod/OPENAI_API_KEY", "Value": "sk-super-secret"},
]


def _write_fake_aws(bin_dir: Path, *, exit_code: int, stdout: str = "") -> None:
    """Write a fake `aws` CLI that only understands
    `ssm get-parameters-by-path` and returns canned output/exit code,
    regardless of arguments — enough to drive the script under test."""
    fake_aws = bin_dir / "aws"
    fake_aws.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"echo '{stdout}'\n"
        f"exit {exit_code}\n"
    )
    fake_aws.chmod(fake_aws.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_script(cwd: Path, bin_dir: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.pop("SSM_PARAMETER_PATH", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "fakebin"
    d.mkdir()
    return d


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "instance"
    d.mkdir()
    return d


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists()
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR


def test_sets_strict_bash_mode() -> None:
    content = SCRIPT.read_text()
    assert "set -euo pipefail" in content


def test_never_traces_or_echoes_secret_values() -> None:
    content = SCRIPT.read_text()
    # `set -x` must never appear as an executable statement anywhere in the
    # script (tracing would leak decrypted parameter values to logs) — only
    # match actual statements, not the phrase inside comments/docs.
    executable_lines = [
        line for line in content.splitlines() if not line.lstrip().startswith("#")
    ]
    assert not any("set -x" in line for line in executable_lines)
    # The script must never pipe fetched values through echo/cat/print.
    assert "echo \"$raw_json\"" not in content
    assert "cat \"$raw_json\"" not in content


def test_successful_fetch_writes_env_file_with_chmod_600(bin_dir: Path, workdir: Path) -> None:
    _write_fake_aws(bin_dir, exit_code=0, stdout=json.dumps({"Parameters": FAKE_PARAMS}))

    result = _run_script(workdir, bin_dir)

    assert result.returncode == 0, result.stderr
    env_file = workdir / ".env"
    assert env_file.exists()

    mode = stat.S_IMODE(env_file.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    content = env_file.read_text()
    assert "DB_PASSWORD=super-secret-db-pw" in content
    assert "JWT_SECRET=super-secret-jwt" in content
    assert "OPENAI_API_KEY=sk-super-secret" in content


def test_secret_values_never_appear_on_stdout_or_stderr(bin_dir: Path, workdir: Path) -> None:
    _write_fake_aws(bin_dir, exit_code=0, stdout=json.dumps({"Parameters": FAKE_PARAMS}))

    result = _run_script(workdir, bin_dir)

    assert result.returncode == 0, result.stderr
    for secret in ("super-secret-db-pw", "super-secret-jwt", "sk-super-secret"):
        assert secret not in result.stdout
        assert secret not in result.stderr


def test_merges_with_existing_env_preserving_non_ssm_keys(bin_dir: Path, workdir: Path) -> None:
    (workdir / ".env").write_text(
        "DOMAIN=example.com\nIMAGE_TAG=abc123\nDB_PASSWORD=old-stale-value\n"
    )
    _write_fake_aws(bin_dir, exit_code=0, stdout=json.dumps({"Parameters": FAKE_PARAMS}))

    result = _run_script(workdir, bin_dir)

    assert result.returncode == 0, result.stderr
    content = (workdir / ".env").read_text()
    # Operator-set keys not sourced from SSM survive untouched.
    assert "DOMAIN=example.com" in content
    assert "IMAGE_TAG=abc123" in content
    # SSM-sourced key is refreshed, not left at its stale value.
    assert "DB_PASSWORD=super-secret-db-pw" in content
    assert "DB_PASSWORD=old-stale-value" not in content


def test_aborts_and_leaves_existing_env_untouched_on_ssm_failure(bin_dir: Path, workdir: Path) -> None:
    original = "DOMAIN=example.com\nDB_PASSWORD=known-good-value\n"
    (workdir / ".env").write_text(original)
    _write_fake_aws(bin_dir, exit_code=1)

    result = _run_script(workdir, bin_dir)

    assert result.returncode != 0
    # Existing .env must be byte-for-byte untouched after a failed fetch.
    assert (workdir / ".env").read_text() == original
    # No leftover temp files from the aborted render.
    leftovers = [p for p in workdir.iterdir() if p.name.startswith(".env.") and p.name != ".env"]
    assert leftovers == []


def test_never_leaves_a_world_readable_temp_file_behind(bin_dir: Path, workdir: Path) -> None:
    # Even on success, no stray temp file (which would carry secrets)
    # should remain next to the final .env.
    _write_fake_aws(bin_dir, exit_code=0, stdout=json.dumps({"Parameters": FAKE_PARAMS}))

    result = _run_script(workdir, bin_dir)

    assert result.returncode == 0, result.stderr
    leftovers = [p for p in workdir.iterdir() if p.name != ".env"]
    assert leftovers == []


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_passes() -> None:
    result = subprocess.run(
        ["shellcheck", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
