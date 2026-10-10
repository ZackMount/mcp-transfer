#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
DOMAIN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:?--domain needs a domain}"; shift 2 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'This installer requires Linux.\n' >&2; exit 1
fi
if [[ $EUID -ne 0 ]]; then
  printf 'Run this installer with sudo.\n' >&2; exit 1
fi
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  if [[ -f /etc/os-release ]]; then . /etc/os-release; fi
  if [[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]]; then
    apt-get update
    apt-get install -y docker.io docker-compose-v2 ca-certificates curl openssl
    systemctl enable --now docker
  else
    printf 'Install Docker Engine with Compose v2 for this Linux distribution, then run this command again. Ubuntu 24.04 is provisioned automatically.\n' >&2
    exit 1
  fi
fi
if [[ ! -f .env ]]; then
  if [[ -z "$DOMAIN" ]]; then read -r -p 'Domain already pointing to this server (e.g. mcp.example.com): ' DOMAIN </dev/tty; fi
  if [[ ! "$DOMAIN" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,63}$ ]]; then
    printf 'Enter a DNS domain only, without a scheme, port or path.\n' >&2; exit 1
  fi
  umask 077
  DB_PASSWORD="$(openssl rand -hex 32)"
  printf 'MCPT_DOMAIN=%s\nMCPT_DB_PASSWORD=%s\n' "$DOMAIN" "$DB_PASSWORD" > .env
else
  SAVED_DOMAIN="$(sed -n 's/^MCPT_DOMAIN=//p' .env)"
  if [[ -n "$DOMAIN" && "$DOMAIN" != "$SAVED_DOMAIN" ]]; then
    printf 'Existing deployment uses a different domain. Origin changes require a credential migration.\n' >&2; exit 1
  fi
  DOMAIN="$SAVED_DOMAIN"
fi
mkdir -p data/cloud
chown 10001:10001 data/cloud
chmod 700 data/cloud
docker compose build --pull cloud
docker compose up -d --wait postgres
docker compose run --rm cloud cloud-init --public-url "https://$DOMAIN"
docker compose up -d
printf '\nControl panel: https://%s\n' "$DOMAIN"
printf 'Allow inbound TCP 80/443 for HTTPS issuance and TCP 7000 for enrolled hosts. Do not open 17800, 17900, 15432 or 21000-21999.\n'
printf 'Save the generated initial password above. On first login, enable MFA and create host/client enrollment codes.\n'
printf 'Deployment status: docker compose ps\nLogs: docker compose logs -f --tail=100\n'
printf 'TLS issuance may take a short time and requires correct DNS and firewall rules.\n'
