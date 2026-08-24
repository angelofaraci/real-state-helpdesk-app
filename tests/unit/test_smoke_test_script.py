"""Tests for `scripts/smoke_test.sh` (PR12 — CD pipeline).

A fake `curl` is placed first on `PATH` so HTTP responses are fully
controlled by the test — no live server needed. The fake reads its desired
behavior from files under a directory pointed to by `FAKE_CURL_STATE_DIR`,
keyed by request count, so tests can simulate "fails N times then
succeeds" without any real networking.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "smoke_test.sh"


def _write_fake_curl(bin_dir: Path, responses: dict[str, list[str]]) -> None:
    """Fake `curl -s -o /dev/null -w '%{http_code}' <url>` that pops the
    next queued status code for the requested path (looked up by URL
    suffix) from `responses`, or replays the last one once the queue is
    exhausted."""
    fake_curl = bin_dir / "curl"
    # Encode the responses as a small inline python dispatcher so the test
    # doesn't need a real HTTP server or state file.
    body = "\n".join(
        f'  "{path}") codes=({" ".join(codes)});;' for path, codes in responses.items()
    )
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'url="${@: -1}"\n'
        'state_file="${FAKE_CURL_STATE_DIR}/$(echo "$url" | tr -c "[:alnum:]" _)"\n'
        'count=0\n'
        'if [[ -f "$state_file" ]]; then count=$(cat "$state_file"); fi\n'
        'count=$((count + 1))\n'
        'echo "$count" > "$state_file"\n'
        'case "$url" in\n'
        f"{body}\n"
        '  *) codes=(000);;\n'
        "esac\n"
        'idx=$((count - 1))\n'
        'if (( idx >= ${#codes[@]} )); then idx=$((${#codes[@]} - 1)); fi\n'
        'printf "%s" "${codes[$idx]}"\n'
    )
    fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_script(bin_dir: Path, state_dir: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_CURL_STATE_DIR"] = str(state_dir)
    env["SMOKE_TEST_RETRY_INTERVAL_SECONDS"] = "0"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)],
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
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "curlstate"
    d.mkdir()
    return d


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


def test_exits_zero_when_both_endpoints_return_200_immediately(bin_dir: Path, state_dir: Path) -> None:
    _write_fake_curl(
        bin_dir,
        {
            "http://localhost:8000/health": ["200"],
            "http://localhost:8000/ready": ["200"],
        },
    )

    result = _run_script(bin_dir, state_dir)

    assert result.returncode == 0, result.stderr


def test_retries_before_succeeding(bin_dir: Path, state_dir: Path) -> None:
    _write_fake_curl(
        bin_dir,
        {
            "http://localhost:8000/health": ["503", "503", "200"],
            "http://localhost:8000/ready": ["200"],
        },
    )

    result = _run_script(bin_dir, state_dir)

    assert result.returncode == 0, result.stderr
    assert "attempt 3/10" in result.stdout


def test_exits_nonzero_when_endpoint_never_returns_200(bin_dir: Path, state_dir: Path) -> None:
    _write_fake_curl(
        bin_dir,
        {
            "http://localhost:8000/health": ["503"],
        },
    )

    result = _run_script(bin_dir, state_dir, extra_env={"SMOKE_TEST_MAX_ATTEMPTS": "3"})

    assert result.returncode != 0
    assert "/health" in result.stderr


def test_checks_health_before_ready(bin_dir: Path, state_dir: Path) -> None:
    _write_fake_curl(
        bin_dir,
        {
            "http://localhost:8000/health": ["503"],
            "http://localhost:8000/ready": ["200"],
        },
    )

    result = _run_script(bin_dir, state_dir, extra_env={"SMOKE_TEST_MAX_ATTEMPTS": "1"})

    # /health never succeeds -> script must fail before ever checking
    # /ready.
    assert result.returncode != 0
    assert not (state_dir / "http___localhost_8000_ready").exists()
