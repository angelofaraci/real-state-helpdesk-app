"""Tests for `scripts/deploy.sh` (PR12 — CD pipeline main sequence).

Runs the real script against fakes for every external dependency:
  - `docker` — a fake that appends every invocation to a log file so
    ordering (pull -> migrate -> up) can be asserted, and that can be made
    to fail (`FAKE_DOCKER_FAIL_ON`) to exercise error paths.
  - `render_env_from_ssm.sh` / `smoke_test.sh` — swapped for tiny fakes via
    `RENDER_ENV_SCRIPT` / `SMOKE_TEST_SCRIPT` env var overrides that
    `deploy.sh` itself supports, so this test never depends on live AWS or
    HTTP.

No real `docker compose` or AWS call is ever made.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_fake_docker(bin_dir: Path, log_file: Path, *, fail_on: str | None = None) -> None:
    fail_clause = ""
    if fail_on:
        fail_clause = (
            f'if [[ "$*" == *"{fail_on}"* ]]; then echo "fake docker: forced failure" >&2; exit 1; fi\n'
        )
    _make_executable(
        bin_dir / "docker",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'echo "$*" >> "{log_file}"\n'
        f"{fail_clause}"
        "exit 0\n",
    )


def _write_fake_script(path: Path, *, exit_code: int = 0, message: str = "") -> None:
    _make_executable(
        path,
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'[[ -n "{message}" ]] && echo "{message}"\n'
        f"exit {exit_code}\n",
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


def _run_deploy(
    workdir: Path,
    bin_dir: Path,
    *,
    render_script: Path,
    smoke_script: Path,
    image_tag: str = "abc123",
    docker_log: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["RENDER_ENV_SCRIPT"] = str(render_script)
    env["SMOKE_TEST_SCRIPT"] = str(smoke_script)
    env["DOCKER_LOG"] = str(docker_log)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), image_tag],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


def test_snapshots_env_to_env_previous_before_rewriting(bin_dir: Path, workdir: Path, tmp_path: Path) -> None:
    (workdir / ".env").write_text("DOMAIN=example.com\nIMAGE_TAG=old-tag\n")
    docker_log = tmp_path / "docker.log"
    _write_fake_docker(bin_dir, docker_log)
    render_script = tmp_path / "fake_render.sh"
    _write_fake_script(render_script)
    smoke_script = tmp_path / "fake_smoke.sh"
    _write_fake_script(smoke_script)

    result = _run_deploy(
        workdir, bin_dir, render_script=render_script, smoke_script=smoke_script, docker_log=docker_log
    )

    assert result.returncode == 0, result.stderr
    assert (workdir / ".env.previous").read_text() == "DOMAIN=example.com\nIMAGE_TAG=old-tag\n"


def test_sets_new_image_tag_in_env(bin_dir: Path, workdir: Path, tmp_path: Path) -> None:
    (workdir / ".env").write_text("DOMAIN=example.com\nIMAGE_TAG=old-tag\n")
    docker_log = tmp_path / "docker.log"
    _write_fake_docker(bin_dir, docker_log)
    render_script = tmp_path / "fake_render.sh"
    _write_fake_script(render_script)
    smoke_script = tmp_path / "fake_smoke.sh"
    _write_fake_script(smoke_script)

    result = _run_deploy(
        workdir, bin_dir, render_script=render_script, smoke_script=smoke_script,
        image_tag="new-sha-123", docker_log=docker_log,
    )

    assert result.returncode == 0, result.stderr
    content = (workdir / ".env").read_text()
    assert "IMAGE_TAG=new-sha-123" in content
    assert "DOMAIN=example.com" in content


def test_runs_pull_then_migrate_then_up_in_order(bin_dir: Path, workdir: Path, tmp_path: Path) -> None:
    (workdir / ".env").write_text("IMAGE_TAG=old-tag\n")
    docker_log = tmp_path / "docker.log"
    _write_fake_docker(bin_dir, docker_log)
    render_script = tmp_path / "fake_render.sh"
    _write_fake_script(render_script)
    smoke_script = tmp_path / "fake_smoke.sh"
    _write_fake_script(smoke_script)

    result = _run_deploy(
        workdir, bin_dir, render_script=render_script, smoke_script=smoke_script, docker_log=docker_log
    )

    assert result.returncode == 0, result.stderr
    lines = docker_log.read_text().splitlines()
    pull_idx = next(i for i, line in enumerate(lines) if "pull" in line)
    migrate_idx = next(i for i, line in enumerate(lines) if "alembic upgrade head" in line)
    up_idx = next(i for i, line in enumerate(lines) if line.strip().endswith("up -d"))
    assert pull_idx < migrate_idx < up_idx
    assert any("run --rm api alembic upgrade head" in line for line in lines)


def test_smoke_test_failure_triggers_rollback_and_nonzero_exit(bin_dir: Path, workdir: Path, tmp_path: Path) -> None:
    (workdir / ".env").write_text("IMAGE_TAG=good-tag\nDOMAIN=example.com\n")
    docker_log = tmp_path / "docker.log"
    _write_fake_docker(bin_dir, docker_log)
    render_script = tmp_path / "fake_render.sh"
    _write_fake_script(render_script)
    smoke_script = tmp_path / "fake_smoke.sh"
    _write_fake_script(smoke_script, exit_code=1, message="smoke test failing")

    result = _run_deploy(
        workdir, bin_dir, render_script=render_script, smoke_script=smoke_script,
        image_tag="bad-tag", docker_log=docker_log,
    )

    # A rolled-back deploy is still a FAILED deploy attempt.
    assert result.returncode != 0
    # .env restored to the pre-deploy (previous) content.
    assert (workdir / ".env").read_text() == "IMAGE_TAG=good-tag\nDOMAIN=example.com\n"
    # `up -d` was called twice: once for the failed new deploy, once again
    # during rollback.
    up_calls = [line for line in docker_log.read_text().splitlines() if line.strip().endswith("up -d")]
    assert len(up_calls) == 2


def test_migration_failure_aborts_before_up(bin_dir: Path, workdir: Path, tmp_path: Path) -> None:
    (workdir / ".env").write_text("IMAGE_TAG=old-tag\n")
    docker_log = tmp_path / "docker.log"
    _write_fake_docker(bin_dir, docker_log, fail_on="alembic upgrade head")
    render_script = tmp_path / "fake_render.sh"
    _write_fake_script(render_script)
    smoke_script = tmp_path / "fake_smoke.sh"
    _write_fake_script(smoke_script)

    result = _run_deploy(
        workdir, bin_dir, render_script=render_script, smoke_script=smoke_script, docker_log=docker_log
    )

    assert result.returncode != 0
    lines = docker_log.read_text().splitlines()
    assert not any(line.strip().endswith("up -d") for line in lines)
