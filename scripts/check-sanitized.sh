#!/usr/bin/env bash
# Fail if any site-specific value or secret-looking string is about to be committed.
#
# The forbidden values themselves must never live in this public repo, so they are
# read at runtime from files that are gitignored:
#   site.env              every non-example value in it is forbidden in tracked files
#   .sanitize-extra       optional, one literal string per line (emails, user names,
#                         home IP, key-pair names, old hostnames, ...)
#
# Usage: scripts/check-sanitized.sh [--all]   (default: staged + untracked files)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

mode=${1:-}
if [[ $mode == --all ]]; then
  mapfile -t files < <(git ls-files)
else
  mapfile -t files < <( { git diff --cached --name-only --diff-filter=ACMR; git ls-files --others --exclude-standard; } | sort -u)
fi
((${#files[@]})) || { echo "check-sanitized: nothing to check"; exit 0; }

patterns=$(mktemp); trap 'rm -f "$patterns"' EXIT

# Values from site.env that differ from site.env.example (i.e. the real ones).
if [[ -f site.env ]]; then
  while IFS='=' read -r k v; do
    [[ $k =~ ^[A-Z_]+$ ]] || continue
    v=${v%%#*}; v=${v//\"/}; v=$(xargs <<<"$v")
    ex=$(sed -n "s/^$k=//p" site.env.example | sed 's/#.*//; s/"//g' | xargs)
    [[ -n $v && $v != "$ex" ]] || continue
    case $k in
      SPARK_USERS|HARNESS_KEYS) continue;;           # names: whole-word list in .sanitize-words
      *_PORT|*_IF|*_GROUP|*_CLIENT_ID|MODELS_DIR|BACKEND_SSH_USER) continue;;  # generic, not identifying
    esac
    if [[ $k == ADMIN_SOURCE_IPS ]]; then items=($v); else items=("$v"); fi
    for w in "${items[@]}"; do
      [[ ${#w} -ge 6 && ! $w =~ ^[0-9]+$ ]] || continue   # skip tiny/numeric tokens
      case $w in 10.100.0.*) continue;; esac              # WireGuard defaults are the examples
      printf '%s\n' "$w"
    done
  done < site.env >> "$patterns"
else
  echo "check-sanitized: WARNING no site.env; only generic secret patterns are checked" >&2
fi
[[ -f .sanitize-extra ]] && grep -v '^\s*\(#\|$\)' .sanitize-extra >> "$patterns" || true

fail=0
if [[ -s $patterns ]]; then
  if hits=$(grep -nFI -f "$patterns" -- "${files[@]}" 2>/dev/null); then
    echo "FORBIDDEN site-specific values found:"; echo "$hits" | cut -c1-200; fail=1
  fi
fi
# .sanitize-words (gitignored): short names matched as whole words, case-insensitive
# (e.g. real user first names that would false-positive as substrings).
if [[ -f .sanitize-words ]]; then
  if hits=$(grep -nwFIi -f <(grep -v '^\s*\(#\|$\)' .sanitize-words) -- "${files[@]}" 2>/dev/null); then
    echo "FORBIDDEN names found:"; echo "$hits" | cut -c1-200; fail=1
  fi
fi

# Generic secret shapes.
secret_re='(sk-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{10,}|(PrivateKey|PresharedKey)[[:space:]]*=[[:space:]]*[A-Za-z0-9+/]{42}[A-Za-z0-9+/=]{2}|(password|passwd|secret|api_?key|master_?key|token)[\"'"'"']?[[:space:]]*[:=][[:space:]]*[\"'"'"']?[A-Za-z0-9+_.-][A-Za-z0-9+/_.-]{19,})'
if hits=$(grep -nEIi "$secret_re" -- "${files[@]}" 2>/dev/null | grep -viE 'CHANGEME|example|placeholder|\$\{|<[a-z_-]+>|generated|os\.environ/|/run/secrets/|[A-Za-z_]+\('); then
  echo "SECRET-LOOKING strings found:"; echo "$hits" | cut -c1-200; fail=1
fi

if ((fail)); then echo "check-sanitized: FAILED"; exit 1; fi
echo "check-sanitized: OK (${#files[@]} files)"
