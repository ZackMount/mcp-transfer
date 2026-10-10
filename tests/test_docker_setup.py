"""Dependency bootstrap regressions without changing the developer machine."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux dependency installer uses bash")
HELPER = Path(__file__).parents[1] / "scripts/ensure-docker.sh"

DOCKER = """#!/bin/bash
case "$*" in
  "compose version") test -f "$MCPT_MOCK_ROOT/compose_ready" || exit 1; echo 'Docker Compose version v2.40.3' ;;
  "buildx version") test -f "$MCPT_MOCK_ROOT/buildx_ready" || exit 1; echo 'github.com/docker/buildx v0.30.1' ;;
  "info") test -f "$MCPT_MOCK_ROOT/daemon_ready" || exit 1 ;;
  "--version") echo 'Docker version 29.1.3' ;;
  *) exit 1 ;;
esac
"""


def setup(
    tmp_path,
    *,
    engine=True,
    compose=False,
    buildx=False,
    daemon=True,
    provider="ubuntu",
    version="22.04",
    os_id="ubuntu",
    apt_fail=False,
):
    commands = tmp_path / "bin"
    commands.mkdir()
    for command in ("bash", "cp", "chmod", "touch"):
        (commands / command).symlink_to(shutil.which(command))
    (tmp_path / "docker-template").write_text(DOCKER)
    (tmp_path / "os-release").write_text(f'ID={os_id}\nVERSION_ID="{version}"\n')
    for field, enabled in [("compose", compose), ("buildx", buildx), ("daemon", daemon)]:
        if enabled:
            (tmp_path / (field + "_ready")).touch()
    scripts = {
        "apt-get": """#!/bin/bash
echo "apt-get $*" >> "$MCPT_MOCK_ROOT/commands.log"
if [[ "$1" == update ]]; then exit 0; fi
if [[ "$MCPT_MOCK_APT_FAIL" == 1 ]]; then exit 100; fi
for item in "$@"; do
  case "$item" in
    docker.io) cp "$MCPT_MOCK_ROOT/docker-template" "$MCPT_MOCK_ROOT/bin/docker"; chmod +x "$MCPT_MOCK_ROOT/bin/docker" ;;
    docker-compose-v2|docker-compose-plugin) touch "$MCPT_MOCK_ROOT/compose_ready" ;;
    docker-buildx|docker-buildx-plugin) touch "$MCPT_MOCK_ROOT/buildx_ready" ;;
  esac
done
""",
        "dpkg-query": """#!/bin/bash
if [[ "$MCPT_MOCK_PROVIDER" == ce ]]; then printf installed; else exit 1; fi
""",
        "systemctl": """#!/bin/bash
echo "systemctl $*" >> "$MCPT_MOCK_ROOT/commands.log"
touch "$MCPT_MOCK_ROOT/daemon_ready"
""",
    }
    if engine:
        scripts["docker"] = DOCKER
    for name, source in scripts.items():
        path = commands / name
        path.write_text(source)
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(commands),
        "MCPT_MOCK_ROOT": str(tmp_path),
        "MCPT_MOCK_PROVIDER": provider,
        "MCPT_MOCK_APT_FAIL": "1" if apt_fail else "0",
    }
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; . "$1"; ensure_docker "$2"',
            "test",
            str(HELPER),
            str(tmp_path / "os-release"),
        ],
        env=env,
        text=True,
        capture_output=True,
    )
    log = (tmp_path / "commands.log").read_text() if (tmp_path / "commands.log").exists() else ""
    return result, log


def test_ubuntu_2204_adds_compose_without_reinstalling_engine(tmp_path):
    result, log = setup(tmp_path, buildx=True)
    assert result.returncode == 0, result.stderr
    assert "docker-compose-v2" in log
    assert "docker.io" not in log and "docker-ce" not in log
    assert "systemctl" not in log


def test_ubuntu_existing_engine_adds_both_missing_plugins(tmp_path):
    result, log = setup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "docker-compose-v2" in log and "docker-buildx" in log
    assert "docker.io" not in log


def test_official_docker_installation_uses_its_plugin_packages(tmp_path):
    result, log = setup(tmp_path, provider="ce")
    assert result.returncode == 0, result.stderr
    assert "docker-compose-plugin" in log and "docker-buildx-plugin" in log
    assert "docker-compose-v2" not in log and "docker.io" not in log


@pytest.mark.parametrize("version", ["22.04", "24.04"])
def test_fresh_supported_ubuntu_installs_engine_and_starts_daemon(tmp_path, version):
    result, log = setup(tmp_path, engine=False, daemon=False, version=version)
    assert result.returncode == 0, result.stderr
    assert "docker.io" in log
    assert "systemctl enable --now docker" in log


def test_ready_installation_does_not_touch_packages_or_daemon(tmp_path):
    result, log = setup(tmp_path, compose=True, buildx=True)
    assert result.returncode == 0, result.stderr
    assert log == ""


def test_stopped_daemon_is_started_without_reinstalling_dependencies(tmp_path):
    result, log = setup(tmp_path, compose=True, buildx=True, daemon=False)
    assert result.returncode == 0, result.stderr
    assert "apt-get" not in log
    assert "systemctl enable --now docker" in log


def test_package_failure_stops_before_claiming_readiness(tmp_path):
    result, log = setup(tmp_path, apt_fail=True)
    assert result.returncode != 0
    assert "Dependency installation failed" in result.stderr
    assert "systemctl" not in log


def test_unsupported_distribution_is_not_silently_modified(tmp_path):
    result, log = setup(tmp_path, os_id="debian", version="12")
    assert result.returncode != 0
    assert "Ubuntu 22.04/24.04" in result.stderr
    assert log == ""
