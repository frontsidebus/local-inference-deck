#!/usr/bin/env bash
# gen-secrets.sh -- generate a component's secrets ON THE TARGET HOST.
#
# Usage: scripts/gen-secrets.sh <component> [--dry-run] [--destdir DIR]
#   component   name of a file in scripts/secrets.d/<component>.sh (walter, covenant, ...)
#   --dry-run   report what would be created; write nothing (also: DRY_RUN=1)
#   --destdir   operate under a prefix instead of / (local tests; also: DESTDIR=...)
#
# Contract (every secrets.d file must honour it):
#   - NEVER overwrite an existing secret. Existing files / env values are kept as is.
#   - NEVER print a secret value (names, paths and modes only).
#   - Idempotent: a second run changes nothing.
#
# secrets.d files are sourced (bash) with DESTDIR, DRY_RUN, REPO and the helpers below
# available. They may also be self-contained (covenant.sh defines its own helpers).
#
# Helpers (all paths are absolute target paths; DESTDIR is prepended internally):
#   gs_file PATH MODE OWNER GROUP CMD...    create PATH from CMD's stdout if PATH is absent/empty
#   gs_copy SRC DST MODE OWNER GROUP        create DST as a copy of secret SRC if DST is absent
#   gs_env  FILE VAR CMD...                 in env file FILE, replace "VAR=CHANGEME" with CMD's
#                                           stdout; never touches a VAR that has another value
#   gs_env_from_file FILE VAR SRC           same, value = contents of secret file SRC
#   gs_placeholder FILE TOKEN SRC           replace a literal TOKEN (e.g. @WG_PRIVATE_KEY@) in FILE
#                                           with the contents of secret file SRC
#   gs_rand_hex N | gs_rand_b64url N | gs_rand_alnum N   generators (N bytes / chars)
#   gs_note MSG                             log to stderr
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export REPO

component=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run) export DRY_RUN=1 ;;
    --destdir) export DESTDIR=${2:?--destdir needs a directory}; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    -*) echo "gen-secrets: unknown option $1" >&2; exit 2 ;;
    *) component=$1 ;;
  esac
  shift
done
[[ -n $component ]] || { echo "usage: gen-secrets.sh <component> [--dry-run] [--destdir DIR]" >&2; exit 2; }
[[ $component =~ ^[a-z0-9_-]+$ ]] || { echo "gen-secrets: bad component name" >&2; exit 2; }
export DESTDIR=${DESTDIR:-}
export DRY_RUN=${DRY_RUN:-0}
DESTDIR=${DESTDIR%/}

SPEC=$REPO/scripts/secrets.d/$component.sh
[[ -f $SPEC ]] || { echo "gen-secrets: no secrets spec $SPEC" >&2; exit 1; }

if [[ -z $DESTDIR && $DRY_RUN != 1 && $EUID -ne 0 ]]; then
  echo "gen-secrets: must run as root on the target host (or use --destdir/--dry-run)" >&2; exit 1
fi

# --------------------------------------------------------------------------- helpers
gs_note() { printf 'secrets[%s]: %s\n' "$component" "$*" >&2; }

gs_rand_hex()    { openssl rand -hex "${1:-32}"; }
gs_rand_b64url() { openssl rand -base64 "${1:-32}" | tr -- '+/' '-_' | tr -d '=\n'; echo; }
gs_rand_alnum()  { local n=${1:-32} s=""; while ((${#s} < n)); do s+=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9'); done; printf '%s\n' "${s:0:n}"; }

# chown only when we can (root); under a non-root DESTDIR test it is reported and skipped.
_gs_chown() { # OWNER GROUP PATH
  if [[ $EUID -eq 0 ]]; then chown "$1:$2" "$3"
  else gs_note "        (not root: skipped chown $1:$2 on ${3#"$DESTDIR"})"; fi
}

gs_file() { # PATH MODE OWNER GROUP CMD...
  local rel=$1 mode=$2 owner=$3 group=$4; shift 4
  local path=$DESTDIR$rel
  if [[ -s $path ]]; then gs_note "keep    $rel (exists)"; return 0; fi
  if [[ $DRY_RUN == 1 ]]; then gs_note "would create $rel ($mode $owner:$group)"; return 0; fi
  install -d -m 0755 "$(dirname "$path")"
  local tmp; tmp=$(mktemp "$(dirname "$path")/.gs.XXXXXX")
  chmod 600 "$tmp"
  if ! "$@" >"$tmp" || [[ ! -s $tmp ]]; then rm -f "$tmp"; gs_note "FAILED  $rel"; return 1; fi
  chmod "$mode" "$tmp"; _gs_chown "$owner" "$group" "$tmp"
  mv -n "$tmp" "$path"; rm -f "$tmp"
  gs_note "created $rel ($mode $owner:$group)"
}

gs_copy() { # SRC DST MODE OWNER GROUP
  local src=$DESTDIR$1
  if [[ ! -s $src ]]; then
    if [[ $DRY_RUN == 1 ]]; then gs_note "would copy $1 -> $2 ($3 $4:$5) once $1 exists"; return 0; fi
    gs_note "MISSING $1 (needed for $2)"; return 1
  fi
  if [[ -s $DESTDIR$2 ]] && ! cmp -s "$src" "$DESTDIR$2"; then
    gs_note "WARN    $2 differs from $1 (kept; re-copy by hand after a rotation)"
  fi
  gs_file "$2" "$3" "$4" "$5" cat "$src"
}

# value of VAR in env file FILE (raw, after the first '=')
_gs_env_get() { sed -n "s/^$2=//p" "$1" | head -n1; }

gs_env() { # FILE VAR CMD...
  local rel=$1 var=$2; shift 2
  local f=$DESTDIR$rel
  if [[ ! -f $f ]]; then
    if [[ $DRY_RUN == 1 ]]; then gs_note "would set $var in $rel (file not installed yet)"; return 0; fi
    gs_note "MISSING env file $rel"; return 1
  fi
  local cur; cur=$(_gs_env_get "$f" "$var")
  if ! grep -q "^$var=" "$f"; then gs_note "WARN    $var not present in $rel (not added)"; return 0; fi
  if [[ $cur != CHANGEME ]]; then gs_note "keep    $var in $rel (set)"; return 0; fi
  if [[ $DRY_RUN == 1 ]]; then gs_note "would generate $var in $rel"; return 0; fi
  local val; val=$("$@") || { gs_note "FAILED  $var"; return 1; }
  [[ -n $val && $val != *$'\n'* ]] || { gs_note "FAILED  $var (empty/multi-line)"; return 1; }
  _gs_replace_line "$f" "$var=CHANGEME" "$var=$val"
  gs_note "set     $var in $rel"
}

gs_env_from_file() { # FILE VAR SRC
  local src=$DESTDIR$3
  if [[ ! -s $src ]]; then
    if [[ $DRY_RUN == 1 ]]; then gs_note "would set $2 in $1 from $3"; return 0; fi
    gs_note "MISSING $3 (for $2 in $1)"; return 1
  fi
  gs_env "$1" "$2" _gs_read_secret "$src"
}

gs_placeholder() { # FILE TOKEN SRC
  local f=$DESTDIR$1 src=$DESTDIR$3
  if [[ $DRY_RUN == 1 ]]; then gs_note "would fill $2 in $1 from $3 (if still a placeholder)"; return 0; fi
  [[ -f $f ]] || { gs_note "MISSING $1"; return 1; }
  if ! grep -qF "$2" "$f"; then gs_note "keep    $2 in $1 (already filled)"; return 0; fi
  [[ -s $src ]] || { gs_note "MISSING $3 (for $2 in $1)"; return 1; }
  local val; val=$(_gs_read_secret "$src")
  _gs_replace_literal "$f" "$2" "$val"
  gs_note "filled  $2 in $1 from $3"
}

_gs_read_secret() { tr -d '[:space:]' <"$1"; echo; }

# In-place line/literal replacement without putting the secret on a command line
# (values are passed through the environment to a python one-liner, never argv).
_gs_replace_line() { # FILE OLDLINE NEWLINE
  GS_F=$1 GS_OLD=$2 GS_NEW=$3 python3 - <<'PY'
import os
f, old, new = os.environ["GS_F"], os.environ["GS_OLD"], os.environ["GS_NEW"]
st = os.stat(f)
lines = open(f).read().split("\n")
done = False
for i, l in enumerate(lines):
    if not done and l == old:
        lines[i] = new; done = True
tmp = f + ".gs-tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o7777)
with os.fdopen(fd, "w") as o: o.write("\n".join(lines))
try: os.chown(tmp, st.st_uid, st.st_gid)
except PermissionError: pass
os.replace(tmp, f)
PY
}
_gs_replace_literal() { # FILE TOKEN VALUE
  GS_F=$1 GS_OLD=$2 GS_NEW=$3 python3 - <<'PY'
import os
f, old, new = os.environ["GS_F"], os.environ["GS_OLD"], os.environ["GS_NEW"]
st = os.stat(f)
s = open(f).read().replace(old, new)
tmp = f + ".gs-tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o7777)
with os.fdopen(fd, "w") as o: o.write(s)
try: os.chown(tmp, st.st_uid, st.st_gid)
except PermissionError: pass
os.replace(tmp, f)
PY
}

command -v openssl >/dev/null || { gs_note "openssl not found"; exit 1; }
command -v python3 >/dev/null || { gs_note "python3 not found"; exit 1; }

[[ $DRY_RUN == 1 ]] && gs_note "dry run: nothing will be written"
[[ -n $DESTDIR ]] && gs_note "destdir: $DESTDIR"
umask 077
# shellcheck disable=SC1090
. "$SPEC"
gs_note "done"
