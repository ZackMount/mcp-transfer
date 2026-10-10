#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PURGE=0; YES=0; KEEP_SOURCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir) ROOT_DIR="${2:?}"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    --yes) YES=1; shift ;;
    --keep-source) KEEP_SOURCE=1; shift ;;
    --help|-h) printf 'Usage: uninstall-cloud.sh [--install-dir /opt/mcp-transfer] [--purge --yes] [--keep-source]\nWithout --purge, stop services and retain data. Purge permanently deletes this platform database, users, device credentials, keys and runtime.\n'; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ "$(uname -s)" != Linux || $EUID -ne 0 ]]; then printf 'Run as root on Linux.\n' >&2; exit 1; fi
if [[ ! -d "$ROOT_DIR" || -L "$ROOT_DIR" ]]; then printf 'Installation directory missing or is a symlink.\n' >&2; exit 2; fi
ROOT_DIR="$(cd "$ROOT_DIR" && pwd -P)"
case "$ROOT_DIR" in /|/opt|/usr|/usr/local|/root|/home|/etc|/var|/tmp) printf 'Unsafe installation directory.\n' >&2; exit 2 ;; esac
if [[ ! -f "$ROOT_DIR/src/mcp_transfer/__init__.py" || ! -f "$ROOT_DIR/pyproject.toml" ]] || ! grep -Fqx 'name = "mcp-transfer"' "$ROOT_DIR/pyproject.toml"; then
  printf 'Not an MCP Transfer installation. Refusing cleanup.\n' >&2; exit 2
fi
if [[ "$PURGE" == 1 && "$YES" != 1 ]]; then printf 'Purge permanently deletes all platform data. Add --yes when you intend this reset.\n' >&2; exit 2; fi
if [[ -f "$ROOT_DIR/.env" ]]; then
  if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    printf 'Docker/Compose must be available before cleanup so its database volumes can be removed safely. No files were deleted.\n' >&2; exit 2
  fi
fi
if [[ -f /etc/systemd/system/mcp-transfer-cloud.service ]]; then
  if ! grep -Fqx "WorkingDirectory=$ROOT_DIR" /etc/systemd/system/mcp-transfer-cloud.service; then
    printf 'The native unit belongs to a different installation. No cleanup performed.\n' >&2; exit 2
  fi
  systemctl disable --now mcp-transfer-cloud.service
  rm /etc/systemd/system/mcp-transfer-cloud.service
  systemctl daemon-reload
fi
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  for CID in $(docker ps -aq --filter label=com.docker.compose.project=mcp-transfer); do
    OWNER="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$CID")"
    [[ "$OWNER" == "$ROOT_DIR" ]] || { printf 'Docker deployment belongs to %s. Refusing cleanup from another directory.\n' "$OWNER" >&2; exit 2; }
  done
  if [[ -f "$ROOT_DIR/.env" ]]; then
    COMPOSE=(docker compose --project-directory "$ROOT_DIR" --env-file "$ROOT_DIR/.env" -f "$ROOT_DIR/compose.yaml")
    if [[ "$PURGE" == 1 ]]; then "${COMPOSE[@]}" down --volumes --remove-orphans; else "${COMPOSE[@]}" down --remove-orphans; fi
  fi
fi
if [[ "$PURGE" == 1 ]]; then
  printf 'Removing MCP Transfer data from %s...\n' "$ROOT_DIR"
  if [[ -f "$ROOT_DIR/.native-user-created" ]]; then userdel mcp-transfer; fi
  cd /tmp
  if [[ "$KEEP_SOURCE" == 1 ]]; then
    rm -rf -- "$ROOT_DIR/data" "$ROOT_DIR/runtime" "$ROOT_DIR/.cache" "$ROOT_DIR/.tools"
    rm -f -- "$ROOT_DIR/.env" "$ROOT_DIR/.env".backup-* "$ROOT_DIR/.native.env" "$ROOT_DIR/.deployment-mode" "$ROOT_DIR/.native-user-created"
  else rm -rf -- "$ROOT_DIR"; fi
  printf 'Platform data and runtime removed. Other system services, Docker Engine and global proxy settings were not modified.\n'
else printf 'Platform stopped. Installation files and all data preserved at %s.\n' "$ROOT_DIR"; fi
