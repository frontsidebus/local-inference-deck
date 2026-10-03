#!/usr/bin/env bash
# clients/install.sh -- install the Spark harness wrappers and render their configs
# for the CURRENT user. Never needs root. Never touches ~/.claude or ~/.claude.json
# (claude-spark uses its own config dir, ~/.claude-spark).
#
# Without --force an existing, different file is left alone: the new version is
# written next to it as <file>.spark and a diff is printed. With --force the old
# file is backed up to <file>.bak-<timestamp> and replaced.
#
# Usage: clients/install.sh [options]
#   --site-env FILE        site settings (default: <repo>/site.env if present)
#   --api-host HOST        override SPARK_API_HOST (e.g. api.example.com)
#   --key-dir DIR          where the <harness>.key files live (default: ~/.config/spark)
#   --with-hermes-gateway  also install the optional hermes-gateway user service
#   --force                replace differing files (with backups) instead of writing .spark sidecars
#   --dry-run              print what would change; write nothing
#   -h, --help
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"

site_env="" api_host="" key_dir="" force=0 dry=0 gateway=0
while (($#)); do
  case $1 in
    --site-env) site_env=$2; shift 2 ;;
    --api-host) api_host=$2; shift 2 ;;
    --key-dir) key_dir=$2; shift 2 ;;
    --with-hermes-gateway) gateway=1; shift ;;
    --force) force=1; shift ;;
    --dry-run) dry=1; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; exit 2 ;;
  esac
done

# ---- settings: flag > environment > site.env > default ----------------------
[[ -z $site_env && -f $repo/site.env ]] && site_env=$repo/site.env
site_get() {  # site_get VAR -> value from site.env (sourced in a subshell)
  [[ -n $site_env ]] || return 0
  ( set +u; unset "$1"; . "$site_env" >/dev/null 2>&1; printf '%s' "${!1-}" )
}
SPARK_API_HOST=${api_host:-${SPARK_API_HOST:-$(site_get SPARK_API_HOST)}}
HYPERVISOR_BRIDGE_IP=${HYPERVISOR_BRIDGE_IP:-$(site_get HYPERVISOR_BRIDGE_IP)}
HERMES_GATEWAY_PORT=${HERMES_GATEWAY_PORT:-$(site_get HERMES_GATEWAY_PORT)}
HERMES_GATEWAY_PORT=${HERMES_GATEWAY_PORT:-8642}
SPARK_KEY_DIR=${key_dir:-${SPARK_KEY_DIR:-$HOME/.config/spark}}
SPARK_KEY_DIR=${SPARK_KEY_DIR%/}
# For configs that expand "~" themselves (OpenCode {file:}), keep the short form.
if [[ $SPARK_KEY_DIR == "$HOME"/* ]]; then SPARK_KEY_DIR_TILDE="~${SPARK_KEY_DIR#"$HOME"}"; else SPARK_KEY_DIR_TILDE=$SPARK_KEY_DIR; fi
export SPARK_API_HOST SPARK_KEY_DIR SPARK_KEY_DIR_TILDE HYPERVISOR_BRIDGE_IP HERMES_GATEWAY_PORT HOME

if [[ -z $SPARK_API_HOST || $SPARK_API_HOST == */* ]]; then
  echo "install.sh: set SPARK_API_HOST (bare host name, e.g. api.example.com) via --api-host, the environment or site.env" >&2
  exit 1
fi
if ((gateway)) && [[ -z $HYPERVISOR_BRIDGE_IP ]]; then
  echo "install.sh: --with-hermes-gateway needs HYPERVISOR_BRIDGE_IP (site.env or environment)" >&2
  exit 1
fi
command -v envsubst >/dev/null || { echo "install.sh: envsubst not found (apt install gettext-base)" >&2; exit 1; }

VARS='${SPARK_API_HOST} ${SPARK_KEY_DIR} ${SPARK_KEY_DIR_TILDE} ${HOME} ${HYPERVISOR_BRIDGE_IP} ${HERMES_GATEWAY_PORT}'
ts=$(date +%Y%m%d-%H%M%S)
changed=0 sidecars=()

say() { printf '%s\n' "$*"; }
# render SRC -> stdout (explicit variable list; never bare envsubst)
render() { envsubst "$VARS" < "$1"; }

guard() {  # refuse anything that could touch the normal Claude Code config
  case $1 in
    "$HOME/.claude"|"$HOME/.claude/"*|"$HOME/.claude.json"*)
      echo "install.sh: BUG: refusing to write $1 (normal claude config)" >&2; exit 3 ;;
  esac
}

write_file() {  # write_file DEST MODE  (content in $content)
  local dest=$1 mode=$2 dir
  dir=$(dirname "$dest")
  [[ -d $dir ]] || mkdir -p "$dir"
  ( umask 077; printf '%s' "$content" > "$dest.tmp.$$" )
  chmod "$mode" "$dest.tmp.$$"
  mv -f "$dest.tmp.$$" "$dest"
}

# place DEST MODE  -- content in $content (exact bytes)
place() {
  local dest=$1 mode=$2
  guard "$dest"
  if [[ ! -e $dest ]]; then
    say "  new        $dest"
    ((dry)) || write_file "$dest" "$mode"
    changed=1
  elif cmp -s "$dest" <(printf '%s' "$content"); then
    say "  unchanged  $dest"
  elif ((force)); then
    say "  replace    $dest  (backup: $dest.bak-$ts)"
    diff -u "$dest" <(printf '%s' "$content") --label "$dest (current)" --label "$dest (new)" || true
    if ! ((dry)); then cp -p "$dest" "$dest.bak-$ts"; write_file "$dest" "$mode"; rm -f "$dest.spark"; fi
    changed=1
  else
    say "  DIFFERS    $dest  -> new version in $dest.spark (use --force to replace)"
    diff -u "$dest" <(printf '%s' "$content") --label "$dest (current)" --label "$dest (new)" || true
    ((dry)) || write_file "$dest.spark" 644
    sidecars+=("$dest.spark")
  fi
}

# load content from a template/verbatim file, keeping trailing newlines exactly
load_render() { content=$(render "$1"; printf x); content=${content%x}; }
load_copy()   { content=$(cat "$1"; printf x); content=${content%x}; }

((dry)) && say "DRY RUN: nothing will be written."
say "SPARK_API_HOST=$SPARK_API_HOST  SPARK_KEY_DIR=$SPARK_KEY_DIR  HOME=$HOME"

# ---- 1. key dir + runtime env file ----------------------------------------
say "[spark] $SPARK_KEY_DIR"
if [[ ! -d $SPARK_KEY_DIR ]] && ! ((dry)); then mkdir -p "$SPARK_KEY_DIR"; chmod 700 "$SPARK_KEY_DIR"; fi
load_render "$here/spark-env.tmpl";            place "$HOME/.config/spark/env" 600
for k in claude-code codex hermes opencode; do   # never read key contents, only check presence/mode
  f=$SPARK_KEY_DIR/$k.key
  if [[ ! -e $f ]]; then say "  MISSING    $f  (see README: Getting a key)"
  elif [[ $(stat -c %a "$f") != 600 ]]; then say "  WARN       $f is mode $(stat -c %a "$f"); run: chmod 600 $f"
  else say "  key ok     $f"; fi
done

# ---- 2. wrappers ------------------------------------------------------------
say "[wrappers] $HOME/.local/bin"
for w in claude-spark codex-spark hermes-spark; do
  load_copy "$here/bin/$w";                    place "$HOME/.local/bin/$w" 755
done

# ---- 3. Claude Code (isolated config dir only) ------------------------------
say "[claude] $HOME/.claude-spark"
if [[ ! -d $HOME/.claude-spark ]] && ! ((dry)); then mkdir -p "$HOME/.claude-spark"; chmod 700 "$HOME/.claude-spark"; fi
load_copy "$here/claude/settings.json";        place "$HOME/.claude-spark/settings.json" 600

# ---- 4. Codex ---------------------------------------------------------------
say "[codex] $HOME/.codex"
load_render "$here/codex/config.toml.tmpl";    place "$HOME/.codex/config.toml" 600
for f in spark.config.toml spark-fast.config.toml spark-models.json; do
  load_copy "$here/codex/$f";                  place "$HOME/.codex/$f" 600
done
if [[ $(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0) == 1 && ! -e /etc/apparmor.d/bwrap ]]; then
  say "  NOTE: Codex's bubblewrap sandbox is blocked by AppArmor here. As root:"
  say "        install -m 644 $here/codex/apparmor-bwrap /etc/apparmor.d/bwrap && apparmor_parser -r /etc/apparmor.d/bwrap"
fi

# ---- 5. OpenCode --------------------------------------------------------------
say "[opencode] $HOME/.config/opencode"
load_render "$here/opencode/opencode.json.tmpl"; place "$HOME/.config/opencode/opencode.json" 644

# ---- 6. Hermes (fragment merged into a personal config) ----------------------
say "[hermes] $HOME/.hermes"
hcfg=$HOME/.hermes/config.yaml
load_render "$here/hermes/config.spark.yaml.tmpl"
fragment=$content
py=$HOME/.hermes/hermes-agent/venv/bin/python
[[ -x $py ]] || py=python3
merge_py='
import sys, yaml
cur = yaml.safe_load(open(sys.argv[1])) or {}
frag = yaml.safe_load(sys.stdin.read())
out = dict(cur)
for k in ("model", "security"):
    out[k] = {**(cur.get(k) or {}), **frag[k]}
provs = [p for p in (cur.get("custom_providers") or []) if p.get("name") != "spark"]
out["custom_providers"] = provs + frag["custom_providers"]
if out == cur:
    sys.exit(10)                      # already merged
sys.stdout.write(yaml.safe_dump(out, sort_keys=False, allow_unicode=True, default_flow_style=False))
'
if [[ ! -e $hcfg ]]; then
  place "$hcfg" 600                    # fresh install: the fragment is the whole config
elif ! "$py" -c 'import yaml' 2>/dev/null; then
  say "  DIFFERS?   $hcfg  (no PyYAML to compare) -> fragment in $hcfg.spark; merge by hand"
  ((dry)) || write_file "$hcfg.spark" 644
  sidecars+=("$hcfg.spark")
else
  rc=0
  merged=$(printf '%s' "$fragment" | "$py" -c "$merge_py" "$hcfg"; r=$?; printf x; exit $r) || rc=$?
  merged=${merged%x}
  if ((rc == 10)); then
    say "  unchanged  $hcfg  (spark keys already present)"
  elif ((rc != 0)); then
    say "  ERROR      could not parse $hcfg; fragment in $hcfg.spark"; ((dry)) || write_file "$hcfg.spark" 644
  elif ((force)); then
    say "  merge      $hcfg  (backup: $hcfg.bak-$ts; comments in the file are not preserved)"
    if ! ((dry)); then cp -p "$hcfg" "$hcfg.bak-$ts"; content=$merged; write_file "$hcfg" 600; rm -f "$hcfg.spark"; fi
    changed=1
  else
    say "  DIFFERS    $hcfg  -> fragment in $hcfg.spark; merge the model:, custom_providers: and"
    say "             security.redact_secrets keys by hand, or re-run with --force (backs up, rewrites)"
    ((dry)) || write_file "$hcfg.spark" 644
    sidecars+=("$hcfg.spark")
  fi
fi

# ---- 7. optional hermes-gateway user service ---------------------------------
if ((gateway)); then
  say "[hermes-gateway] bind ${HYPERVISOR_BRIDGE_IP}:${HERMES_GATEWAY_PORT}"
  load_copy "$here/hermes/hermes-gateway.service"; place "$HOME/.config/systemd/user/hermes-gateway.service" 644
  henv=$HOME/.hermes/.env
  want=$(render "$here/hermes/gateway.env.example.tmpl" | grep -E '^[A-Z_]+=')
  missing=()
  while IFS= read -r line; do         # check NAMES only; never print existing values
    name=${line%%=*}
    [[ -f $henv ]] && grep -q "^${name}=" "$henv" && continue
    missing+=("$line")
  done <<<"$want"
  if ((${#missing[@]} == 0)); then
    say "  unchanged  $henv  (all gateway variables present; values not checked)"
  elif ((force)); then
    say "  append     $henv: ${missing[*]%%=*}"
    if ! ((dry)); then
      [[ -e $henv ]] && cp -p "$henv" "$henv.bak-$ts"
      ( umask 077
        printf '\n# hermes-gateway (added by clients/install.sh %s)\n' "$ts"
        for line in "${missing[@]}"; do
          if [[ $line == API_SERVER_KEY=* ]]; then printf 'API_SERVER_KEY=%s\n' "$(openssl rand -hex 32)"
          else printf '%s\n' "$line"; fi
        done ) >> "$henv"
      chmod 600 "$henv"
      say "  API_SERVER_KEY was generated into $henv; copy it into the backend's OPENAI_API_KEYS yourself."
    fi
  else
    say "  MISSING in $henv (add them, or re-run with --force to append; API_SERVER_KEY: openssl rand -hex 32):"
    printf '    %s\n' "${missing[@]}"
  fi
  say "  enable:  loginctl enable-linger \"\$USER\"; systemctl --user daemon-reload && systemctl --user enable --now hermes-gateway"
fi

# ---- summary ------------------------------------------------------------------
say ""
((${#sidecars[@]})) && { say "Review and merge these sidecars (or re-run with --force):"; printf '  %s\n' "${sidecars[@]}"; }
for b in claude codex hermes opencode; do
  [[ -x $HOME/.local/bin/$b ]] || command -v "$b" >/dev/null || say "note: '$b' binary not found; install it before using the matching wrapper (see README)."
done
((dry)) && say "DRY RUN complete: nothing was written." || say "Done. Smoke tests: see clients/README.md."
exit 0
