#!/usr/bin/env bash
# Sourced by install-cloud.sh after its Linux/root checks.
ensure_docker() {
  local release_file="${1:-/etc/os-release}"
  local engine_missing=0 compose_missing=0 buildx_missing=0
  local ID="" VERSION_ID="" compose_package="docker-compose-v2" buildx_package="docker-buildx"
  local packages=(ca-certificates curl openssl)
  command -v docker >/dev/null 2>&1 || engine_missing=1
  docker compose version >/dev/null 2>&1 || compose_missing=1
  docker buildx version >/dev/null 2>&1 || buildx_missing=1

  if (( engine_missing || compose_missing || buildx_missing )); then
    if [[ -f "$release_file" ]]; then . "$release_file"; fi
    if [[ "$ID" != ubuntu || ( "$VERSION_ID" != 22.04 && "$VERSION_ID" != 24.04 ) ]]; then
      printf 'Install Docker Engine, Compose and Buildx plugins first. Ubuntu 22.04/24.04 are provisioned automatically.\n' >&2
      return 1
    fi
    # Match the installed CLI's package provider; do not switch an existing
    # Ubuntu Docker installation to Docker CE just to add a plugin.
    if [[ "$(dpkg-query -W -f='${db:Status-Status}' docker-ce-cli 2>/dev/null || true)" == installed ]]; then
      compose_package="docker-compose-plugin"
      buildx_package="docker-buildx-plugin"
    fi
    if (( engine_missing )); then packages+=(docker.io); fi
    if (( compose_missing )); then packages+=("$compose_package"); fi
    if (( buildx_missing )); then packages+=("$buildx_package"); fi
    apt-get update || return 1
    if ! apt-get install -y --no-install-recommends "${packages[@]}"; then
      printf 'Dependency installation failed. Ubuntu plugin packages require the matching updates/universe repositories; Docker CE plugins require its configured apt repository.\n' >&2
      return 1
    fi
  fi
  if ! docker compose version || ! docker buildx version; then
    printf 'Docker CLI plugins are still unavailable. Check the package installation above.\n' >&2
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    if ! command -v systemctl >/dev/null 2>&1 || ! systemctl enable --now docker; then
      printf 'Docker daemon is not ready. Check systemctl status docker.\n' >&2
      return 1
    fi
    if ! docker info >/dev/null 2>&1; then
      printf 'Docker daemon is still unreachable. Check systemctl status docker and the configured Docker context.\n' >&2
      return 1
    fi
  fi
}
