#!/usr/bin/env bash
# Fail if any site-specific value or secret-looking string is about to be committed.
#
# The forbidden values themselves must never live in this public repo, so they are
# read at runtime from files that are gitignored or outside the checkout:
#   site env         every non-example value in it is forbidden in tracked files. First found of:
#                      --site-env FILE  >  $SITE_ENV  >  ~/.config/deck/site.env  >  <repo>/site.env
#   judge.env        when present ($JUDGE_ENV, else ~/.config/judge/judge.env): checked under the
#                    same per-key policy, so a value that lives only there is still caught
#   .sanitize-extra  optional, one literal string per line (emails, user names, home IP,
#                    key-pair names, old hostnames, ...); read from ~/.config/deck/ and the repo root
#   .sanitize-words  optional, whole words matched case-insensitively (real first names);
#                    read from ~/.config/deck/ and the repo root
# The repo checked: --repo DIR, else the git work tree of the current directory (so one repo's script can
# check another checkout when run from there). The baseline, policy and repo-root files are read from that
# repo, never from the repo this script is vendored in.
# Example baseline (a value equal to its example default is public), first found of:
#   --keys FILE  >  <repo>/contracts/site-keys.yaml (`default` of each active key)  >  the union of
#   <repo>/*.env.example. FILE is a registry like contracts/site-keys.yaml when it ends in .yaml/.yml, else an
#   env-style example. With none of them, every site value counts as real (stricter, never looser), and a
#   note on stderr says so.
# Per-key policy (which keys are checked): <repo>/contracts/sanitize-policy.yaml when present (generated from
# contracts/site-keys.yaml by tools/site_keys.py), else the built-in table in builtin_action below. Both give
# every key the same action; a malformed policy file is an error (exit 2), never a silent fallback.
#
# Usage: check-sanitized.sh [--all | --history] [--regex-only] [--site-env FILE] [--repo DIR] [--keys FILE]
#   (deck-tools engine; in a repo, run it through its scripts/check-sanitized.sh shim)
#   (default)      staged + untracked files (working-tree contents)
#   --all          every tracked file (working-tree contents of `git ls-files`)
#   --history      every commit (git rev-list --all): real values, .sanitize-extra and
#                  .sanitize-words. Hits are reported as commit, path and key name; the matched
#                  value is never printed.
#   --regex-only   only the generic secret-shape regexes, no real values. CI only (CI=true).
#
# Exit codes: 0 clean, 1 finding(s), 2 no real-value source found, a dangling symlink, or a
# refused/invalid invocation. The message for 2 names the paths that were tried.
set -euo pipefail

die2() { echo "check-sanitized: $*" >&2; exit 2; }
abs() { [[ $1 == /* ]] && printf '%s\n' "$1" || printf '%s\n' "$PWD/$1"; }
# A dangling symlink is an error, never a silent fall-through: it means the operator's pointer is broken.
dangling() { [[ -L $1 && ! -e $1 ]]; }

mode=staged regex_only=0 site_flag="" repo_flag="" keys_flag=""
while (($#)); do
  case $1 in
    --all) mode=all ;;
    --history) mode=history ;;
    --regex-only) regex_only=1 ;;
    --site-env) [[ -n ${2:-} ]] || die2 "--site-env needs a file"; site_flag=$(abs "$2"); shift ;;
    --site-env=?*) site_flag=$(abs "${1#--site-env=}") ;;
    --repo) [[ -n ${2:-} ]] || die2 "--repo needs a directory"; repo_flag=$(abs "$2"); shift ;;
    --repo=?*) repo_flag=$(abs "${1#--repo=}") ;;
    --keys) [[ -n ${2:-} ]] || die2 "--keys needs a file"; keys_flag=$(abs "$2"); shift ;;
    --keys=?*) keys_flag=$(abs "${1#--keys=}") ;;
    -h|--help) sed -n '2,/^set -euo pipefail/{/^set -euo pipefail/!p}' "$0"; exit 0 ;;
    *) die2 "unknown argument: $1 (usage: check-sanitized.sh [--all|--history] [--regex-only] [--site-env FILE])" ;;
  esac
  shift
done
# Relative $SITE_ENV / $JUDGE_ENV are relative to the caller's directory, not the repo root.
[[ -z ${SITE_ENV:-} ]] || SITE_ENV=$(abs "$SITE_ENV")
[[ -z ${JUDGE_ENV:-} ]] || JUDGE_ENV=$(abs "$JUDGE_ENV")
if [[ -n $repo_flag ]]; then
  [[ -d $repo_flag ]] || die2 "--repo: not a directory: $repo_flag"
  top=$(git -C "$repo_flag" rev-parse --show-toplevel 2>/dev/null) || die2 "--repo: not a git work tree: $repo_flag"
  cd "$top"
else
  cd "$(git rev-parse --show-toplevel)"
fi
if [[ -n $keys_flag ]]; then
  dangling "$keys_flag" && die2 "--keys is a dangling symlink: $keys_flag"
  [[ -f $keys_flag ]] || die2 "--keys: no such file: $keys_flag"
fi

if ((regex_only)); then
  [[ ${CI:-} == true ]] || die2 "--regex-only checks no real values and is refused outside CI (CI=true)"
  [[ $mode != history ]] || die2 "--regex-only and --history cannot be combined"
fi

# ---- real-value sources ---------------------------------------------------------------------
# A candidate is usable when it is a regular file (or a symlink to one). A dangling symlink is an
# error, never a silent fall-through (dangling() is defined at the top).

resolve_site_env() {
  local c how
  if [[ -n $site_flag || -n ${SITE_ENV:-} ]]; then
    if [[ -n $site_flag ]]; then c=$site_flag how=--site-env; else c=$SITE_ENV how=\$SITE_ENV; fi
    dangling "$c" && die2 "site env is a dangling symlink: $c (from $how)"
    [[ -f $c ]] || die2 "site env not found: $c (from $how)"
    printf '%s\n' "$c"; return 0
  fi
  local tried=()
  for c in "${HOME:-/nonexistent}/.config/deck/site.env" "$PWD/site.env"; do
    tried+=("$c")
    dangling "$c" && die2 "site env is a dangling symlink: $c"
    [[ -f $c ]] && { printf '%s\n' "$c"; return 0; }
  done
  die2 "no real-value source found; tried: --site-env (not given), \$SITE_ENV (unset), ${tried[*]}." \
       "Create ~/.config/deck/site.env (for example a symlink to your site.env)."
}

resolve_judge_env() { # prints the judge.env path, or nothing when there is none
  local c
  if [[ -n ${JUDGE_ENV:-} ]]; then
    c=$JUDGE_ENV
    dangling "$c" && die2 "judge env is a dangling symlink: $c (from \$JUDGE_ENV)"
    [[ -f $c ]] || die2 "judge env not found: $c (from \$JUDGE_ENV)"
    printf '%s\n' "$c"; return 0
  fi
  c=${HOME:-/nonexistent}/.config/judge/judge.env
  dangling "$c" && die2 "judge env is a dangling symlink: $c"
  [[ ! -f $c ]] || printf '%s\n' "$c"
}

extra_files() { # NAME: ~/.config/deck/NAME and <repo>/NAME (both, when both exist)
  local c
  for c in "${HOME:-/nonexistent}/.config/deck/$1" "$PWD/$1"; do
    dangling "$c" && die2 "dangling symlink: $c"
    [[ ! -f $c ]] || printf '%s\n' "$c"
  done
}

# ---- per-key policy ----------------------------------------------------------------------------
# key_action KEY sets $act to check, split (check each space-separated item) or skip.
builtin_action() {
  case $1 in
    SPARK_USERS|HARNESS_KEYS) act=skip;;                  # names: whole-word list in .sanitize-words
    *_PORT|*_IF|*_GROUP|*_CLIENT_ID|MODELS_DIR|BACKEND_SSH_USER) act=skip;;  # generic, not identifying
    JUDGE_SSH_ALIASES|JUDGE_INFRA_REPOS|JUDGE_REPO_DIR) act=check;;  # site-identifying judge settings: still checked
    JUDGE_*) act=skip;;                                   # judge policy knobs (modes, model aliases, caps): public values
    ADMIN_SOURCE_IPS) act='split';;                       # space-separated list of addresses
    *) act=check;;
  esac
}
pol_keys=() pol_acts=() pol_default=check
policy=contracts/sanitize-policy.yaml
load_policy() { # the generated file's fixed line shapes only; anything else is an error, not a fallback
  local line n=0
  local rule_re='^  - \{"key": "([A-Z0-9_*]+)", "action": "(check|split|skip)", "class": "[a-z-]+"\}$'
  while IFS= read -r line || [[ -n $line ]]; do
    n=$((n + 1))
    if [[ $line =~ $rule_re ]]; then pol_keys+=("${BASH_REMATCH[1]}"); pol_acts+=("${BASH_REMATCH[2]}")
    elif [[ $line =~ ^default:\ (check|skip)$ ]]; then pol_default=${BASH_REMATCH[1]}
    elif [[ -z $line || $line == \#* || $line == rules: || $line =~ ^contract_version:\ [0-9]+$ ]]; then :
    else die2 "$policy:$n: unexpected line (regenerate it: python3 tools/site_keys.py policy)"
    fi
  done <"$policy"
  ((${#pol_keys[@]})) || die2 "$policy has no rules (regenerate it: python3 tools/site_keys.py policy)"
}
key_action() {
  if ((${#pol_keys[@]} == 0)); then builtin_action "$1"; return; fi
  local i
  for i in "${!pol_keys[@]}"; do
    # shellcheck disable=SC2053  # the rule key is a glob
    if [[ $1 == ${pol_keys[$i]} ]]; then act=${pol_acts[$i]}; return; fi
  done
  act=$pol_default
}

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
patterns=$tmp/patterns   # one literal per line (real values + .sanitize-extra)
labels=$tmp/labels       # "LABEL<TAB>literal" per pattern, for --history reports
words=$tmp/words         # .sanitize-words
: >"$patterns"; : >"$labels"; : >"$words"

# ---- example baseline -------------------------------------------------------------------------
# $baseline holds "KEY=default" lines as written in an env example. The registry's `default` is copied from
# site.env.example exactly as written, so both normalise the same way. A value equal to any baseline line of
# its key is public; with one line per key (the norm) this is the plain "differs from the example" test.
baseline=$tmp/baseline
registry_baseline() { # REGISTRY: append KEY=default for each active key of a contracts/site-keys.yaml file
  python3 -I -c '
import json, re, sys
path, out, section = sys.argv[1], [], None
for n, line in enumerate(open(path, encoding="utf-8"), 1):
    line = line.rstrip("\n")
    if not line.strip() or line.startswith("#"):
        continue
    m = re.match(r"([a-z_]+):", line)
    if m:
        section = m.group(1)
    elif section == "keys" and line.startswith("  - {"):
        try:
            k = json.loads(line[4:])
        except ValueError:
            sys.exit(f"{path}:{n}: a keys entry is not one JSON object")
        if not (isinstance(k, dict) and isinstance(k.get("name"), str) and isinstance(k.get("default", ""), str)):
            sys.exit(f"{path}:{n}: a keys entry needs a string name and default")
        if k.get("active", True):
            out.append(k["name"] + "=" + k.get("default", "").replace("\n", " "))
if not out:
    sys.exit(f"{path}: no active keys")
print("\n".join(out))
' "$1" >>"$baseline" || die2 "cannot read the key registry $1 (see the format in its header)"
}
load_baseline() {
  local f found=0
  : >"$baseline"
  if [[ -n $keys_flag ]]; then
    case $keys_flag in
      *.yaml|*.yml) registry_baseline "$keys_flag" ;;
      *) cat "$keys_flag" >>"$baseline" ;;
    esac
    return 0
  fi
  f=contracts/site-keys.yaml
  dangling "$f" && die2 "key registry is a dangling symlink: $PWD/$f"
  if [[ -f $f ]]; then registry_baseline "$f"; return 0; fi
  for f in ./*.env.example; do
    dangling "$f" && die2 "example is a dangling symlink: $PWD/${f#./}"
    [[ -f $f ]] || continue
    cat "$f" >>"$baseline"; found=1
  done
  ((found)) || echo "check-sanitized: note: no example baseline in $PWD (tried --keys, contracts/site-keys.yaml," \
    "*.env.example): every site value counts as real" >&2
}
is_example() { # KEY VALUE: true when VALUE equals a baseline default of KEY
  local line ex
  while IFS= read -r line; do
    ex=$(sed 's/#.*//; s/"//g' <<<"$line" | xargs)
    [[ $2 != "$ex" ]] || return 0
  done < <(sed -n "s/^$1=//p" "$baseline")
  return 1
}

# Values from an env file that differ from the example baseline (i.e. the real ones), with the
# per-key policy below. Emits "KEY<TAB>value" lines.
real_values() { # ENV_FILE
  local k v w items
  while IFS='=' read -r k v; do
    [[ $k =~ ^[A-Z_]+$ ]] || continue
    v=${v%%#*}; v=${v//\"/}; v=$(xargs <<<"$v")
    [[ -n $v ]] || continue
    ! is_example "$k" "$v" || continue
    key_action "$k"
    [[ $act != skip ]] || continue
    # shellcheck disable=SC2206  # a split key (ADMIN_SOURCE_IPS) is a space-separated list
    if [[ $act == split ]]; then items=($v); else items=("$v"); fi
    for w in "${items[@]}"; do
      [[ ${#w} -ge 6 && ! $w =~ ^[0-9]+$ ]] || continue   # skip tiny/numeric tokens
      case $w in 10.100.0.*) continue;; esac              # WireGuard defaults are the examples
      printf '%s\t%s\n' "$k" "$w"
    done
  done <"$1"
}

if ((!regex_only)); then
  dangling "$policy" && die2 "policy file is a dangling symlink: $policy"
  [[ ! -f $policy ]] || load_policy
  site_env=$(resolve_site_env)
  judge_env=$(resolve_judge_env)
  extra=$(extra_files .sanitize-extra)
  wordfiles=$(extra_files .sanitize-words)
  load_baseline
  real_values "$site_env" >>"$labels"
  [[ -z $judge_env ]] || real_values "$judge_env" >>"$labels"
  while IFS= read -r f; do
    [[ -z $f ]] || { grep -v '^\s*\(#\|$\)' "$f" | sed 's/^/sanitize-extra\t/' >>"$labels" || true; }
  done <<<"$extra"
  while IFS= read -r f; do
    [[ -z $f ]] || { grep -v '^\s*\(#\|$\)' "$f" >>"$words" || true; }
  done <<<"$wordfiles"
  cut -f2- "$labels" | grep -v '^$' | sort -u >"$patterns" || true
fi

# ---- --history: every commit ------------------------------------------------------------------
if [[ $mode == history ]]; then
  git rev-list --all >"$tmp/revs"
  [[ -s $tmp/revs ]] || { echo "check-sanitized: no commits"; exit 0; }
  # hist KIND GREP_OPTS...: git grep -z prints "rev:path\0line\0content"; report rev, path and the
  # label of each pattern that matched, never the content. A hit no label explains is still reported.
  hist() {
    local kind=$1; shift
    # xargs splits long rev lists; git grep exits 1 on "no match".
    xargs -r git grep -z -n -I "$@" <"$tmp/revs" >"$tmp/raw.$kind" 2>/dev/null || true
    : >"$tmp/hist.$kind"
    [[ -s $tmp/raw.$kind ]] || return 0
    python3 -I - "$tmp/raw.$kind" "$labels" "$words" "$kind" >"$tmp/hist.$kind" <<'PY'
import re, sys
raw, labels_f, words_f, kind = sys.argv[1:5]
pats = []
if kind == "fixed":
    for l in open(labels_f, encoding="utf-8", errors="replace"):
        k, sep, v = l.rstrip("\n").partition("\t")
        if sep and v:
            pats.append((k, lambda s, v=v: v in s))
else:
    for i, w in enumerate(open(words_f, encoding="utf-8", errors="replace"), 1):
        w = w.rstrip("\n")
        if w:
            rx = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(w) + r"(?![A-Za-z0-9_])", re.I)
            pats.append((f"sanitize-words entry {i}", lambda s, rx=rx: rx.search(s) is not None))
out = set()
for rec in open(raw, "rb").read().decode("utf-8", "replace").split("\n"):
    parts = rec.split("\0")
    if len(parts) < 3:
        continue
    rev, _, path = parts[0].partition(":")
    content = "\0".join(parts[2:])
    got = [label for label, match in pats if match(content)] or ["(unidentified pattern)"]
    for label in got:
        out.add(f"{rev[:12]} {path}: {label}")
print("\n".join(sorted(out)))
PY
    sed -i '/^$/d' "$tmp/hist.$kind"
  }
  fail=0
  if [[ -s $patterns ]]; then
    hist fixed -F -f "$patterns"
    if [[ -s $tmp/hist.fixed ]]; then
      echo "FORBIDDEN site-specific values in history (commit path: key):"; cat "$tmp/hist.fixed"; fail=1
    fi
  fi
  if [[ -s $words ]]; then
    hist words -w -F -i -f "$words"
    if [[ -s $tmp/hist.words ]]; then
      echo "FORBIDDEN names in history (commit path: entry):"; cat "$tmp/hist.words"; fail=1
    fi
  fi
  if ((fail)); then echo "check-sanitized: FAILED (history)"; exit 1; fi
  echo "check-sanitized: OK (history, $(wc -l <"$tmp/revs") commits)"
  exit 0
fi

# ---- working-tree files -----------------------------------------------------------------------
if [[ $mode == all ]]; then
  mapfile -t files < <(git ls-files)
else
  mapfile -t files < <( { git diff --cached --name-only --diff-filter=ACMR; git ls-files --others --exclude-standard; } | sort -u)
fi
((${#files[@]})) || { echo "check-sanitized: nothing to check"; exit 0; }

fail=0
if [[ -s $patterns ]]; then
  if hits=$(grep -nFI -f "$patterns" -- "${files[@]}" 2>/dev/null); then
    echo "FORBIDDEN site-specific values found:"; echo "$hits" | cut -c1-200; fail=1
  fi
fi
# .sanitize-words: short names matched as whole words, case-insensitive
# (e.g. real user first names that would false-positive as substrings).
if [[ -s $words ]]; then
  if hits=$(grep -nwFIi -f "$words" -- "${files[@]}" 2>/dev/null); then
    echo "FORBIDDEN names found:"; echo "$hits" | cut -c1-200; fail=1
  fi
fi

# Generic secret shapes.
secret_re='(sk-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{10,}|(PrivateKey|PresharedKey)[[:space:]]*=[[:space:]]*[A-Za-z0-9+/]{42}[A-Za-z0-9+/=]{2}|(password|passwd|secret|api_?key|master_?key|token)[\"'"'"']?[[:space:]]*[:=][[:space:]]*[\"'"'"']?[A-Za-z0-9+_.-][A-Za-z0-9+/_.-]{19,})'
if hits=$(grep -nEIi "$secret_re" -- "${files[@]}" 2>/dev/null | grep -viE 'CHANGEME|example|placeholder|\$\{|<[a-z_-]+>|generated|os\.environ/|/run/secrets/|[A-Za-z_]+\('); then
  echo "SECRET-LOOKING strings found:"; echo "$hits" | cut -c1-200; fail=1
fi

if ((fail)); then echo "check-sanitized: FAILED"; exit 1; fi
if ((regex_only)); then
  echo "check-sanitized: OK (${#files[@]} files, generic secret regexes only)"
else
  echo "check-sanitized: OK (${#files[@]} files)"
fi
