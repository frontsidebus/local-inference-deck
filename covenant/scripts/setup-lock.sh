#!/usr/bin/env bash
# Optional Pocket-ID first-claim lock for the id. site.
#
# Between Pocket-ID going live and the owner claiming the initial admin, anyone
# who reaches /setup could claim it. The lock restricts the setup paths to
# ADMIN_SOURCE_IPS. Remove it as soon as the admin is claimed.
#
#   setup-lock.sh render-snippet OUT   render nginx/snippets/pocketid-setup-lock.conf.in -> OUT
#   setup-lock.sh patch-site FILE      add the include to a rendered 30-id (idempotent)
#   setup-lock.sh enable               on the edge host: install snippet + include, nginx -t, reload
#   setup-lock.sh disable              on the edge host: remove both, nginx -t, reload
#   setup-lock.sh status
# Needs ADMIN_SOURCE_IPS, BACKEND_WG_IP, POCKETID_PORT in the environment (source site.env).
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SNIPPET_IN=$HERE/../nginx/snippets/pocketid-setup-lock.conf.in
NGINX=${DESTDIR:-}/etc/nginx
SITE=$NGINX/sites-available/30-id
SNIPPET=$NGINX/snippets/pocketid-setup-lock.conf
MARKER='    # Temporary: setup paths restricted to owner IP until admin is claimed'
INCLUDE='    include snippets/pocketid-setup-lock.conf;'

render_snippet() {
  : "${ADMIN_SOURCE_IPS:?}" "${BACKEND_WG_IP:?}" "${POCKETID_PORT:?}"
  local allow="" ip
  for ip in $ADMIN_SOURCE_IPS; do allow+="    allow $ip;"$'\n'; done
  allow=${allow%$'\n'}
  local up="$BACKEND_WG_IP:$POCKETID_PORT" line
  while IFS= read -r line; do
    case $line in
      @ALLOW@) printf '%s\n' "$allow" ;;
      *) printf '%s\n' "${line//@POCKETID_UPSTREAM@/$up}" ;;
    esac
  done <"$SNIPPET_IN" >"$1"
}

patch_site() {   # insert the include right after the marker comment
  grep -qxF "$INCLUDE" "$1" && return 0
  grep -qxF "$MARKER" "$1" || { echo "marker comment not found in $1" >&2; return 1; }
  local tmp; tmp=$(mktemp)
  awk -v m="$MARKER" -v inc="$INCLUDE" '{print} $0==m{print inc}' "$1" >"$tmp"
  cat "$tmp" >"$1"; rm -f "$tmp"
}

unpatch_site() { local tmp; tmp=$(mktemp); grep -vxF "$INCLUDE" "$1" >"$tmp" || true; cat "$tmp" >"$1"; rm -f "$tmp"; }

reload_or_rollback() {   # $1 = backup of 30-id
  if nginx -t; then systemctl reload nginx
  else echo "nginx -t failed, restoring 30-id" >&2; cp -p "$1" "$SITE"; rm -f "$SNIPPET.new"; exit 1; fi
}

case ${1:-} in
  render-snippet) render_snippet "${2:?OUT}" ;;
  patch-site)     patch_site "${2:?FILE}" ;;
  enable)
    bak=$(mktemp); cp -p "$SITE" "$bak"
    render_snippet "$SNIPPET.new"; install -m 0644 "$SNIPPET.new" "$SNIPPET"; rm -f "$SNIPPET.new"
    patch_site "$SITE"
    reload_or_rollback "$bak"; rm -f "$bak"
    echo "setup lock ENABLED for: $ADMIN_SOURCE_IPS" ;;
  disable)
    bak=$(mktemp); cp -p "$SITE" "$bak"
    unpatch_site "$SITE"; rm -f "$SNIPPET"
    reload_or_rollback "$bak"; rm -f "$bak"
    echo "setup lock removed" ;;
  status)
    if grep -qxF "$INCLUDE" "$SITE" 2>/dev/null; then echo enabled; else echo disabled; fi ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
