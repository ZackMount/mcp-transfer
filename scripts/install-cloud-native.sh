#!/usr/bin/env bash
# Native Linux deployment: isolated Python, SQLite and a dedicated systemd unit.
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT_DIR"
if [[ "$(uname -s)" != Linux || $EUID -ne 0 ]]; then printf 'Run this installer as root on Linux.\n' >&2; exit 1; fi
if [[ "$ROOT_DIR" =~ [[:space:]\"\%] ]]; then printf 'Use an installation path without spaces, quotes or percent signs.\n' >&2; exit 2; fi
case "$ROOT_DIR" in /home/*|/root/*|/run/user/*) printf 'The isolated service cannot read home directories. Install under /opt or /srv.\n' >&2; exit 2 ;; esac
if ! command -v systemctl >/dev/null; then printf 'Native deployment requires systemd.\n' >&2; exit 1; fi
if [[ -f .deployment-mode && "$(cat .deployment-mode)" != native ]] || [[ -f .env && ! -f .native.env ]]; then
  printf 'Existing Docker deployment detected. Use --clean --native for a full reset, or retain Docker. No data was changed.\n' >&2; exit 2
fi
if [[ -f /etc/systemd/system/mcp-transfer-cloud.service ]] && ! grep -Fqx "WorkingDirectory=$ROOT_DIR" /etc/systemd/system/mcp-transfer-cloud.service; then
  printf 'Another native installation owns mcp-transfer-cloud.service. Upgrade its original directory.\n' >&2; exit 2
fi
DOWNLOAD_PROXY=""; PIP_INDEX_URL="https://pypi.org/simple"; PRINT_CONFIG=0
ARGS=("$@")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --download-proxy) DOWNLOAD_PROXY="${2:?}"; shift 2 ;;
    --pip-index-url) PIP_INDEX_URL="${2:?}"; shift 2 ;;
    --print-config) PRINT_CONFIG=1; shift ;;
    *) shift ;;
  esac
done
CONFIG="$(MCPT_INSTALL_ENV=.native.env bash scripts/install-cloud.sh "${ARGS[@]}" --print-config)"
PUBLIC_URL="$(printf '%s\n' "$CONFIG" | sed -n 's/^MCPT_PUBLIC_URL=//p')"
FRP_PORT="$(printf '%s\n' "$CONFIG" | sed -n 's/^MCPT_FRP_PORT=//p')"
ALLOW_HTTP="$(printf '%s\n' "$CONFIG" | sed -n 's/^MCPT_ALLOW_INSECURE_HTTP=//p')"
if [[ "$PRINT_CONFIG" == 1 ]]; then printf '%s\n' "$CONFIG"; exit 0; fi
# HTTP can run directly in Python. HTTPS uses the administrator's existing
# reverse proxy forwarding to loopback 17800 (see docs/deployment.md).
if [[ "$PUBLIC_URL" != http://* ]]; then
  printf 'Native automatic deployment currently requires an HTTP URL and --allow-insecure-http. For HTTPS, use Docker or an existing reverse proxy with the manual instructions.\n' >&2; exit 2
fi
PORT="${PUBLIC_URL##*:}"
if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then PORT=80; fi
if (( PORT < 1024 || FRP_PORT < 1024 )); then printf 'Native services use an unprivileged account. Choose public and frp ports from 1024 to 65535.\n' >&2; exit 2; fi
if ! command -v curl >/dev/null || ! command -v tar >/dev/null || ! command -v openssl >/dev/null; then
  printf 'curl, tar and openssl are required. Install them using your system package manager.\n' >&2; exit 1
fi
umask 077
mkdir -p .tools runtime/releases runtime/python .cache/uv data/cloud
chmod 755 "$ROOT_DIR" runtime runtime/releases runtime/python data
chmod 700 data/cloud
download() {
  if [[ -n "$DOWNLOAD_PROXY" ]]; then
    env HTTP_PROXY="$DOWNLOAD_PROXY" HTTPS_PROXY="$DOWNLOAD_PROXY" NO_PROXY=localhost,127.0.0.1,::1 http_proxy="$DOWNLOAD_PROXY" https_proxy="$DOWNLOAD_PROXY" no_proxy=localhost,127.0.0.1,::1 "$@"
  else "$@"; fi
}
UV="$ROOT_DIR/.tools/uv"
if [[ ! -x "$UV" ]]; then
  case "$(uname -m)" in x86_64) ARCH=x86_64 ;; aarch64|arm64) ARCH=aarch64 ;; *) printf 'Automatic Python installation supports x86_64/arm64 Linux.\n' >&2; exit 1 ;; esac
  UV_ARCHIVE="uv-$ARCH-unknown-linux-gnu.tar.gz"
  UV_URL="https://github.com/astral-sh/uv/releases/download/0.10.0"
  STAGING="$(mktemp -d)"
  trap 'rm -rf -- "$STAGING"' EXIT
  download curl -fsSL --retry 3 "$UV_URL/$UV_ARCHIVE" -o "$STAGING/uv.tar.gz"
  download curl -fsSL --retry 3 "$UV_URL/$UV_ARCHIVE.sha256" -o "$STAGING/uv.sha256"
  EXPECTED="$(awk '{print $1}' "$STAGING/uv.sha256")"
  [[ "$EXPECTED" =~ ^[a-f0-9]{64}$ ]] || { printf 'Invalid uv checksum.\n' >&2; exit 1; }
  ACTUAL="$(sha256sum "$STAGING/uv.tar.gz" | awk '{print $1}')"
  [[ "$ACTUAL" == "$EXPECTED" ]] || { printf 'uv checksum mismatch.\n' >&2; exit 1; }
  tar -xzf "$STAGING/uv.tar.gz" --strip-components=1 -C "$STAGING"
  install -m 755 "$STAGING/uv" "$UV"
fi
export UV_PYTHON_INSTALL_DIR="$ROOT_DIR/runtime/python" UV_CACHE_DIR="$ROOT_DIR/.cache/uv"
download "$UV" python install 3.12
RELEASE="$ROOT_DIR/runtime/releases/$(date -u +%Y%m%d%H%M%S)-$$"
download "$UV" venv --python 3.12 --managed-python "$RELEASE"
download "$UV" pip install --python "$RELEASE/bin/python" --index-url "$PIP_INDEX_URL" -c requirements.lock setuptools wheel
download "$UV" pip install --python "$RELEASE/bin/python" --index-url "$PIP_INDEX_URL" --no-build-isolation -c requirements.lock .
# The service account needs read/execute access to Python and installed code;
# credentials and databases stay in its separate mode-700 data directory.
chmod -R a+rX runtime
if ! id mcp-transfer >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin mcp-transfer
  touch .native-user-created
fi
chown -R mcp-transfer:mcp-transfer data/cloud
INIT_ARGS=(cloud-init --public-url "$PUBLIC_URL" --frp-port "$FRP_PORT" --allow-insecure-http)
for OPTION in "${ARGS[@]}"; do if [[ "$OPTION" == --migrate-origin ]]; then INIT_ARGS+=(--migrate-origin); fi; done
printf 'Preparing frps...\n'
download runuser -u mcp-transfer -- "$RELEASE/bin/python" -m mcp_transfer --home "$ROOT_DIR/data/cloud" install-frp --binary frps
OLD_RELEASE="$(readlink runtime/current 2>/dev/null || true)"
systemctl stop mcp-transfer-cloud.service 2>/dev/null || true
if ! runuser -u mcp-transfer -- "$RELEASE/bin/python" -m mcp_transfer --home "$ROOT_DIR/data/cloud" "${INIT_ARGS[@]}"; then
  if [[ -n "$OLD_RELEASE" ]]; then systemctl start mcp-transfer-cloud.service || true; fi
  exit 1
fi
ln -s "$RELEASE" runtime/current.new
mv -Tf runtime/current.new runtime/current
cat > /etc/systemd/system/mcp-transfer-cloud.service <<UNIT
[Unit]
Description=MCP Transfer Cloud
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=mcp-transfer
Group=mcp-transfer
WorkingDirectory=$ROOT_DIR
ExecStart=$ROOT_DIR/runtime/current/bin/python -m mcp_transfer --home $ROOT_DIR/data/cloud cloud --bind-host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$ROOT_DIR/data/cloud
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 /etc/systemd/system/mcp-transfer-cloud.service
printf '%s\n' "$CONFIG" > .native.env
printf 'native\n' > .deployment-mode
systemctl daemon-reload
systemctl enable --now mcp-transfer-cloud.service
if ! "$RELEASE/bin/python" - "$PUBLIC_URL" "$FRP_PORT" <<'PY'
import json, socket, sys, time, urllib.request
from urllib.parse import urlsplit
origin = urlsplit(sys.argv[1])
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for _ in range(45):
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{origin.port or 80}/healthz', headers={'Host': origin.netloc})
        with opener.open(request, timeout=2) as response:
            assert json.load(response)['role'] == 'cloud'
        with socket.create_connection(('127.0.0.1', int(sys.argv[2])), timeout=2):
            pass
        print('Cloud gateway and frps are ready.')
        break
    except (OSError, ValueError, AssertionError):
        time.sleep(1)
else:
    raise SystemExit('Startup check failed. Inspect: journalctl -u mcp-transfer-cloud -n 100')
PY
then
  printf 'New service did not become ready. Inspect journalctl; the previous Python runtime is preserved at %s.\n' "$OLD_RELEASE" >&2
  exit 1
fi
printf '\nNative installation ready: %s\nAllow inbound TCP %s and %s only.\nStart/restart: systemctl restart mcp-transfer-cloud\nLogs: journalctl -u mcp-transfer-cloud -f\n' "$PUBLIC_URL" "$PORT" "$FRP_PORT"
