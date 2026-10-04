# shellcheck shell=bash
# Covenant (edge) secrets. Sourced by `scripts/gen-secrets.sh covenant` (or by
# covenant/deploy.sh when gen-secrets.sh is absent). Runs ON the edge host as root.
#
# Contract: never overwrite an existing secret; never print a secret value.
# Honors:  DESTDIR (stage under a prefix, for local tests)
#          DRY_RUN=1 (only report what would be created)
#
#   path                                 mode / owner     how
#   /etc/oauth2-proxy/cookie-secret      0600 root:root   32 random bytes, URL-safe base64 (44 chars, no newline)
#   /etc/oauth2-proxy/client-secret      0600 root:root   NOT generated: comes from Pocket-ID when the OIDC
#                                                         client is created (see covenant/README.md)
#   /etc/oauth2-proxy-digest/cookie-secret  0600 root:root   ditto; the digest oauth2-proxy instance
#   /etc/oauth2-proxy-digest/client-secret  0600 root:root   NOT generated: from Pocket-ID for the digest OIDC client
#   /etc/wireguard/privatekey            0600 root:root   wg genkey
#   /etc/wireguard/publickey             0644 root:root   wg pubkey < privatekey (not secret; give it to the backend)

_cov_root=${DESTDIR:-}
_cov_dry=${DRY_RUN:-0}

_cov_note() { printf 'secrets[covenant]: %s\n' "$*" >&2; }

# _cov_put PATH MODE GENERATOR...   - create PATH from GENERATOR's stdout if absent
_cov_put() {
  local rel=$1 path=$_cov_root$1 mode=$2; shift 2
  if [[ -s $path ]]; then _cov_note "keep    $rel (exists)"; return 0; fi
  if [[ $_cov_dry == 1 ]]; then _cov_note "would create $rel (mode $mode)"; return 0; fi
  install -d -m 0755 "$(dirname "$path")"
  local tmp; tmp=$(mktemp "$(dirname "$path")/.new.XXXXXX")
  chmod "$mode" "$tmp"
  if ! "$@" >"$tmp" || [[ ! -s $tmp ]]; then rm -f "$tmp"; _cov_note "FAILED  $rel"; return 1; fi
  mv -n "$tmp" "$path"; rm -f "$tmp"
  _cov_note "created $rel (mode $mode)"
}

_cov_cookie_secret() { openssl rand -base64 32 | tr -- '+/' '-_' | tr -d '\n'; }

_cov_wg_genkey() {
  if command -v wg >/dev/null 2>&1; then wg genkey
  elif [[ -n $_cov_root ]]; then head -c 32 /dev/urandom | base64   # staging only: placeholder-quality key
  else _cov_note "wg not installed (apt-get install wireguard-tools)"; return 1; fi
}
_cov_wg_pubkey() {
  if command -v wg >/dev/null 2>&1; then wg pubkey <"$_cov_root/etc/wireguard/privatekey"
  else echo "STAGING-NO-WG-TOOLS"; fi
}

# directories (mode matters: the oauth2-proxy dirs are root:oauth2-proxy 0750 once the user exists)
if [[ $_cov_dry != 1 ]]; then
  install -d -m 0700 "$_cov_root/etc/wireguard"
  install -d -m 0750 "$_cov_root/etc/oauth2-proxy"
  install -d -m 0750 "$_cov_root/etc/oauth2-proxy-digest"
fi

_cov_put /etc/oauth2-proxy/cookie-secret      0600 _cov_cookie_secret
_cov_put /etc/oauth2-proxy-digest/cookie-secret 0600 _cov_cookie_secret
_cov_put /etc/wireguard/privatekey       0600 _cov_wg_genkey
_cov_put /etc/wireguard/publickey        0644 _cov_wg_pubkey

if [[ -s $_cov_root/etc/oauth2-proxy/client-secret ]]; then
  _cov_note "keep    /etc/oauth2-proxy/client-secret (exists)"
else
  _cov_note "MISSING /etc/oauth2-proxy/client-secret - create the Pocket-ID OIDC client, then run"
  _cov_note "        sudo covenant/deploy.sh --set-client-secret   (reads it from stdin without echo, no trailing newline)"
  _cov_note "        oauth2-proxy stays stopped until it exists."
fi

if [[ -s $_cov_root/etc/oauth2-proxy-digest/client-secret ]]; then
  _cov_note "keep    /etc/oauth2-proxy-digest/client-secret (exists)"
else
  _cov_note "MISSING /etc/oauth2-proxy-digest/client-secret - create the digest Pocket-ID OIDC client, then store it"
  _cov_note "        (deploy.sh --set-client-secret currently targets the telemetry instance only)"
  _cov_note "        the digest oauth2-proxy instance stays stopped until it exists."
fi

unset -f _cov_note _cov_put _cov_cookie_secret _cov_wg_genkey _cov_wg_pubkey
unset _cov_root _cov_dry
