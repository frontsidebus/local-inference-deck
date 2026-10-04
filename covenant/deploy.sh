#!/usr/bin/env bash
# covenant/deploy.sh - build or converge the Covenant edge host. Run ON the edge,
# as root, from a checkout of this repo with a filled-in site.env.
#
#   sudo covenant/deploy.sh [--env FILE] [--dry-run] [--setup-lock]
#                           [--allow-bootstrap-downtime] [--staging-certs]
#   sudo covenant/deploy.sh --set-client-secret [--instance digest] [--force]
#   DESTDIR=/tmp/stage covenant/deploy.sh --env FILE   # stage files locally, run nothing
#
# Order: packages (+ sshd, unattended-upgrades, secrets) -> wireguard -> ufw ->
#        nginx base + bootstrap + certs -> full sites -> oauth2-proxy -> fail2ban
#
# Idempotent: files are only rewritten when content or mode differ, services are
# only reloaded/restarted when something they read changed, secrets are never
# overwritten, certs are only requested for names that have none.
#
#   --dry-run                   print what would change (diffs for non-secret files), change nothing
#   --setup-lock                install the Pocket-ID /setup lock (first-claim window only;
#                               the next deploy without the flag removes it)
#   --allow-bootstrap-downtime  a cert is missing while the full sites are live: allow the
#                               temporary switch to the port-80-only bootstrap site
#   --staging-certs             use the Let's Encrypt staging CA (rehearsal)
#   --set-client-secret         read the oauth2-proxy OIDC client secret from stdin (no echo),
#                               store it root 0600, restart the instance. --force to replace.
#   --instance telemetry|digest --set-client-secret target: /etc/oauth2-proxy(-digest)
#                               + the matching unit (default: telemetry)
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/.." && pwd)

ENV_FILE=$REPO/site.env
DRY_RUN=0 SETUP_LOCK=0 ALLOW_DOWNTIME=0 STAGING=0 SET_SECRET=0 FORCE=0
INSTANCE=telemetry
DESTDIR=${DESTDIR:-}
while (($#)); do
  case $1 in
    --env) ENV_FILE=$2; shift ;;
    --dry-run) DRY_RUN=1 ;;
    --setup-lock) SETUP_LOCK=1 ;;
    --allow-bootstrap-downtime) ALLOW_DOWNTIME=1 ;;
    --staging-certs) STAGING=1 ;;
    --set-client-secret) SET_SECRET=1 ;;
    --instance) INSTANCE=$2; shift ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
case $INSTANCE in telemetry|digest) ;; *) echo "unknown --instance: $INSTANCE (telemetry or digest)" >&2; exit 2 ;; esac
export DESTDIR DRY_RUN

# ---------------------------------------------------------------- versions (pinned, = live)
OAUTH2_PROXY_VERSION=v7.15.5
OAUTH2_PROXY_TARBALL=oauth2-proxy-${OAUTH2_PROXY_VERSION}.linux-amd64.tar.gz
OAUTH2_PROXY_URL=https://github.com/oauth2-proxy/oauth2-proxy/releases/download/${OAUTH2_PROXY_VERSION}/${OAUTH2_PROXY_TARBALL}
OAUTH2_PROXY_TARBALL_SHA256=f63f94bf72c5f46ab002a0a275aa8b3cf19b4d828aed08a13978cb9a62c3a1fd
OAUTH2_PROXY_BINARY_SHA256=ed5063f5a655560048a594974210f1c9e4f539bbfe81a834addb6fc740f3309d

PACKAGES=(nginx certbot python3-certbot-nginx fail2ban ufw wireguard wireguard-tools
          unattended-upgrades openssl curl ca-certificates)

# site.env variables substituted into *.tmpl (explicit list: nginx's own $vars survive)
COVENANT_VARS=(SPARK_DOMAIN SPARK_CHAT_HOST SPARK_API_HOST SPARK_ID_HOST SPARK_TELEMETRY_HOST SPARK_DIGEST_HOST
               LETSENCRYPT_EMAIL EDGE_WG_IP BACKEND_WG_IP WG_PORT WG_SUBNET WG_BACKEND_PUBLIC_KEY
               ADMIN_SOURCE_IPS TELEMETRY_GROUP DIGEST_GROUP OAUTH2_PROXY_CLIENT_ID OAUTH2_PROXY_DIGEST_CLIENT_ID
               WEBUI_PORT POCKETID_PORT LITELLM_PORT TELEMETRY_PORT DIGEST_PORT)

SITES=(00-default 10-apex 20-chat 30-id 40-api 50-telemetry 60-digest)

# ---------------------------------------------------------------- helpers
log()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mxx  %s\033[0m\n' "$*" >&2; exit 1; }

# Host-affecting commands: printed under --dry-run, printed-not-run under DESTDIR.
run() {
  if ((DRY_RUN)); then printf '    + %s\n' "$*"
  elif [[ -n $DESTDIR ]]; then printf '    + (staged, not run) %s\n' "$*"
  else "$@"; fi
}
live() { ((DRY_RUN == 0)) && [[ -z $DESTDIR ]]; }

# put SRC DEST MODE [OWNER:GROUP] [secret] -> returns 0 if DEST was (or would be) changed
put() {
  local src=$1 dst=$DESTDIR$2 mode=$3 own=${4:-root:root} secret=${5:-}
  if [[ -f $dst ]] && cmp -s "$src" "$dst" && [[ $(stat -c %a "$dst") == "${mode#0}" ]]; then
    if live && [[ $(stat -c %U:%G "$dst") != "$own" ]]; then chown "$own" "$dst"; info "chown $own $2"; fi
    return 1
  fi
  info "install $2 ($mode $own)"
  if ((DRY_RUN)); then
    [[ -z $secret && -f $dst ]] && { diff -u "$dst" "$src" | sed 's/^/      /' || true; }
    return 0
  fi
  install -D -m "$mode" "$src" "$dst"
  [[ -z $DESTDIR ]] && chown "$own" "$dst"
  return 0
}

mkdir_p() {   # DIR MODE [OWNER:GROUP]
  local d=$DESTDIR$1
  ((DRY_RUN)) && { [[ -d $d ]] || info "mkdir $1 ($2)"; return 0; }
  install -d -m "$2" "$d"
  [[ -z $DESTDIR && -n ${3:-} ]] && chown "$3" "$d"
  return 0
}

symlink() {   # TARGET LINK
  local l=$DESTDIR$2
  [[ -L $l && $(readlink "$l") == "$1" ]] && return 1
  info "link $2 -> $1"
  ((DRY_RUN)) || { mkdir -p "$(dirname "$l")"; ln -sfn "$1" "$l"; }
  return 0
}

unlink_if() {   # LINK
  [[ -e $DESTDIR$1 || -L $DESTDIR$1 ]] || return 1
  info "remove $1"
  ((DRY_RUN)) || rm -f "$DESTDIR$1"
  return 0
}

service_active() { live && systemctl is-active --quiet "$1"; }

render() {   # TEMPLATE OUT
  local list; list=$(printf '${%s} ' "${COVENANT_VARS[@]}")
  mkdir -p "$(dirname "$2")"
  envsubst "$list" <"$1" >"$2"
}

# ---------------------------------------------------------------- env
[[ -f $ENV_FILE ]] || die "no site.env at $ENV_FILE (copy site.env.example, or pass --env FILE)"
set -a; # shellcheck source=/dev/null
source "$ENV_FILE"; set +a
for v in "${COVENANT_VARS[@]}"; do [[ -n ${!v:-} ]] || die "$v is empty in $ENV_FILE"; done
command -v envsubst >/dev/null || die "envsubst missing (apt-get install gettext-base)"
if live; then [[ $EUID -eq 0 ]] || die "run as root (or use --dry-run / DESTDIR=)"; fi

OAUTH2_DIR=/etc/oauth2-proxy
OAUTH2_DIGEST_DIR=/etc/oauth2-proxy-digest

# ---------------------------------------------------------------- --set-client-secret
if ((SET_SECRET)); then
  live || die "--set-client-secret only runs on the edge host"
  if [[ $INSTANCE == digest ]]; then O2P_DIR=$OAUTH2_DIGEST_DIR; O2P_UNIT=oauth2-proxy-digest
  else O2P_DIR=$OAUTH2_DIR; O2P_UNIT=oauth2-proxy; fi
  f=$O2P_DIR/client-secret
  [[ -s $f && $FORCE -eq 0 ]] && die "$f exists; pass --force to replace it"
  read -rsp "oauth2-proxy client secret (from Pocket-ID): " s; echo
  [[ -n $s ]] || die "empty secret"
  install -d -m 0750 -o root -g oauth2-proxy "$O2P_DIR" 2>/dev/null || install -d -m 0750 "$O2P_DIR"
  (umask 077; printf '%s' "$s" >"$f.new"); unset s
  chown root:root "$f.new"; chmod 0600 "$f.new"; mv "$f.new" "$f"
  info "stored $f (root 0600)"
  systemctl restart "$O2P_UNIT" && systemctl --no-pager --lines=0 status "$O2P_UNIT"
  exit 0
fi

BUILD=$(mktemp -d); trap 'rm -rf "$BUILD"' EXIT
log "Rendering templates into a private build dir"
while IFS= read -r -d '' t; do
  rel=${t#"$HERE"/}; render "$t" "$BUILD/${rel%.tmpl}"
done < <(find "$HERE" -name '*.tmpl' -print0)
mkdir -p "$BUILD/nginx/snippets"
"$HERE/scripts/setup-lock.sh" render-snippet "$BUILD/nginx/snippets/pocketid-setup-lock.conf"
((SETUP_LOCK)) && "$HERE/scripts/setup-lock.sh" patch-site "$BUILD/nginx/sites-available/30-id"
if grep -rl '\${[A-Z_]*}' "$BUILD"; then die "unrendered \${VARS} left (see files above)"; fi

# ================================================================ 1. packages + base
log "1/7 packages, sshd, unattended-upgrades, secrets"
missing=()
for p in "${PACKAGES[@]}"; do dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'ok installed' || missing+=("$p"); done
if ((${#missing[@]})); then
  run apt-get update
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
else info "all packages present"; fi

if put "$HERE/apt/apt.conf.d/20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades 0644; then :; fi
if put "$HERE/ssh/sshd_config.d/10-hardening.conf" /etc/ssh/sshd_config.d/10-hardening.conf 0644; then
  run sshd -t
  run systemctl reload ssh
fi
run systemctl enable --now unattended-upgrades

if live && [[ -x $REPO/scripts/gen-secrets.sh ]]; then
  "$REPO/scripts/gen-secrets.sh" covenant
else
  # shellcheck source=../scripts/secrets.d/covenant.sh
  source "$REPO/scripts/secrets.d/covenant.sh"
fi

# ================================================================ 2. wireguard
log "2/7 wireguard (wg0)"
[[ $WG_BACKEND_PUBLIC_KEY == CHANGEME ]] && { live && die "set WG_BACKEND_PUBLIC_KEY in site.env"; warn "WG_BACKEND_PUBLIC_KEY is CHANGEME"; }
keyf=$DESTDIR/etc/wireguard/privatekey
if [[ -s $keyf ]]; then
  ( umask 077; k=$(<"$keyf"); c=$(<"$BUILD/wireguard/wg0.conf"); printf '%s\n' "${c//@WG_PRIVATE_KEY@/$k}" >"$BUILD/wireguard/wg0.conf.final" )
else
  cp "$BUILD/wireguard/wg0.conf" "$BUILD/wireguard/wg0.conf.final"   # dry-run on a fresh host
fi
mkdir_p /etc/wireguard 0700
if put "$BUILD/wireguard/wg0.conf.final" /etc/wireguard/wg0.conf 0600 root:root secret; then
  if service_active wg-quick@wg0; then
    # peer/key changes apply live; an Address/ListenPort change needs a restart
    run bash -c 'wg syncconf wg0 <(wg-quick strip wg0)'
  fi
fi
rm -f "$BUILD/wireguard/wg0.conf.final"
run systemctl enable --now wg-quick@wg0
[[ -s $DESTDIR/etc/wireguard/publickey ]] && info "edge WG public key (give to the backend): /etc/wireguard/publickey"

# ================================================================ 3. ufw
log "3/7 ufw"
if live; then "$HERE/scripts/ufw.sh"; else "$HERE/scripts/ufw.sh" --dry-run | sed 's/^/    /'; fi

# ================================================================ 4. nginx base, bootstrap, certs
log "4/7 nginx base + TLS certs"
NGX_CHANGED=0
if live; then
  mkdir -p /var/backups/covenant
  tar -C / -czf "/var/backups/covenant/nginx-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" etc/nginx 2>/dev/null || true
fi
if put "$HERE/nginx/nginx.conf" /etc/nginx/nginx.conf 0644; then NGX_CHANGED=1; fi
if put "$HERE/nginx/conf.d/phase3-http.conf" /etc/nginx/conf.d/phase3-http.conf 0644; then NGX_CHANGED=1; fi
for s in proxy-common tls-common; do
  if put "$HERE/nginx/snippets/$s.conf" "/etc/nginx/snippets/$s.conf" 0644; then NGX_CHANGED=1; fi
done
put "$HERE/letsencrypt/renewal-hooks/deploy/reload-nginx.sh" /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh 0755 || true
mkdir_p /var/www/letsencrypt 0755
# tls-common.conf includes certbot's nginx TLS params. `certbot certonly --webroot`
# never writes them (only the nginx installer does), so seed them from the package.
for pair in "certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf:options-ssl-nginx.conf" \
            "certbot/ssl-dhparams.pem:ssl-dhparams.pem"; do
  src=/usr/lib/python3/dist-packages/${pair%%:*}; dst=/etc/letsencrypt/${pair##*:}
  if [[ ! -s $DESTDIR$dst ]]; then
    info "seed $dst from $src"
    if live; then install -D -m 0644 "$src" "$dst"; elif [[ -n $DESTDIR && -f $src ]] && ((DRY_RUN == 0)); then install -D -m 0644 "$src" "$DESTDIR$dst"; fi
  fi
done

NAMES=("$SPARK_DOMAIN" "$SPARK_CHAT_HOST" "$SPARK_API_HOST" "$SPARK_ID_HOST" "$SPARK_TELEMETRY_HOST" "$SPARK_DIGEST_HOST")
need=()
for n in "${NAMES[@]}"; do [[ -s $DESTDIR/etc/letsencrypt/live/$n/fullchain.pem ]] || need+=("$n"); done
certs_args=(); ((STAGING)) && certs_args+=(--staging)
if ((${#need[@]})); then
  info "no cert yet for: ${need[*]}"
  if [[ -L $DESTDIR/etc/nginx/sites-enabled/00-default ]] && ((ALLOW_DOWNTIME == 0)) && live; then
    die "full sites are live but certs are missing; re-run with --allow-bootstrap-downtime
    (443 is down while the port-80-only bootstrap site issues the new certs)"
  fi
  # Bootstrap: port 80 only, ACME webroot for every name; no 443 server can load yet.
  put "$BUILD/nginx/bootstrap/00-acme-bootstrap" /etc/nginx/sites-available/00-acme-bootstrap 0644 || true
  for l in "$DESTDIR"/etc/nginx/sites-enabled/*; do [[ -e $l || -L $l ]] && unlink_if "/etc/nginx/sites-enabled/$(basename "$l")"; done
  symlink /etc/nginx/sites-available/00-acme-bootstrap /etc/nginx/sites-enabled/00-acme-bootstrap || true
  run nginx -t
  run systemctl enable nginx
  if service_active nginx; then run systemctl reload nginx; else run systemctl start nginx; fi
  if live; then "$HERE/scripts/certs.sh" "${certs_args[@]}" "${need[@]}"
  else "$HERE/scripts/certs.sh" --dry-run "${certs_args[@]}" "${need[@]}" | sed 's/^/    /'; fi
  NGX_CHANGED=1
else
  info "certs present for all ${#NAMES[@]} names"
fi

# ================================================================ 5. full sites
log "5/7 nginx sites"
for s in "${SITES[@]}"; do
  if put "$BUILD/nginx/sites-available/$s" "/etc/nginx/sites-available/$s" 0644; then NGX_CHANGED=1; fi
  if symlink "/etc/nginx/sites-available/$s" "/etc/nginx/sites-enabled/$s"; then NGX_CHANGED=1; fi
done
if unlink_if /etc/nginx/sites-enabled/default; then NGX_CHANGED=1; fi
if unlink_if /etc/nginx/sites-enabled/00-acme-bootstrap; then NGX_CHANGED=1; fi
if ((SETUP_LOCK)); then
  if put "$BUILD/nginx/snippets/pocketid-setup-lock.conf" /etc/nginx/snippets/pocketid-setup-lock.conf 0644; then NGX_CHANGED=1; fi
  warn "Pocket-ID setup lock is ON. Claim the admin at https://$SPARK_ID_HOST/setup, then re-deploy without --setup-lock."
elif unlink_if /etc/nginx/snippets/pocketid-setup-lock.conf; then NGX_CHANGED=1; fi
if ((NGX_CHANGED)); then
  if live; then
    if ! nginx -t; then
      latest=$(ls -1t /var/backups/covenant/nginx-*.tar.gz | head -1)
      rm -rf /etc/nginx/sites-enabled/*; tar -C / -xzf "$latest"
      die "nginx -t failed; restored /etc/nginx from $latest (nginx not reloaded)"
    fi
    systemctl enable nginx
    if systemctl is-active --quiet nginx; then systemctl reload nginx; else systemctl start nginx; fi
  else run nginx -t; run systemctl reload nginx; fi
else info "nginx unchanged"; fi

# ================================================================ 6. oauth2-proxy
log "6/7 oauth2-proxy $OAUTH2_PROXY_VERSION"
if live && ! getent passwd oauth2-proxy >/dev/null; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin oauth2-proxy
elif ! live; then info "(ensure system user oauth2-proxy: useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin)"; fi

BIN=/usr/local/bin/oauth2-proxy
O2P_CHANGED=0
have=$( [[ -f $DESTDIR$BIN ]] && sha256sum "$DESTDIR$BIN" | cut -d' ' -f1 || echo none)
if [[ $have != "$OAUTH2_PROXY_BINARY_SHA256" ]]; then
  info "install $BIN $OAUTH2_PROXY_VERSION (have: $have)"
  if ((DRY_RUN)); then info "+ curl -fsSL $OAUTH2_PROXY_URL  (verify tarball + binary sha256)"
  else
    dl=$BUILD/dl; mkdir -p "$dl"
    curl -fsSL -o "$dl/$OAUTH2_PROXY_TARBALL" "$OAUTH2_PROXY_URL"
    echo "$OAUTH2_PROXY_TARBALL_SHA256  $dl/$OAUTH2_PROXY_TARBALL" | sha256sum -c --quiet - || die "tarball sha256 mismatch"
    tar -C "$dl" -xzf "$dl/$OAUTH2_PROXY_TARBALL"
    b=$dl/oauth2-proxy-${OAUTH2_PROXY_VERSION}.linux-amd64/oauth2-proxy
    echo "$OAUTH2_PROXY_BINARY_SHA256  $b" | sha256sum -c --quiet - || die "binary sha256 mismatch"
    install -D -m 0755 "$b" "$DESTDIR$BIN"
    O2P_CHANGED=1
  fi
fi
mkdir_p "$OAUTH2_DIR" 0750 root:oauth2-proxy
if put "$BUILD/oauth2-proxy/oauth2-proxy.cfg" "$OAUTH2_DIR/oauth2-proxy.cfg" 0640 root:oauth2-proxy; then O2P_CHANGED=1; fi
if put "$BUILD/oauth2-proxy/oauth2-proxy.service" /etc/systemd/system/oauth2-proxy.service 0644; then
  O2P_CHANGED=1; run systemctl daemon-reload
fi
run systemctl enable oauth2-proxy
if [[ -s $DESTDIR$OAUTH2_DIR/client-secret ]]; then
  if service_active oauth2-proxy; then ((O2P_CHANGED)) && run systemctl restart oauth2-proxy
  else run systemctl start oauth2-proxy; fi
else
  warn "oauth2-proxy NOT started: $OAUTH2_DIR/client-secret missing (deploy.sh --set-client-secret)"
fi

# Second instance: the digest gate (same binary, own config/unit/secret dir)
O2P_DIGEST_CHANGED=0
mkdir_p "$OAUTH2_DIGEST_DIR" 0750 root:oauth2-proxy
if put "$BUILD/oauth2-proxy/oauth2-proxy-digest.cfg" "$OAUTH2_DIGEST_DIR/oauth2-proxy.cfg" 0640 root:oauth2-proxy; then O2P_DIGEST_CHANGED=1; fi
if put "$BUILD/oauth2-proxy/oauth2-proxy-digest.service" /etc/systemd/system/oauth2-proxy-digest.service 0644; then
  O2P_DIGEST_CHANGED=1; run systemctl daemon-reload
fi
run systemctl enable oauth2-proxy-digest
if [[ -s $DESTDIR$OAUTH2_DIGEST_DIR/client-secret ]]; then
  if service_active oauth2-proxy-digest; then ((O2P_DIGEST_CHANGED)) && run systemctl restart oauth2-proxy-digest
  else run systemctl start oauth2-proxy-digest; fi
else
  warn "oauth2-proxy-digest NOT started: $OAUTH2_DIGEST_DIR/client-secret missing (deploy.sh --set-client-secret --instance digest)"
fi

# ================================================================ 7. fail2ban
log "7/7 fail2ban"
F2B_CHANGED=0
if put "$BUILD/fail2ban/jail.local" /etc/fail2ban/jail.local 0644; then F2B_CHANGED=1; fi
if put "$HERE/fail2ban/jail.d/sshd-ubuntu.local" /etc/fail2ban/jail.d/sshd-ubuntu.local 0644; then F2B_CHANGED=1; fi
run systemctl enable fail2ban
if ((F2B_CHANGED)); then
  run fail2ban-client --test
  run systemctl restart fail2ban
fi

log "done$( ((DRY_RUN)) && echo ' (dry run: nothing changed)')$( [[ -n $DESTDIR ]] && echo " (staged under $DESTDIR)")"
