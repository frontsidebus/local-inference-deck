#!/usr/bin/env bash
# Issue Let's Encrypt certs for Covenant: one SEPARATE ECDSA cert per name,
# http-01 via the nginx webroot /var/www/letsencrypt, deploy hook reloads nginx.
#
# Prerequisite: something on port 80 serves /.well-known/acme-challenge/ from
# /var/www/letsencrypt for these names - either the bootstrap site
# (nginx/bootstrap/00-acme-bootstrap), the full sites (00-default for the apex, chat,
# api and id names; 50-telemetry; 60-digest), or an optional site's ACME-only stub
# (nginx/bootstrap/60-digest-acme). 00-default's catch-all returns 444, so a name with
# none of these (e.g. a new digest name before deploy.sh linked its stub) fails http-01.
# DNS A records for every name must already point at the Elastic IP.
#
# Usage: LETSENCRYPT_EMAIL=you@example.com covenant/scripts/certs.sh [--dry-run] [--staging] NAME...
# Existing, unexpired certs are left alone (--keep-until-expiring), so this is
# safe to re-run. Renewal is certbot.timer (packaged); `certbot renew --dry-run` tests it.
set -euo pipefail

dry=0; extra=()
while [[ ${1:-} == --* ]]; do
  case $1 in
    --dry-run) dry=1 ;;
    --staging) extra+=(--staging) ;;   # LE staging CA, for rehearsals
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
(($#)) || { echo "usage: $0 [--dry-run] [--staging] NAME..." >&2; exit 2; }
: "${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL must be set (source site.env)}"

WEBROOT=/var/www/letsencrypt
HOOK=/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

for name in "$@"; do
  cmd=(certbot certonly --webroot -w "$WEBROOT"
       --cert-name "$name" -d "$name"
       --key-type ecdsa
       --deploy-hook "$HOOK"
       -m "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email
       --non-interactive --keep-until-expiring "${extra[@]}")
  if ((dry)); then printf '+ %s\n' "${cmd[*]}"; else "${cmd[@]}"; fi
done
