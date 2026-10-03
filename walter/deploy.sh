#!/usr/bin/env bash
# walter/deploy.sh -- build or update Walter (backend VM) from this repo. Idempotent.
#
# Usage (on Walter, from a checkout of this repo, with ../site.env filled in):
#   sudo walter/deploy.sh [options]
#
#   --dry-run          print what would change; write nothing outside a temp dir
#   --destdir DIR      install files under DIR instead of / and skip every system action
#                      (apt, users, mounts, systemctl, docker, ufw). For local tests:
#                        walter/deploy.sh --destdir /tmp/walter-root            # stage files + secrets
#                        walter/deploy.sh --destdir /tmp/walter-root --dry-run  # diff against a staged root
#   --site-env FILE    site values (default: ../site.env next to this repo's walter/)
#   --skip-packages    do not apt-get install / pin the NVIDIA, Docker and tool packages
#   --no-start         install and enable, but do not start units or compose stacks
#   --with-hermes      also configure Hermes Agent for BACKEND_SSH_USER (needs Hermes installed)
#
# Order: render -> packages -> users -> /models mount -> llama-swap binary -> files ->
#        env files -> gen-secrets -> systemd units + firewall -> gateway -> keys -> webui ->
#        monitoring -> telemetry -> (hermes). Never deletes anything; never overwrites a secret.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/.." && pwd)

# ---- pinned as live -------------------------------------------------------------------
LLAMA_SWAP_VERSION=260
LLAMA_SWAP_URL="https://github.com/mostlygeek/llama-swap/releases/download/v${LLAMA_SWAP_VERSION}/llama-swap_${LLAMA_SWAP_VERSION}_linux_amd64.tar.gz"
LLAMA_SWAP_SHA256=1392a6cdb3fec96845d091254b43ed42768082ed9c511e5708ed4a1d1ba95101   # the extracted binary
NVIDIA_DRIVER_PKG=nvidia-driver-580             # live: 580.178.04-0ubuntu0.24.04.1 (Ubuntu archive, DKMS)
NVIDIA_CTK_VERSION=1.20.1-1                     # nvidia-container-toolkit (NVIDIA apt repo)
APT_PKGS=(docker.io docker-compose-v2 wireguard-tools ufw xfsprogs zstd jq curl python3 python3-yaml gettext-base iptables)

# ---- options -------------------------------------------------------------------------
DRY=0 DESTDIR="" SITE_ENV_FILE="$REPO/site.env" SKIP_PKGS=0 NO_START=0 WITH_HERMES=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run) DRY=1 ;;
    --destdir) DESTDIR=$(realpath -m "${2:?}"); shift ;;
    --site-env) SITE_ENV_FILE=$(realpath "${2:?}"); shift ;;
    --skip-packages) SKIP_PKGS=1 ;;
    --no-start) NO_START=1 ;;
    --with-hermes) WITH_HERMES=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "deploy: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
LIVE=1; [[ -n $DESTDIR || $DRY == 1 ]] && LIVE=0   # LIVE = touching the real system

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'deploy: %s\n' "$*" >&2; exit 1; }

# run CMD... : execute only when acting on the real system
run() {
  if [[ $DRY == 1 ]]; then say "  would run: $*"
  elif [[ -n $DESTDIR ]]; then say "  skip (destdir): $*"
  else "$@"; fi
}

[[ -f $SITE_ENV_FILE ]] || die "site env not found: $SITE_ENV_FILE"
if [[ $LIVE == 1 && $EUID -ne 0 ]]; then die "run as root (or use --dry-run / --destdir)"; fi
set -a; . "$SITE_ENV_FILE"; set +a
for v in BACKEND_WG_IP EDGE_WG_IP EDGE_PUBLIC_IP WG_PORT WG_SUBNET MODELS_DIR MODELS_FS_UUID \
         BACKEND_SSH_USER BACKEND_LAN_IF GATEWAY_DOCKER_SUBNET SPARK_CHAT_HOST SPARK_ID_HOST; do
  [[ -n ${!v:-} ]] || die "$v is empty in $SITE_ENV_FILE"
done
for v in MODELS_FS_UUID WG_EDGE_PUBLIC_KEY; do
  if [[ ${!v} == CHANGEME ]]; then
    [[ $LIVE == 1 ]] && die "set $v in $SITE_ENV_FILE"
    warn "$v is CHANGEME (ok for a test run)"
  fi
done

T=$DESTDIR   # target root prefix
USER_HOME=$(getent passwd "$BACKEND_SSH_USER" | cut -d: -f6 || true)
USER_HOME=${USER_HOME:-/home/$BACKEND_SSH_USER}

# ---- 1. render ----------------------------------------------------------------------
step "render templates"
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/walter-render.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
for d in base wireguard llama-swap gateway webui monitoring telemetry backup update-check firewall hermes; do
  "$REPO/scripts/render.sh" -e "$SITE_ENV_FILE" "$HERE/$d" "$STAGE/$d"
done
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
say "  rendered to $STAGE"

# ---- install helpers -----------------------------------------------------------------
declare -A CHANGED=()   # tag -> 1 when a file in that group changed

# install_file SRC DEST MODE OWNER GROUP [TAG]
install_file() {
  local src=$1 dest=$T$2 mode=$3 owner=$4 group=$5 tag=${6:-}
  local state=new
  if [[ -e $dest ]]; then
    if [[ ! -r $dest ]]; then state="unreadable"
    elif cmp -s "$src" "$dest"; then state=same; fi
  fi
  case $state in
    same) return 0 ;;
    *) say "  $(printf '%-8s' "$state") $2" ;;
  esac
  [[ -n $tag ]] && CHANGED[$tag]=1
  [[ $DRY == 1 ]] && return 0
  install -d -m 0755 "$(dirname "$dest")"
  if [[ $EUID -eq 0 ]]; then install -m "$mode" -o "$owner" -g "$group" "$src" "$dest"
  else install -m "$mode" "$src" "$dest"; fi
}

# install_tree SRCDIR DESTDIR MODE OWNER GROUP TAG   (every file, same mode)
install_tree() {
  local src=$1 dst=$2 f rel
  while IFS= read -r -d '' f; do
    rel=${f#"$src"/}
    install_file "$f" "$dst/$rel" "$3" "$4" "$5" "$6"
  done < <(find "$src" -type f -print0 | sort -z)
}

mkdir_p() { # PATH MODE OWNER GROUP
  [[ $DRY == 1 ]] && { [[ -d $T$1 ]] || say "  mkdir    $1 ($2 $3:$4)"; return 0; }
  install -d -m "$2" "$T$1"
  [[ $EUID -eq 0 ]] && chown "$3:$4" "$T$1"
  return 0
}

# env_from_example DIR NAME : DIR/NAME.example -> DIR/NAME (0600) only if NAME is absent;
# if present, report drift of non-secret keys (never modified).
env_from_example() {
  local ex=$T$1/$2.example f=$T$1/$2
  if [[ ! -e $f ]]; then
    say "  create   $1/$2 from $2.example (secrets filled by gen-secrets)"
    [[ $DRY == 1 ]] && return 0
    install -m 0600 "$ex" "$f"
    return 0
  fi
  [[ -r $f ]] || { say "  unreadable $1/$2 (run as root to check drift)"; return 0; }
  [[ -r $ex ]] || ex=$STAGE/$3   # dry run: compare with the freshly rendered example
  python3 - "$ex" "$f" "$1/$2" <<'PY'
import sys
def parse(p):
    d = {}
    for l in open(p):
        l = l.rstrip("\n")
        if l and not l.lstrip().startswith("#") and "=" in l:
            k, v = l.split("=", 1); d[k] = v
    return d
ex, cur, name = parse(sys.argv[1]), parse(sys.argv[2]), sys.argv[3]
for k, v in ex.items():
    if k not in cur:
        print("  DRIFT    %s: %s missing (add it from the .example)" % (name, k))
    elif v != "CHANGEME" and "CHANGEME" not in v and cur[k] != v:
        print("  DRIFT    %s: %s differs from the .example (kept; edit by hand if intended)" % (name, k))
PY
}

# ---- 2. packages ---------------------------------------------------------------------
step "packages"
if [[ $SKIP_PKGS == 1 ]]; then say "  skipped (--skip-packages)"
else
  run apt-get update -q
  run apt-get install -y -q "${APT_PKGS[@]}"
  if ! dpkg-query -W -f '${Status}' "$NVIDIA_DRIVER_PKG" 2>/dev/null | grep -q installed; then
    run apt-get install -y -q "$NVIDIA_DRIVER_PKG"
    say "  NOTE: a new NVIDIA driver needs a reboot before llama-swap/dcgm can use the GPUs"
  fi
  if [[ ! -f /etc/apt/sources.list.d/nvidia-container-toolkit.list ]]; then
    run sh -c 'curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg'
    run sh -c 'curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed "s#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#" > /etc/apt/sources.list.d/nvidia-container-toolkit.list'
    run apt-get update -q
  fi
  run apt-get install -y -q "nvidia-container-toolkit=$NVIDIA_CTK_VERSION" "nvidia-container-toolkit-base=$NVIDIA_CTK_VERSION" \
      "libnvidia-container-tools=$NVIDIA_CTK_VERSION" "libnvidia-container1=$NVIDIA_CTK_VERSION"
  if ! grep -qs '"nvidia"' /etc/docker/daemon.json; then
    run nvidia-ctk runtime configure --runtime=docker   # adds the "nvidia" runtime (compose uses runtime: nvidia)
    CHANGED[docker]=1
  fi
fi

# ---- 3. users ------------------------------------------------------------------------
step "users"
if id llamaswap >/dev/null 2>&1; then say "  llamaswap exists"
else
  # Same IDs as live (uid 995 / gid 987) when free, so restored tarballs keep ownership (RESTORE.md).
  ids=()
  getent group 987 >/dev/null || { run groupadd --system --gid 987 llamaswap; ids=(--gid 987); }
  getent passwd 995 >/dev/null || ids+=(--uid 995)
  [[ ${ids[*]} == *--gid* ]] || ids+=(--user-group)
  run useradd --system "${ids[@]}" --home-dir /var/lib/llama-swap --shell /usr/sbin/nologin llamaswap
fi
for u in llamaswap "$BACKEND_SSH_USER"; do
  if id -nG "$u" 2>/dev/null | grep -qw docker; then :; else run usermod -aG docker "$u"; fi
done

# ---- 4. /models (XFS by UUID, nofail) ------------------------------------------------
step "models disk ($MODELS_DIR)"
FSTAB_LINE=$(cat "$STAGE/base/fstab-models")
if awk -v m="$MODELS_DIR" '$1 !~ /^#/ && $2 == m {f=1} END {exit !f}' "$T/etc/fstab" 2>/dev/null; then
  grep -qsF "UUID=$MODELS_FS_UUID" "$T/etc/fstab" || warn "/etc/fstab already mounts $MODELS_DIR with a different source; left alone"
  say "  fstab entry present"
else
  say "  add fstab: $FSTAB_LINE"
  if [[ $DRY == 0 ]]; then
    [[ -n $T ]] && install -d "$T/etc"
    [[ -f $T/etc/fstab ]] && cp -p "$T/etc/fstab" "$T/etc/fstab.bak-$(date +%Y%m%d-%H%M%S)"
    printf '%s\n' "$FSTAB_LINE" >>"$T/etc/fstab"
  fi
fi
mkdir_p "$MODELS_DIR" 0755 "$BACKEND_SSH_USER" "$BACKEND_SSH_USER"
if [[ $LIVE == 1 ]] && ! mountpoint -q "$MODELS_DIR"; then
  systemctl daemon-reload
  mount "$MODELS_DIR" || warn "could not mount $MODELS_DIR (is the disk attached and XFS-formatted? see README)"
  chown "$BACKEND_SSH_USER:$BACKEND_SSH_USER" "$MODELS_DIR"
fi
mkdir_p "$MODELS_DIR/gguf" 0775 "$BACKEND_SSH_USER" "$BACKEND_SSH_USER"
mkdir_p "$MODELS_DIR/backups" 0700 root root

# ---- 5. llama-swap binary ------------------------------------------------------------
step "llama-swap v$LLAMA_SWAP_VERSION"
if [[ -x $T/usr/local/bin/llama-swap ]] && echo "$LLAMA_SWAP_SHA256  $T/usr/local/bin/llama-swap" | sha256sum -c --quiet 2>/dev/null; then
  say "  binary present, sha256 ok"
elif [[ $LIVE == 1 ]]; then
  tmp=$(mktemp -d); curl -fsSL "$LLAMA_SWAP_URL" | tar -xz -C "$tmp" llama-swap
  echo "$LLAMA_SWAP_SHA256  $tmp/llama-swap" | sha256sum -c --quiet || die "llama-swap sha256 mismatch"
  install -m 0755 -o root -g root "$tmp/llama-swap" /usr/local/bin/llama-swap; rm -rf "$tmp"
  CHANGED[llama-swap-unit]=1; say "  installed /usr/local/bin/llama-swap"
else
  say "  would download $LLAMA_SWAP_URL and verify sha256 $LLAMA_SWAP_SHA256"
fi

# ---- 6. files ------------------------------------------------------------------------
step "files"
S=$STAGE
mkdir_p /etc/llama-swap 0750 root llamaswap
mkdir_p /var/lib/llama-swap 0755 llamaswap llamaswap
mkdir_p /srv 0755 root root
mkdir_p /srv/webui 0750 root root
mkdir_p /srv/gateway/keys 0700 root root
mkdir_p /srv/monitoring/secrets 0700 root root
mkdir_p /srv/telemetry/secrets 0700 root root

install_file "$S/base/nvidia-persistenced.service.d/override.conf" /etc/systemd/system/nvidia-persistenced.service.d/override.conf 0644 root root units
# wg0.conf holds the private key once gen-secrets has filled it: compare with the key masked.
if [[ -r $T/etc/wireguard/wg0.conf ]] && cmp -s "$S/wireguard/wg0.conf" \
     <(sed -E 's/^(PrivateKey[[:space:]]*=[[:space:]]*).*/\1@WG_PRIVATE_KEY@/' "$T/etc/wireguard/wg0.conf"); then :
else
  [[ -e $T/etc/wireguard/wg0.conf ]] && CHANGED[wg]=1
  mkdir_p /etc/wireguard 0700 root root
  install_file "$S/wireguard/wg0.conf" /etc/wireguard/wg0.conf 0600 root root
fi
install_file "$S/llama-swap/config.yaml"        /etc/llama-swap/config.yaml               0640 root llamaswap
install_file "$S/llama-swap/llama-swap.service" /etc/systemd/system/llama-swap.service    0644 root root llama-swap-unit
install_file "$S/llama-swap/spark-docker-run.sh" /usr/local/sbin/spark-docker-run.sh      0755 root root
install_file "$S/llama-swap/fetch-models.sh"    "$MODELS_DIR/fetch-models.sh"             0755 "$BACKEND_SSH_USER" "$BACKEND_SSH_USER"

install_file "$S/gateway/compose.yaml"          /srv/gateway/compose.yaml                 0644 root root gateway
install_file "$S/gateway/litellm.yaml"          /srv/gateway/litellm.yaml                 0644 root root gateway
install_file "$S/gateway/hooks/spark_hooks.py"  /srv/gateway/hooks/spark_hooks.py         0644 root root gateway
install_file "$S/gateway/.env.example"          /srv/gateway/.env.example                 0644 root root
install_file "$S/gateway/provision-keys.py"     /srv/gateway/provision-keys.py            0700 root root

install_file "$S/webui/compose.yaml"            /srv/webui/compose.yaml                   0644 root root webui
install_file "$S/webui/.env.example"            /srv/webui/.env.example                   0644 root root
install_file "$S/webui/pocket-id.env.example"   /srv/webui/pocket-id.env.example          0644 root root
install_tree "$S/webui/theme"                   /srv/webui/theme                          0644 root root webui-theme
install_file "$S/webui/pocketid-bootstrap.py"   /srv/webui/pocketid-bootstrap.py          0700 root root

for f in compose.yaml README.md blackbox/blackbox.yml prometheus/prometheus.yml llamaswap-sd/llamaswap_sd.py \
         grafana/build_spark_overview.py; do
  install_file "$S/monitoring/$f" "/srv/monitoring/$f" 0644 root root monitoring
done
install_tree "$S/monitoring/prometheus/rules" /srv/monitoring/prometheus/rules 0644 root root monitoring
install_tree "$S/monitoring/grafana/provisioning" /srv/monitoring/grafana/provisioning 0644 root root monitoring

install_file "$S/telemetry/compose.yaml"        /srv/telemetry/compose.yaml               0644 root root telemetry
install_file "$S/telemetry/README.md"           /srv/telemetry/README.md                  0644 root root
install_tree "$S/telemetry/build"               /srv/telemetry/build                      0644 root root telemetry

install_file "$S/backup/spark-backup.sh"        /usr/local/sbin/spark-backup.sh           0750 root root
install_file "$S/backup/spark-backup-restore-test.sh" /usr/local/sbin/spark-backup-restore-test.sh 0750 root root
install_file "$S/backup/spark-backup.service"   /etc/systemd/system/spark-backup.service  0644 root root units
install_file "$S/backup/spark-backup.timer"     /etc/systemd/system/spark-backup.timer    0644 root root units
install_file "$S/backup/RESTORE.md"             "$MODELS_DIR/backups/RESTORE.md"          0600 root root

install_file "$S/update-check/spark-update-check.py" /usr/local/sbin/spark-update-check.py 0750 root root
install_file "$S/update-check/spark-update-check.service" /etc/systemd/system/spark-update-check.service 0644 root root units
install_file "$S/update-check/spark-update-check.timer" /etc/systemd/system/spark-update-check.timer 0644 root root units
install_file "$S/update-check/90-spark-updates" /etc/update-motd.d/90-spark-updates      0755 root root
install_file "$S/update-check/README.md"        /var/lib/spark-update-check/README.md     0644 root root

install_file "$S/firewall/docker-user-rules.sh" /usr/local/sbin/docker-user-rules.sh      0755 root root fw-docker
install_file "$S/firewall/docker-user-rules.service" /etc/systemd/system/docker-user-rules.service 0644 root root units
install_file "$S/firewall/ufw-rules.sh"         /usr/local/sbin/ufw-rules.sh              0755 root root fw-ufw

# ---- 7. env files ----------------------------------------------------------------------
step "env files"
env_from_example /srv/gateway .env gateway/.env.example
env_from_example /srv/webui .env webui/.env.example
env_from_example /srv/webui pocket-id.env webui/pocket-id.env.example

# ---- 8. secrets ------------------------------------------------------------------------
step "secrets (scripts/gen-secrets.sh walter)"
gs_args=(walter); [[ $DRY == 1 ]] && gs_args+=(--dry-run); [[ -n $DESTDIR ]] && gs_args+=(--destdir "$DESTDIR")
"$REPO/scripts/gen-secrets.sh" "${gs_args[@]}"

# ---- 9. systemd units + firewall ---------------------------------------------------------
step "systemd + firewall"
run systemctl daemon-reload
run systemctl enable nvidia-persistenced.service wg-quick@wg0.service docker-user-rules.service docker.service \
    llama-swap.service spark-backup.timer spark-update-check.timer
run /usr/local/sbin/ufw-rules.sh
if [[ $NO_START == 0 ]]; then
  run systemctl restart nvidia-persistenced.service
  if [[ -n ${CHANGED[wg]:-} ]]; then run systemctl restart wg-quick@wg0.service
  else run systemctl start wg-quick@wg0.service; fi
  if [[ -n ${CHANGED[fw-docker]:-} ]]; then run systemctl restart docker-user-rules.service
  else run systemctl start docker-user-rules.service; fi
  if [[ -n ${CHANGED[docker]:-} ]]; then run systemctl restart docker.service; else run systemctl start docker.service; fi
  if [[ -n ${CHANGED[llama-swap-unit]:-} ]]; then run systemctl restart llama-swap.service
  else run systemctl start llama-swap.service; fi   # config.yaml changes: llama-swap --watch-config reloads itself
  run systemctl start spark-backup.timer spark-update-check.timer
  if ! compgen -G "$T$MODELS_DIR/gguf/*/*.gguf" >/dev/null; then
    warn "no GGUF files under $MODELS_DIR/gguf yet: run $MODELS_DIR/fetch-models.sh as $BACKEND_SSH_USER (see walter/models.md)"
  fi
fi

# ---- 10. compose stacks, in dependency order ---------------------------------------------
# compose_up DIR TAG...: recreate when any file of the stack changed (bind-mounted configs are
# not noticed by `up -d` alone), otherwise a no-op `up -d`.
compose_up() {
  local dir=$1 recreate=(); shift
  local t; for t in "$@"; do [[ -n ${CHANGED[$t]:-} ]] && recreate=(--force-recreate); done
  run docker compose -f "$dir/compose.yaml" up -d "${recreate[@]}" "${EXTRA_UP[@]}"
}
if [[ $NO_START == 1 ]]; then step "compose stacks: skipped (--no-start)"
else
  step "gateway (Postgres + LiteLLM)";   EXTRA_UP=(--wait); compose_up /srv/gateway gateway
  step "LiteLLM keys (HARNESS_KEYS, SPARK_USERS)"
  if [[ $LIVE == 1 ]]; then /srv/gateway/provision-keys.py
  else python3 "$S/gateway/provision-keys.py" --dry-run ${DESTDIR:+--root "$DESTDIR"} 2>&1 | sed 's/^/  /' || true; fi
  step "webui (Pocket-ID + Open WebUI)"; EXTRA_UP=(--wait); compose_up /srv/webui webui webui-theme
  step "monitoring";                     EXTRA_UP=(); compose_up /srv/monitoring monitoring
  step "telemetry";                      EXTRA_UP=(--build); compose_up /srv/telemetry telemetry
fi

# ---- 11. Hermes Agent on Walter (optional) ------------------------------------------------
if [[ $WITH_HERMES == 1 ]]; then
  step "hermes (user $BACKEND_SSH_USER)"
  H=$USER_HOME
  install_file "$S/hermes/hermes-spark" "$H/.local/bin/hermes-spark" 0755 "$BACKEND_SSH_USER" "$BACKEND_SSH_USER"
  mkdir_p "$H/.config/spark" 0700 "$BACKEND_SSH_USER" "$BACKEND_SSH_USER"
  if [[ -s $T$H/.config/spark/hermes.key ]]; then say "  keep     ~/.config/spark/hermes.key"
  elif [[ -s $T/srv/gateway/keys/hermes.key ]]; then
    say "  create   ~/.config/spark/hermes.key (copy of /srv/gateway/keys/hermes.key)"
    if [[ $DRY == 0 ]]; then
      install -m 0600 "$T/srv/gateway/keys/hermes.key" "$T$H/.config/spark/hermes.key"
      [[ $EUID -eq 0 ]] && chown "$BACKEND_SSH_USER:$BACKEND_SSH_USER" "$T$H/.config/spark/hermes.key"
    fi
  else warn "/srv/gateway/keys/hermes.key missing (is 'hermes' in HARNESS_KEYS?)"; fi
  HPY=$T$H/.hermes/hermes-agent/venv/bin/python
  if [[ -f $T$H/.hermes/config.yaml && -x $HPY ]]; then
    if [[ $DRY == 1 || -n $DESTDIR ]]; then say "  would merge hermes/config.spark.yaml into ~/.hermes/config.yaml"
    else
      rc=0; runuser -u "$BACKEND_SSH_USER" -- "$HPY" "$HERE/hermes/merge-config.py" "$S/hermes/config.spark.yaml" "$H/.hermes/config.yaml" || rc=$?
      case $rc in 0) ;; 10) say "  ~/.hermes/config.yaml up to date" ;; *) warn "hermes config merge failed ($rc)" ;; esac
    fi
  else
    warn "Hermes not installed for $BACKEND_SSH_USER; install tag v2026.9.24 (see README), then re-run with --with-hermes"
  fi
fi

step "done"
cat <<EOF
Next steps (see walter/README.md):
  - Edge: put /etc/wireguard/publickey into site.env WG_BACKEND_PUBLIC_KEY and deploy covenant.
  - Models: sudo -u $BACKEND_SSH_USER $MODELS_DIR/fetch-models.sh
  - Pocket-ID: claim the admin at https://$SPARK_ID_HOST/setup, then sudo /srv/webui/pocketid-bootstrap.py [--users FILE]
  - Optional Hermes connection in Open WebUI: put the workstation's API_SERVER_KEY in /srv/webui/hermes-gateway.key
    (root 0600) and re-run deploy (provision-keys fills OPENAI_API_KEYS).
EOF
