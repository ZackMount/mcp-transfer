#!/usr/bin/env bash
# Public download bootstrap. No GitHub account or Git is needed for a new install.
set -euo pipefail
INSTALL_DIR=""; PREPARE_ONLY=0; INSTALL_ARGS=(); DOWNLOAD_PROXY=""; NATIVE=0; CLEAN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir) INSTALL_DIR="${2:?--install-dir needs a directory}"; shift 2 ;;
    --prepare-only) PREPARE_ONLY=1; shift ;;
    --native) NATIVE=1; shift ;;
    --clean) CLEAN=1; shift ;;
    --download-proxy) DOWNLOAD_PROXY="${2:?--download-proxy needs a URL}"; INSTALL_ARGS+=("--download-proxy" "$DOWNLOAD_PROXY"); shift 2 ;;
    *) INSTALL_ARGS+=("$1"); shift ;;
  esac
done
if [[ -n "$DOWNLOAD_PROXY" && ! "$DOWNLOAD_PROXY" =~ ^https?://[^[:space:]]+$ ]]; then
  printf 'Download proxy must be an HTTP(S) URL without whitespace.\n' >&2; exit 2
fi
if [[ "$PREPARE_ONLY" != 1 && ( "$(uname -s)" != Linux || $EUID -ne 0 ) ]]; then
  printf 'Run on Linux through sudo bash.\n' >&2; exit 1
fi
if ! command -v curl >/dev/null || ! command -v tar >/dev/null; then
  if [[ $EUID == 0 ]] && command -v apt-get >/dev/null; then
    apt-get update && apt-get install -y curl tar ca-certificates
  else
    printf 'curl and tar are required to download the public source archive.\n' >&2; exit 1
  fi
fi

# Reuse the original Compose directory when upgrading: the database volume alone
# is insufficient without the existing CA and signing keys in data/cloud.
EXISTING_DIR=""
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  CONTAINERS="$(docker ps -aq --filter label=com.docker.compose.project=mcp-transfer)"
  for CONTAINER_ID in $CONTAINERS; do
    FOUND="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$CONTAINER_ID")"
    if [[ -n "$EXISTING_DIR" && "$FOUND" != "$EXISTING_DIR" ]]; then
      printf 'Multiple existing deployment directories found. Resolve this before upgrading.\n' >&2; exit 1
    fi
    EXISTING_DIR="$FOUND"
  done
fi
if [[ -n "$EXISTING_DIR" ]]; then
  if [[ -n "$INSTALL_DIR" && "$(cd "$INSTALL_DIR" 2>/dev/null && pwd -P)" != "$EXISTING_DIR" ]]; then
    printf 'Existing deployment uses %s. Use that directory to preserve keys and data.\n' "$EXISTING_DIR" >&2; exit 1
  fi
  INSTALL_DIR="$EXISTING_DIR"
fi
if [[ -z "$INSTALL_DIR" && -f .env && -f data/cloud/settings.json ]]; then INSTALL_DIR="$(pwd -P)"; fi
INSTALL_DIR="${INSTALL_DIR:-/opt/mcp-transfer}"
if [[ -d "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR")" && ! -f "$INSTALL_DIR/src/mcp_transfer/__init__.py" ]]; then
  printf 'Selected directory is nonempty and is not an MCP Transfer source directory. Refusing to overwrite it.\n' >&2; exit 1
fi
if [[ "$CLEAN" != 1 ]] && command -v docker >/dev/null && docker volume inspect mcp-transfer_postgres_data >/dev/null 2>&1; then
  if [[ ! -f "$INSTALL_DIR/data/cloud/settings.json" || ! -f "$INSTALL_DIR/data/cloud/pki/ca.key" || ! -f "$INSTALL_DIR/data/cloud/signing.key" ]]; then
    printf 'An existing database volume was found, but the selected directory lacks its identity keys. Specify --install-dir with the original deployment directory.\n' >&2; exit 1
  fi
fi
mkdir -p "$INSTALL_DIR"
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  # Existing Git deployments keep their history and local edits. Fetch publicly
  # without needing a configured GitHub account or changing the saved remote.
  command -v git >/dev/null || { printf 'Existing Git checkout needs Git to update safely.\n' >&2; exit 1; }
  GIT_RUN=(git -c "safe.directory=$INSTALL_DIR" -c core.hooksPath=/dev/null -C "$INSTALL_DIR")
  if [[ $EUID == 0 && "$(uname -s)" == Linux ]]; then
    REPO_OWNER="$(stat -c %U "$INSTALL_DIR/.git")"
    if [[ "$REPO_OWNER" != root && "$REPO_OWNER" != UNKNOWN ]]; then
      if command -v runuser >/dev/null; then
        GIT_RUN=(runuser -u "$REPO_OWNER" -- "${GIT_RUN[@]}")
      else
        printf 'Update this existing checkout as its owner (%s), then run its installer.\n' "$REPO_OWNER" >&2; exit 1
      fi
    fi
  fi
  if [[ -n "$DOWNLOAD_PROXY" ]]; then GIT_RUN+=(-c "http.proxy=$DOWNLOAD_PROXY"); fi
  "${GIT_RUN[@]}" -c credential.helper= fetch https://github.com/ZackMount/mcp-transfer.git main
  "${GIT_RUN[@]}" merge --ff-only FETCH_HEAD
else
  STAGING="$(mktemp -d)"
  trap 'rm -rf -- "$STAGING"' EXIT
  CURL_ARGS=(--fail --show-error --silent --location --retry 3)
  if [[ -n "$DOWNLOAD_PROXY" ]]; then CURL_ARGS+=(--proxy "$DOWNLOAD_PROXY"); fi
  curl "${CURL_ARGS[@]}" https://codeload.github.com/ZackMount/mcp-transfer/tar.gz/refs/heads/main -o "$STAGING/source.tar.gz"
  mkdir "$STAGING/source"
  tar -xzf "$STAGING/source.tar.gz" --strip-components=1 -C "$STAGING/source"
  [[ -f "$STAGING/source/pyproject.toml" && -f "$STAGING/source/scripts/install-cloud.sh" ]] || { printf 'Invalid source archive.\n' >&2; exit 1; }
  cp -a "$STAGING/source/." "$INSTALL_DIR/"
fi
printf 'Source ready: %s\n' "$INSTALL_DIR"
if [[ "$PREPARE_ONLY" == 1 ]]; then exit 0; fi
if [[ "$CLEAN" == 1 ]]; then
  # Retain the freshly downloaded source while deleting only deployment data.
  bash "$INSTALL_DIR/scripts/uninstall-cloud.sh" --install-dir "$INSTALL_DIR" --purge --keep-source --yes
fi
if [[ "$NATIVE" == 1 ]]; then
  bash "$INSTALL_DIR/scripts/install-cloud-native.sh" "${INSTALL_ARGS[@]}"
else
  bash "$INSTALL_DIR/scripts/install-cloud.sh" "${INSTALL_ARGS[@]}"
fi
