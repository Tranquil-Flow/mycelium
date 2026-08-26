#!/usr/bin/env bash
# Provision the A8 public HTTPS origin via Cloudflare Tunnel (spec §10).
#
# The publicly trusted certificate is Cloudflare's edge certificate; the
# seed listener stays loopback. Renewal is automatic and grants NO seed
# authority. Runtime secrets (tunnel credentials) live only in the
# owner-private state dir ~/.mycelium/a8-tls (mode 700) - never in the
# repository.
#
# Usage:
#   a8_provision_public_origin.sh <domain> [--dry-run]
#   a8_provision_public_origin.sh <domain> --check
#
# Requires one interactive browser step once per machine:
#   cloudflared tunnel login

set -euo pipefail

DOMAIN="${1:-}"
MODE="${2:-run}"
TUNNEL_NAME="${A8_TUNNEL_NAME:-a8-bootstrap}"
STATE_DIR="${HOME}/.mycelium/a8-tls"
TEMPLATE="$(cd "$(dirname "$0")/.." && pwd)/release/a8-tls-bootstrap/cloudflared-config.yml.template"

if [[ -z "${DOMAIN}" ]]; then
    echo "usage: $0 <public-hostname> [--dry-run|--check]" >&2
    exit 2
fi
if [[ ! "${DOMAIN}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "origin invalid: hostname must be a bare DNS name (no scheme, no path)" >&2
    exit 2
fi

if ! command -v cloudflared >/dev/null 2>&1; then
    echo "cloudflared is required (brew install cloudflared)" >&2
    exit 2
fi

mkdir -p "${STATE_DIR}"
chmod 700 "${STATE_DIR}"

if ! cloudflared tunnel list >/dev/null 2>&1; then
    echo "not authenticated: run 'cloudflared tunnel login' once in a browser, then retry" >&2
    exit 2
fi

TUNNEL_ID="$(cloudflared tunnel list --output json | python3 -c "
import json, sys
rows = json.load(sys.stdin)
match = [r for r in rows if r.get('name') == '${TUNNEL_NAME}']
print(match[0]['id'] if match else '')
")"

if [[ -z "${TUNNEL_ID}" ]]; then
    if [[ "${MODE}" == "--dry-run" ]]; then
        echo "dry-run: would create tunnel ${TUNNEL_NAME}"
    else
        cloudflared tunnel create "${TUNNEL_NAME}"
        TUNNEL_ID="$(cloudflared tunnel list --output json | python3 -c "
import json, sys
rows = json.load(sys.stdin)
print([r for r in rows if r.get('name') == '${TUNNEL_NAME}'][0]['id'])
")"
    fi
fi

if [[ -n "${TUNNEL_ID}" ]]; then
    if [[ "${MODE}" == "--dry-run" ]]; then
        echo "dry-run: would route dns ${TUNNEL_NAME} ${DOMAIN}"
    else
        cloudflared tunnel route dns "${TUNNEL_NAME}" "${DOMAIN}"
    fi
fi

RUNTIME_CONFIG="${STATE_DIR}/${TUNNEL_NAME}-config.yml"
if [[ "${MODE}" == "--dry-run" ]]; then
    echo "dry-run: would write runtime config to ${RUNTIME_CONFIG}"
    echo "dry-run: would run 'cloudflared tunnel run ${TUNNEL_NAME}'"
    exit 0
fi

CREDENTIALS_FILE="${STATE_DIR}/${TUNNEL_ID}.json"
sed \
    -e "s|__A8_PUBLIC_HOSTNAME__|${DOMAIN}|g" \
    -e "s|__A8_TUNNEL_ID__|${TUNNEL_ID}|g" \
    -e "s|__A8_CREDENTIALS_FILE__|${CREDENTIALS_FILE}|g" \
    -e "s|__A8_SEED_LOOPBACK_PORT__|${A8_SEED_LOOPBACK_PORT:-8876}|g" \
    "${TEMPLATE}" > "${RUNTIME_CONFIG}"
chmod 600 "${RUNTIME_CONFIG}"

if [[ "${MODE}" == "--check" ]]; then
    echo "checking public origin https://${DOMAIN}/seed/identity ..."
    curl --fail --silent --show-error --max-time 10 \
        "https://${DOMAIN}/seed/identity" >/dev/null
    echo "public origin reachable: https://${DOMAIN}"
    exit 0
fi

echo "starting tunnel ${TUNNEL_NAME} for https://${DOMAIN} (seed loopback :${A8_SEED_LOOPBACK_PORT:-8876})"
exec cloudflared tunnel --config "${RUNTIME_CONFIG}" run "${TUNNEL_NAME}"
