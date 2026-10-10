#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
DOMAIN=""; PUBLIC_URL=""; FRP_PORT=""; ALLOW_HTTP=""; MIGRATE=0; PRINT_CONFIG=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:?--domain needs a domain}"; shift 2 ;;
    --public-url) PUBLIC_URL="${2:?--public-url needs a URL}"; shift 2 ;;
    --frp-port) FRP_PORT="${2:?--frp-port needs a port}"; shift 2 ;;
    --allow-insecure-http) ALLOW_HTTP=1; shift ;;
    --migrate-origin) MIGRATE=1; shift ;;
    --print-config) PRINT_CONFIG=1; shift ;;
    --help|-h)
      printf 'Usage: sudo bash scripts/install-cloud.sh [--domain NAME | --public-url URL] [--frp-port PORT] [--allow-insecure-http] [--migrate-origin]\n'
      printf 'HTTP example: --public-url http://203.0.113.10:18080 --frp-port 17000 --allow-insecure-http\n'
      printf 'Changing an existing public URL requires --migrate-origin; update both agents afterwards.\n'
      exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
read_env() { if [[ -f .env ]]; then sed -n "s/^$1=//p" .env | tail -1; fi; }
OLD_URL="$(read_env MCPT_PUBLIC_URL)"
OLD_DOMAIN="$(read_env MCPT_DOMAIN)"
if [[ -z "$OLD_URL" && -n "$OLD_DOMAIN" ]]; then OLD_URL="https://$OLD_DOMAIN"; fi
if [[ -n "$DOMAIN" && -n "$PUBLIC_URL" ]]; then printf 'Use either --domain or --public-url.\n' >&2; exit 2; fi
if [[ -n "$DOMAIN" ]]; then PUBLIC_URL="https://$DOMAIN"; fi
if [[ -z "$PUBLIC_URL" ]]; then PUBLIC_URL="$OLD_URL"; fi
if [[ -z "$PUBLIC_URL" ]]; then read -r -p 'Cloud public URL (e.g. https://mcp.example.com): ' PUBLIC_URL </dev/tty; fi
PUBLIC_URL="${PUBLIC_URL%/}"
if [[ ! "$PUBLIC_URL" =~ ^(https?)://(\[[0-9a-fA-F:]+\]|[a-zA-Z0-9][a-zA-Z0-9.-]*)(:([0-9]{1,5}))?$ ]]; then
  printf 'Use http(s)://host[:port] without a path, credentials, query or spaces.\n' >&2; exit 2
fi
SCHEME="${BASH_REMATCH[1]}"; PUBLIC_HOST="${BASH_REMATCH[2]}"; PUBLIC_PORT="${BASH_REMATCH[4]:-}"
if [[ -z "$PUBLIC_PORT" ]]; then
  if [[ "$SCHEME" == https ]]; then PUBLIC_PORT=443; else PUBLIC_PORT=80; fi
fi
PUBLIC_PORT=$((10#$PUBLIC_PORT))
if [[ -z "$FRP_PORT" ]]; then FRP_PORT="$(read_env MCPT_FRP_PORT)"; fi
FRP_PORT="${FRP_PORT:-7000}"
if [[ ! "$FRP_PORT" =~ ^[0-9]{1,5}$ ]]; then printf 'Invalid frp port.\n' >&2; exit 2; fi
FRP_PORT=$((10#$FRP_PORT))
for CHECK_PORT in "$PUBLIC_PORT" "$FRP_PORT"; do
  if (( CHECK_PORT < 1 || CHECK_PORT > 65535 )); then printf 'Ports must be 1-65535.\n' >&2; exit 2; fi
  if [[ "$CHECK_PORT" == 2019 || "$CHECK_PORT" == 17800 || "$CHECK_PORT" == 17900 || "$CHECK_PORT" == 15432 ]] || (( CHECK_PORT >= 21000 && CHECK_PORT <= 21999 )); then
    printf 'Port %s is reserved for internal services. Choose another external port.\n' "$CHECK_PORT" >&2; exit 2
  fi
done
if [[ "$PUBLIC_PORT" == "$FRP_PORT" ]]; then printf 'Web/MCP and frp must use different ports.\n' >&2; exit 2; fi
if [[ "$SCHEME" == https && ( "$FRP_PORT" == 80 || "$FRP_PORT" == 443 ) ]]; then
  printf 'HTTPS certificate management reserves 80/443; choose a different frp port.\n' >&2; exit 2
fi
if [[ "$SCHEME" == http ]]; then
  if [[ -z "$ALLOW_HTTP" && "$PUBLIC_URL" == "$OLD_URL" ]]; then ALLOW_HTTP="$(read_env MCPT_ALLOW_INSECURE_HTTP)"; fi
  if [[ "$ALLOW_HTTP" != 1 ]]; then
    printf 'HTTP exposes passwords, tokens and MCP contents. Add --allow-insecure-http if intended.\n' >&2; exit 2
  fi
else
  ALLOW_HTTP=0
fi
ORIGIN_CHANGED=0
if [[ -n "$OLD_URL" && "$OLD_URL" != "$PUBLIC_URL" ]]; then
  ORIGIN_CHANGED=1
  if [[ "$MIGRATE" != 1 ]]; then
    printf 'Existing public URL differs. Add --migrate-origin, then update both agents in WebUI > Settings. Existing users/devices/services are preserved.\n' >&2; exit 2
  fi
fi
if [[ "$PRINT_CONFIG" == 1 ]]; then
  printf 'MCPT_PUBLIC_URL=%s\nMCPT_FRP_PORT=%s\nMCPT_ALLOW_INSECURE_HTTP=%s\n' "$PUBLIC_URL" "$FRP_PORT" "$ALLOW_HTTP"
  exit 0
fi
if [[ "$(uname -s)" != Linux || $EUID -ne 0 ]]; then printf 'Run this installer on Linux with sudo.\n' >&2; exit 1; fi
. "$ROOT_DIR/scripts/ensure-docker.sh"
ensure_docker
umask 077
if [[ -f .env ]]; then cp -p .env ".env.backup-$(date -u +%Y%m%d%H%M%S)"; else touch .env; fi
update_env() {
  local TEMP_ENV
  TEMP_ENV="$(mktemp .env.tmp.XXXXXX)"
  awk -v key="$1" -v value="$2" 'BEGIN { found=0 } index($0,key "=")==1 { if (!found) print key "=" value; found=1; next } { print } END { if (!found) print key "=" value }' .env > "$TEMP_ENV"
  mv "$TEMP_ENV" .env
}
if [[ -z "$(read_env MCPT_DB_PASSWORD)" ]]; then update_env MCPT_DB_PASSWORD "$(openssl rand -hex 32)"; fi
update_env MCPT_PUBLIC_URL "$PUBLIC_URL"
update_env MCPT_FRP_PORT "$FRP_PORT"
update_env MCPT_ALLOW_INSECURE_HTTP "$ALLOW_HTTP"
mkdir -p data/cloud
chown 10001:10001 data/cloud
chmod 700 data/cloud
docker compose build --pull cloud
docker compose up -d --wait postgres
if [[ "$ORIGIN_CHANGED" == 1 ]]; then docker compose stop cloud caddy; fi
INIT_ARGS=(cloud-init --public-url "$PUBLIC_URL" --frp-port "$FRP_PORT")
if [[ "$ALLOW_HTTP" == 1 ]]; then INIT_ARGS+=(--allow-insecure-http); fi
if [[ "$MIGRATE" == 1 ]]; then INIT_ARGS+=(--migrate-origin); fi
docker compose run --rm cloud "${INIT_ARGS[@]}"
docker compose up -d --no-deps --force-recreate cloud caddy
printf '\nControl panel: %s\nfrp host connection: %s:%s (TLS retained)\n' "$PUBLIC_URL" "$PUBLIC_HOST" "$FRP_PORT"
if [[ "$SCHEME" == http ]]; then
  printf 'Allow inbound TCP %s and %s. This HTTP deployment does not require 80/443.\n' "$PUBLIC_PORT" "$FRP_PORT"
  printf 'HTTP is plaintext: passwords, tokens and MCP contents can be intercepted or modified.\n'
else
  printf 'Allow inbound TCP %s and %s, plus 80/443 for HTTPS certificate management.\n' "$PUBLIC_PORT" "$FRP_PORT"
fi
printf 'Do not expose internal 17800, 17900, 15432 or 21000-21999.\n'
if [[ "$ORIGIN_CHANGED" == 1 ]]; then printf 'Update BOTH host and client WebUI > Settings > Cloud address. Device identities and mappings remain valid.\n'; fi
printf 'Save the initial administrator password shown on first initialization.\nLogs: docker compose logs -f --tail=100\n'
