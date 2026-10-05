#!/usr/bin/env bash
# render.sh -- render *.tmpl files with site values from site.env.
#
# Usage:
#   scripts/render.sh [-e SITE_ENV] [-x GLOB]... SRC DST
#     SRC is a file or a directory.
#       file  *.tmpl   -> rendered to DST (DST is the output file path)
#       file  other    -> copied verbatim to DST
#       dir            -> walked recursively; every *.tmpl is rendered to the same
#                         relative path under DST minus ".tmpl", other files copied
#                         verbatim (mode bits preserved).
#     -x GLOB  (directory SRC only, repeatable) skip files whose name matches GLOB, e.g. the
#              files of an optional feature that is off.
#   scripts/render.sh [-e SITE_ENV] --vars       print the variable list and exit
#   scripts/render.sh [-e SITE_ENV] --check SRC  list site vars used by SRC that are empty
#
# Site env: -e FILE, else $SITE_ENV, else <repo>/site.env.
#
# Substitution uses envsubst with an EXPLICIT variable list: only the names that are
# defined in site.env.example (the contract) or site.env are replaced. Everything else
# (${PORT} in llama-swap, $host in nginx, ${POSTGRES_PASSWORD:?} in compose files,
# shell variables in scripts) passes through untouched. Templates must use ${NAME}.
# A template that references a site variable which is unset or empty is an error.
#
# Optional blocks: a template line containing @@if NAME@@ starts a block that is kept only when
# site variable NAME is set (non-empty and not CHANGEME); @@else@@ and @@endif@@ work as expected
# (no nesting). The directive lines themselves are always dropped, so wrap them in the file's
# comment syntax for readability, e.g. "<!-- @@if RESTIC_BUCKET@@ -->". Variables inside a
# dropped block are not required. Templates without @@if are rendered byte for byte as before.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SITE_ENV_FILE=${SITE_ENV:-$REPO/site.env}

die() { echo "render: $*" >&2; exit 1; }

EXCLUDES=()
while [[ ${1:-} == -e || ${1:-} == -x ]]; do
  [[ -n ${2:-} ]] || die "$1 needs an argument"
  if [[ $1 == -e ]]; then SITE_ENV_FILE=$2; else EXCLUDES+=("$2"); fi
  shift 2
done
command -v envsubst >/dev/null || die "envsubst not found (apt-get install gettext-base)"
[[ -f $SITE_ENV_FILE ]] || die "site env not found: $SITE_ENV_FILE (copy site.env.example to site.env)"

# Variable names: the union of names assigned in site.env.example and site.env.
mapfile -t VARS < <(cat "$REPO/site.env.example" "$SITE_ENV_FILE" 2>/dev/null \
  | sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Z][A-Z0-9_]*)=.*/\2/p' | sort -u)
((${#VARS[@]})) || die "no variables found in site.env(.example)"

# Load values (site.env is shell syntax: KEY=value, quotes for lists) and export them.
set -a
# shellcheck disable=SC1090
. "$SITE_ENV_FILE"
set +a

SHELL_FORMAT=$(printf '${%s} ' "${VARS[@]}")

# Names of site vars referenced as ${NAME} in a file.
used_vars() {
  grep -oE '\$\{[A-Z][A-Z0-9_]*\}' "$1" 2>/dev/null | tr -d '${}' | sort -u \
    | grep -Fxf <(printf '%s\n' "${VARS[@]}") || true
}

check_file() { # FILE [LABEL]: prints "LABEL: NAME" for every referenced-but-empty site var
  local v bad=0
  for v in $(used_vars "$1"); do
    if [[ -z ${!v:-} ]]; then echo "${2:-$1}: \${$v} is unset/empty in $SITE_ENV_FILE" >&2; bad=1; fi
  done
  return $bad
}

# Optional blocks (@@if NAME@@ / @@else@@ / @@endif@@): see the header.
is_on() { [[ -n ${!1:-} && ${!1} != CHANGEME ]]; }
COND_TMP=$(mktemp -d "${TMPDIR:-/tmp}/render-cond.XXXXXX")   # created here: cond_filter runs in $(...)
trap 'rm -rf "$COND_TMP"' EXIT
cond_filter() { # SRC -> prints the path of the template to use (SRC itself when it has no blocks)
  local src=$1 out v on=" "
  if ! grep -qE '@@(if [A-Z][A-Z0-9_]*|else|endif)@@' "$src"; then printf '%s\n' "$src"; return 0; fi
  out=$(mktemp "$COND_TMP/t.XXXXXX")
  for v in $(grep -oE '@@if [A-Z][A-Z0-9_]*@@' "$src" | sed -E 's/@@if (.*)@@/\1/' | sort -u); do
    if is_on "$v"; then on+="$v "; fi
  done
  awk -v on="$on" -v f="$src" '
    function bad(m) { err = m; exit 1 }
    match($0, /@@if [A-Z][A-Z0-9_]*@@/) {
      if (depth) bad("nested @@if at line " NR)
      depth = 1; keep = index(on, " " substr($0, RSTART + 5, RLENGTH - 7) " ") > 0; next
    }
    /@@else@@/  { if (!depth) bad("@@else@@ without @@if at line " NR); keep = !keep; next }
    /@@endif@@/ { if (!depth) bad("@@endif@@ without @@if at line " NR); depth = 0; next }
    !depth || keep { print }
    END {
      if (err == "" && depth) err = "unterminated @@if"
      if (err != "") { print "render: " f ": " err > "/dev/stderr"; exit 1 }
    }' "$src" >"$out" || return 1
  printf '%s\n' "$out"
}

render_file() { # SRC DST
  local src=$1 dst=$2 in
  mkdir -p "$(dirname "$dst")"
  if [[ $src == *.tmpl ]]; then
    in=$(cond_filter "$src") || die "bad @@if block in $src"
    check_file "$in" "$src" || die "refusing to render $src with missing values"
    envsubst "$SHELL_FORMAT" <"$in" >"$dst.render.$$"
    chmod --reference="$src" "$dst.render.$$"
    mv -f "$dst.render.$$" "$dst"
  else
    cp -p "$src" "$dst"
  fi
}

case ${1:-} in
  --vars) printf '%s\n' "${VARS[@]}"; exit 0 ;;
  --check)
    [[ -e ${2:-} ]] || die "usage: render.sh --check SRC"
    rc=0
    while IFS= read -r -d '' f; do
      in=$(cond_filter "$f") || { rc=1; continue; }
      check_file "$in" "$f" || rc=1
    done < <(find "$2" -type f -name '*.tmpl' -print0)
    exit $rc ;;
esac

[[ $# -eq 2 ]] || die "usage: render.sh [-e SITE_ENV] [-x GLOB]... SRC DST"
SRC=$1 DST=$2
[[ -e $SRC ]] || die "no such source: $SRC"

if [[ -d $SRC ]]; then
  mkdir -p "$DST"
  skip=()
  for g in "${EXCLUDES[@]}"; do skip+=(-not -name "$g"); done
  while IFS= read -r -d '' f; do
    rel=${f#"$SRC"/}
    out=$DST/${rel%.tmpl}
    render_file "$f" "$out"
  done < <(find "$SRC" -type f -not -name '*.swp' "${skip[@]}" -print0)
else
  render_file "$SRC" "$DST"
fi
