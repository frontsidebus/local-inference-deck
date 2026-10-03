#!/usr/bin/env bash
# judge/install.sh -- wire the agent judge into a Hermes profile.
#
# Usage:
#   judge/install.sh [--dry-run]            show what would change (default; changes nothing)
#   judge/install.sh --apply                merge the hooks block, create the review dir, render the gate policy
#   judge/install.sh --uninstall [--dry-run]  remove only the judge-managed hook entries (review data is kept)
#
# Options:
#   --hermes-home DIR     Hermes profile home (default: $HERMES_HOME, else ~/.hermes). Use a temp dir for tests.
#   --hermes-python PATH  Python with PyYAML used to edit config.yaml
#                         (default: $HERMES_HOME/hermes-agent/venv/bin/python, else ~/.hermes/... venv)
#   --hermes-src DIR      hermes-agent source tree, used to check event names against VALID_HOOKS
#                         (default: next to --hermes-python, else $HERMES_HOME/hermes-agent)
#   --python PATH         Python 3.10+ that runs the hooks (default: python3 on PATH). Stdlib only.
#   --site-env FILE       site values (default: $SITE_ENV, else <judge>/../site.env, else <judge>/site.env)
#   --review-dir DIR      review data dir (default: $JUDGE_REVIEW_DIR, else site.env, else $HERMES_HOME/review)
#   --with-units          also install the systemd user units from runner/units/ and watch/
#   --unit-dir DIR        where units go (default: ${XDG_CONFIG_HOME:-~/.config}/systemd/user)
#   --start               with --with-units: run systemctl --user daemon-reload/enable --now (else only print)
#
# The hooks block is merged with the Hermes venv's PyYAML. Managed entries carry `managed_by: agent-judge`
# (Hermes ignores unknown keys in a hook entry), and are also recognised by their command path
# (.../judge/hooks/{gate,verify,enqueue,inject}.py). Entries you added yourself are never touched.
# Comments outside the `hooks:` block are preserved; comments inside it are not (a backup is kept).
# This script never sets hooks_auto_accept: Hermes asks for consent on first use of each hook.
set -euo pipefail

JUDGE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MANAGED_TAG=agent-judge

die() { echo "install: $*" >&2; exit 1; }
warn() { echo "install: WARNING: $*" >&2; }
usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; }

MODE=dry-run          # dry-run | apply
ACTION=install        # install | uninstall
EXPLICIT_DRY=0
WITH_UNITS=0
START=0
ARG_HERMES_HOME=""; ARG_HERMES_PY=""; ARG_HERMES_SRC=""; ARG_PY=""; ARG_SITE_ENV=""; ARG_REVIEW=""; ARG_UNIT_DIR=""

while (($#)); do
  case $1 in
    --dry-run) MODE=dry-run; EXPLICIT_DRY=1;;
    --apply) MODE=apply;;
    --uninstall) ACTION=uninstall;;
    --with-units) WITH_UNITS=1;;
    --start) START=1;;
    --hermes-home) ARG_HERMES_HOME=${2:?--hermes-home needs a dir}; shift;;
    --hermes-python) ARG_HERMES_PY=${2:?--hermes-python needs a path}; shift;;
    --hermes-src) ARG_HERMES_SRC=${2:?--hermes-src needs a dir}; shift;;
    --python) ARG_PY=${2:?--python needs a path}; shift;;
    --site-env) ARG_SITE_ENV=${2:?--site-env needs a file}; shift;;
    --review-dir) ARG_REVIEW=${2:?--review-dir needs a dir}; shift;;
    --unit-dir) ARG_UNIT_DIR=${2:?--unit-dir needs a dir}; shift;;
    -h|--help) usage; exit 0;;
    *) die "unknown argument: $1 (see --help)";;
  esac
  shift
done
# --uninstall acts unless --dry-run is given explicitly; install only acts with --apply.
if [[ $ACTION == uninstall ]]; then
  if ((EXPLICIT_DRY)); then MODE=dry-run; else MODE=apply; fi
fi
if ((START)) && ! ((WITH_UNITS)); then die "--start only makes sense with --with-units"; fi

# ---------------------------------------------------------------- paths and site values
# Values given on the command line or in the environment win over site.env.
PRE_HERMES_HOME=${ARG_HERMES_HOME:-${HERMES_HOME:-}}
PRE_REVIEW=${ARG_REVIEW:-${JUDGE_REVIEW_DIR:-}}

SITE_ENV_FILE=${ARG_SITE_ENV:-${SITE_ENV:-${JUDGE_SITE_ENV:-}}}
if [[ -z $SITE_ENV_FILE ]]; then
  for f in "$JUDGE_DIR/../site.env" "$JUDGE_DIR/site.env"; do
    if [[ -f $f ]]; then SITE_ENV_FILE=$(cd "$(dirname "$f")" && pwd)/site.env; break; fi
  done
fi
[[ -z $SITE_ENV_FILE || -f $SITE_ENV_FILE ]] || die "site env not found: $SITE_ENV_FILE"
if [[ -n $SITE_ENV_FILE ]]; then export SITE_ENV=$SITE_ENV_FILE; fi
if [[ -n $ARG_HERMES_HOME ]]; then export HERMES_HOME=$ARG_HERMES_HOME; fi
if [[ -n $ARG_REVIEW ]]; then export JUDGE_REVIEW_DIR=$ARG_REVIEW; fi

# Shared loader (judge/lib/config.sh: defaults < site.env < environment). The inline fallback below
# follows the same rules: site.env is parsed, never sourced, and the environment wins.
loaded=0
if [[ -f $JUDGE_DIR/lib/config.sh ]]; then
  # shellcheck disable=SC1091
  . "$JUDGE_DIR/lib/config.sh"
  if judge_load_config; then loaded=1; else warn "judge/lib/config.sh failed; using the inline loader"; fi
fi
if ((!loaded)) && [[ -n $SITE_ENV_FILE ]]; then
  re='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$'
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line =~ $re ]] || continue
    k=${BASH_REMATCH[2]}; v=${BASH_REMATCH[3]}
    case $v in
      \"*) v=${v#\"}; v=${v%%\"*};;
      \'*) v=${v#\'}; v=${v%%\'*};;
      *) v=${v%%[[:space:]]#*}; v=${v%"${v##*[![:space:]]}"};;
    esac
    [[ -n ${!k:-} ]] || export "$k=$v"
  done < "$SITE_ENV_FILE"
fi
: "${JUDGE_MODE:=frontier}" "${JUDGE_LOCAL_MODEL:=big}" "${JUDGE_FRONTIER_CMD:=claude}"
: "${JUDGE_RUNAWAY_TOKENS:=20000}" "${JUDGE_RUNAWAY_MINUTES:=10}"
export JUDGE_MODE JUDGE_LOCAL_MODEL JUDGE_FRONTIER_CMD JUDGE_RUNAWAY_TOKENS JUDGE_RUNAWAY_MINUTES

HERMES_HOME=${PRE_HERMES_HOME:-${HERMES_HOME:-$HOME/.hermes}}
HERMES_HOME=${HERMES_HOME/#\~/$HOME}
if [[ -n $PRE_REVIEW ]]; then JUDGE_REVIEW_DIR=$PRE_REVIEW
elif [[ -n $ARG_HERMES_HOME ]]; then JUDGE_REVIEW_DIR=$HERMES_HOME/review   # a test home never points at real data
else JUDGE_REVIEW_DIR=${JUDGE_REVIEW_DIR:-$HERMES_HOME/review}; fi
JUDGE_REVIEW_DIR=${JUDGE_REVIEW_DIR/#\~/$HOME}
export HERMES_HOME JUDGE_REVIEW_DIR JUDGE_DIR
CONFIG=$HERMES_HOME/config.yaml

HERMES_PY=${ARG_HERMES_PY:-$HERMES_HOME/hermes-agent/venv/bin/python}
if [[ ! -x $HERMES_PY && -z $ARG_HERMES_PY ]]; then HERMES_PY=$HOME/.hermes/hermes-agent/venv/bin/python; fi
[[ -x $HERMES_PY ]] || die "Hermes venv python not found: $HERMES_PY (pass --hermes-python)"
"$HERMES_PY" -c 'import yaml' 2>/dev/null || die "$HERMES_PY has no PyYAML"
if [[ -n $ARG_HERMES_SRC ]]; then HERMES_SRC=$ARG_HERMES_SRC
else HERMES_SRC=$(cd "$(dirname "$HERMES_PY")/../.." 2>/dev/null && pwd || echo "$HERMES_HOME/hermes-agent"); fi

JUDGE_PYTHON=${ARG_PY:-$(command -v python3 || true)}
[[ -n $JUDGE_PYTHON && -x $JUDGE_PYTHON ]] || die "python3 not found (pass --python)"
"$JUDGE_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || die "$JUDGE_PYTHON is older than 3.10 (the judge runtime needs 3.10+)"
export JUDGE_PYTHON

UNIT_DIR=${ARG_UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}

echo "== agent judge: $ACTION ($MODE)"
echo "   judge dir     $JUDGE_DIR"
echo "   HERMES_HOME   $HERMES_HOME"
echo "   config        $CONFIG"
echo "   review dir    $JUDGE_REVIEW_DIR"
echo "   site.env      ${SITE_ENV_FILE:-(none; defaults only)}"
echo "   hook python   $JUDGE_PYTHON"
echo "   yaml python   $HERMES_PY"
echo

[[ -f $CONFIG ]] || die "no config.yaml at $CONFIG (run Hermes once, or pass --hermes-home)"

if [[ $ACTION == install && $MODE == apply ]]; then
  missing=()
  for h in gate verify enqueue inject; do [[ -f $JUDGE_DIR/hooks/$h.py ]] || missing+=("hooks/$h.py"); done
  # gate.py is installed fail_closed: a missing script would block every terminal/write call.
  ((${#missing[@]} == 0)) || die "missing hook scripts: ${missing[*]} (refusing to install hooks that cannot run)"
fi

# ---------------------------------------------------------------- hooks block merge (PyYAML)
merge_hooks() { # $1 = install|uninstall, $2 = dry-run|apply
  "$HERMES_PY" - "$1" "$2" "$CONFIG" "$JUDGE_DIR" "$JUDGE_PYTHON" "$HERMES_SRC" "$MANAGED_TAG" <<'PY'
import ast, difflib, os, re, shlex, shutil, sys, time
import yaml

action, mode, config, judge_dir, hook_py, hermes_src, tag = sys.argv[1:8]
MARK = "managed_by"
HOOK_NAMES = ("gate", "verify", "enqueue", "inject")
RESERVED = ("outbound", "output_spill")      # non-event sub-sections of hooks: in Hermes

def cmd(name):
    return " ".join(shlex.quote(p) for p in (hook_py, os.path.join(judge_dir, "hooks", name + ".py")))

def entry(**kw):
    kw[MARK] = tag
    return kw

desired = {
    "pre_tool_call":    [entry(matcher="terminal|write_file|patch|read_file", command=cmd("gate"), timeout=10, fail_closed=True)],
    "post_tool_call":   [entry(matcher="write_file|patch|terminal|memory|skill_manage", command=cmd("enqueue"))],
    "on_session_start": [entry(command=cmd("enqueue"))],
    "on_session_end":   [entry(command=cmd("enqueue"))],
    "pre_verify":       [entry(command=cmd("verify"), timeout=60)],
    "pre_llm_call":     [entry(command=cmd("inject"), timeout=10)],
}

# Check event names against the Hermes source (VALID_HOOKS minus SHELL_UNSUPPORTED_HOOKS).
plugins = os.path.join(hermes_src, "hermes_cli", "plugins.py")
sets = {}
try:
    for node in ast.parse(open(plugins, encoding="utf-8").read()).body:
        target = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
        if isinstance(target, ast.Name) and target.id in ("VALID_HOOKS", "SHELL_UNSUPPORTED_HOOKS"):
            sets[target.id] = set(ast.literal_eval(node.value))
except (OSError, SyntaxError, ValueError) as exc:
    print(f"note: cannot read {plugins} ({exc.__class__.__name__}); event names not verified")
if "VALID_HOOKS" in sets:
    for ev in list(desired):
        if ev not in sets["VALID_HOOKS"] or ev in sets.get("SHELL_UNSUPPORTED_HOOKS", set()):
            print(f"WARNING: hook event {ev!r} is not supported for shell hooks by this Hermes; skipping it")
            del desired[ev]
    print(f"events verified against {plugins}: {', '.join(desired)}")

judge_hooks = os.path.join(os.path.realpath(judge_dir), "hooks")
path_re = re.compile(r"(^|/)judge/hooks/(%s)\.py$" % "|".join(HOOK_NAMES))

def is_managed(e):
    if not isinstance(e, dict):
        return False
    if e.get(MARK) == tag:
        return True
    try:
        argv = shlex.split(str(e.get("command") or ""))
    except ValueError:
        return False
    for tok in argv:
        p = os.path.expanduser(tok)
        if path_re.search(p):
            return True
        if os.path.dirname(os.path.realpath(p)) == judge_hooks and os.path.basename(p)[:-3] in HOOK_NAMES:
            return True
    return False

text = open(config, encoding="utf-8").read()
data = yaml.safe_load(text)
if data is None:
    data = {}
if not isinstance(data, dict):
    sys.exit(f"error: {config} is not a YAML mapping")
old_hooks = data.get("hooks")
if old_hooks is not None and not isinstance(old_hooks, dict):
    sys.exit("error: hooks: in config.yaml is not a mapping; fix it by hand")
hooks = dict(old_hooks or {})

want = {} if action == "uninstall" else desired
for ev in list(hooks) + [e for e in want if e not in hooks]:
    if ev in RESERVED:
        continue
    entries = hooks.get(ev)
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        if ev in want:
            sys.exit(f"error: hooks.{ev} is not a list; fix it by hand")
        continue
    user = [e for e in entries if not is_managed(e)]
    managed = [e for e in entries if is_managed(e)]
    if managed == want.get(ev, []):
        continue
    new = user + want.get(ev, [])
    if new:
        hooks[ev] = new
    else:
        hooks.pop(ev, None)

print("Hooks block merged by this installer (managed entries only):" if action == "install"
      else "Managed entries to remove:")
shown = want if action == "install" else {ev: [e for e in (v or []) if is_managed(e)]
                                          for ev, v in (old_hooks or {}).items()
                                          if ev not in RESERVED and isinstance(v, list)}
shown = {k: v for k, v in shown.items() if v}
print(yaml.safe_dump({"hooks": shown}, sort_keys=False, default_flow_style=False, width=1000).rstrip()
      if shown else "  (none)")
print()

if hooks == (old_hooks or {}) and (old_hooks is not None or not hooks):
    print(f"config.yaml: no change needed ({'already installed' if action == 'install' else 'nothing managed'})")
    sys.exit(0)

# Rewrite only the top-level hooks: block; the rest of the file (comments included) is kept verbatim.
lines = text.splitlines(keepends=True)
start = next((i for i, l in enumerate(lines) if re.match(r"hooks\s*:", l)), None)
if start is None and old_hooks is not None:
    sys.exit("error: could not locate the top-level hooks: line; edit config.yaml by hand")
if start is not None:
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#"):
        end += 1
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1][0] == "#"):
        end -= 1                      # trailing blanks / column-0 comments belong to what follows
else:
    start = end = len(lines)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

if hooks:
    dumped = yaml.safe_dump({"hooks": hooks}, sort_keys=False, default_flow_style=False, width=1000)
    first, rest = dumped.split("\n", 1)
    block = [first + "\n",
             "  # entries with managed_by: %s are maintained by judge/install.sh; other entries are yours\n" % tag
             ] + rest.splitlines(keepends=True)
    if start == len(lines) and lines:
        block = ["\n"] + block
else:
    block = []
new_lines = lines[:start] + block + lines[end:]
new_text = "".join(new_lines)

expected = dict(data)
if hooks:
    expected["hooks"] = hooks
else:
    expected.pop("hooks", None)
check = yaml.safe_load(new_text) or {}
if check != expected:
    sys.exit("error: the edited config.yaml does not round-trip; nothing written. Merge the block above by hand.")

diff = difflib.unified_diff(text.splitlines(keepends=True), new_lines, config, config + " (new)")
print("Diff of config.yaml:")
sys.stdout.writelines(diff)
print()
if mode != "apply":
    print("config.yaml: dry run, nothing written")
    sys.exit(0)
ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
backup, n = f"{config}.bak-judge-{ts}", 1
while os.path.exists(backup):          # never overwrite an earlier backup
    backup, n = f"{config}.bak-judge-{ts}-{n}", n + 1
shutil.copy2(config, backup)
os.chmod(backup, 0o600)
tmp = f"{config}.tmp-judge-{os.getpid()}"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(new_text)
shutil.copymode(config, tmp)
os.replace(tmp, config)
print(f"config.yaml: written; backup at {backup}")
PY
}

# ---------------------------------------------------------------- gate policy rendering
render_policy() { # $1 = dry-run|apply
  local tmpl=$JUDGE_DIR/policy/gate-policy.json.tmpl out=$JUDGE_REVIEW_DIR/gate-policy.json
  if [[ ! -f $tmpl ]]; then warn "no $tmpl yet; gate policy not rendered"; return 0; fi
  "$JUDGE_PYTHON" - "$1" "$tmpl" "$out" <<'PY'
import json, os, re, sys
mode, tmpl, out = sys.argv[1:4]
src = open(tmpl, encoding="utf-8").read()
names = sorted(set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", src)))
missing = [n for n in names if not os.environ.get(n)]
if missing:
    sys.exit("install: gate policy needs site values that are unset or empty: " + ", ".join(missing))
# Values land inside JSON strings, so escape them for that context.
text = re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", lambda m: json.dumps(os.environ[m.group(1)])[1:-1], src)
json.loads(text)
if mode != "apply":
    print(f"gate policy: would render {tmpl} -> {out} (vars: {', '.join(names) or 'none'})")
    sys.exit(0)
tmp = out + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(text)
os.replace(tmp, out)
os.chmod(out, 0o600)
print(f"gate policy: rendered {out} (vars: {', '.join(names) or 'none'})")
PY
}

# ---------------------------------------------------------------- systemd user units
unit_sources() { # prints source unit files (runner/units/ and watch/)
  local d f
  for d in "$JUDGE_DIR/runner/units" "$JUDGE_DIR/watch"; do
    [[ -d $d ]] || continue
    for f in "$d"/*.service "$d"/*.path "$d"/*.timer "$d"/*.service.tmpl "$d"/*.path.tmpl "$d"/*.timer.tmpl; do
      [[ -f $f ]] && printf '%s\n' "$f"
    done
  done
}

units_to_enable() { # $@ = installed unit names; .path/.timer units, plus services nothing else triggers
  local u base
  for u in "$@"; do
    case $u in
      *.path|*.timer) echo "$u";;
      *.service)
        base=${u%.service}
        if ! printf '%s\n' "$@" | grep -qxE "$base\\.(path|timer)"; then echo "$u"; fi;;
    esac
  done
}

install_units() { # $1 = dry-run|apply
  local srcs names=() f name
  mapfile -t srcs < <(unit_sources)
  if ((${#srcs[@]} == 0)); then warn "no unit files under runner/units/ or watch/ yet"; return 0; fi
  for f in "${srcs[@]}"; do
    name=$(basename "$f" .tmpl); names+=("$name")
    if [[ $1 != apply ]]; then echo "units: would install $f -> $UNIT_DIR/$name"; continue; fi
    mkdir -p "$UNIT_DIR"
    # Every unit file is rendered (not only *.tmpl): only ${JUDGE_DIR}-style names known to the judge are
    # replaced, so systemd's own syntax (%h, $VAR, ${OTHER}) passes through untouched.
    "$JUDGE_PYTHON" - "$f" "$UNIT_DIR/$name" <<'PY'
import os, re, sys
src, dst = sys.argv[1:3]
allowed = {"JUDGE_DIR", "JUDGE_REVIEW_DIR", "HERMES_HOME", "JUDGE_PYTHON"} | {
    k for k in os.environ if k.startswith(("JUDGE_", "SPARK_", "EDGE_", "BACKEND_"))}
text = open(src, encoding="utf-8").read()
text = re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", lambda m: os.environ[m.group(1)] if m.group(1) in allowed else m.group(0), text)
open(dst, "w", encoding="utf-8").write(text)
PY
    chmod 644 "$UNIT_DIR/$name"
    echo "units: installed $UNIT_DIR/$name"
  done
  local enable; mapfile -t enable < <(units_to_enable "${names[@]}")
  echo
  if ((START)) && [[ $1 == apply ]]; then
    systemctl --user daemon-reload
    ((${#enable[@]})) && systemctl --user enable --now "${enable[@]}"
    echo "units: enabled and started ${enable[*]}"
  else
    echo "To start the judge units:"
    echo "  systemctl --user daemon-reload"
    ((${#enable[@]})) && echo "  systemctl --user enable --now ${enable[*]}"
    echo "  (add --start to have this script run them)"
  fi
}

uninstall_units() { # $1 = dry-run|apply
  local srcs names=() f enable
  mapfile -t srcs < <(unit_sources)
  for f in "${srcs[@]}"; do names+=("$(basename "$f" .tmpl)"); done
  ((${#names[@]})) || return 0
  mapfile -t enable < <(units_to_enable "${names[@]}")
  if ((START)) && [[ $1 == apply ]]; then
    systemctl --user disable --now "${enable[@]}" || true
  else
    echo "Stop the judge units first:  systemctl --user disable --now ${enable[*]}"
  fi
  for f in "${names[@]}"; do
    [[ -e $UNIT_DIR/$f ]] || continue
    if [[ $1 == apply ]]; then rm -f "$UNIT_DIR/$f"; echo "units: removed $UNIT_DIR/$f"
    else echo "units: would remove $UNIT_DIR/$f"; fi
  done
  if ((START)) && [[ $1 == apply ]]; then systemctl --user daemon-reload; fi
}

hermes_cmd() { # how to call hermes for this profile
  if [[ $HERMES_HOME == "$HOME/.hermes" ]]; then echo "hermes"; else echo "HERMES_HOME=$HERMES_HOME hermes"; fi
}

# ---------------------------------------------------------------- main
merge_hooks "$ACTION" "$MODE"
echo

if [[ $ACTION == uninstall ]]; then
  ((WITH_UNITS)) && uninstall_units "$MODE"
  H=$(hermes_cmd)
  echo "Review data is left in place: $JUDGE_REVIEW_DIR"
  echo "Consent records are not removed. To drop them as well:"
  for h in gate enqueue verify inject; do
    echo "  $H hooks revoke \"$JUDGE_PYTHON $JUDGE_DIR/hooks/$h.py\""
  done
  exit 0
fi

if [[ $MODE == apply ]]; then
  mkdir -p "$JUDGE_REVIEW_DIR"
  chmod 700 "$JUDGE_REVIEW_DIR"
  for d in queue evidence findings acks done snapshots; do
    mkdir -p "$JUDGE_REVIEW_DIR/$d"; chmod 700 "$JUDGE_REVIEW_DIR/$d"
  done
  echo "review dir: $JUDGE_REVIEW_DIR (mode 700)"
else
  echo "review dir: would create $JUDGE_REVIEW_DIR and its queue/ evidence/ findings/ acks/ done/ snapshots/ (mode 700)"
fi
render_policy "$MODE"
((WITH_UNITS)) && { echo; install_units "$MODE"; }

H=$(hermes_cmd)
auto=$("$HERMES_PY" -c 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])) or {}; print(d.get("hooks_auto_accept") is True)' "$CONFIG")
echo
if [[ $auto == True ]]; then
  warn "hooks_auto_accept is true in $CONFIG: Hermes registers new hooks WITHOUT asking. This installer does not set it; consider setting it to false."
fi
if [[ $MODE != apply ]]; then
  echo "Dry run: nothing changed. Re-run with --apply to install."
  exit 0
fi
cat <<EOF
Next: consent (required; this installer does not set hooks_auto_accept).
  Hermes asks once per (event, command) pair the first time it loads a hook, and records the
  answer in $HERMES_HOME/shell-hooks-allowlist.json. Start an interactive session and approve
  each judge hook when asked:
    $H chat
  Non-TTY runs (gateway, cron) never prompt: they skip unapproved hooks with a warning, so approve
  in a terminal first. Then check:
    $H hooks list          # every judge hook shows as allowed
    $H hooks doctor
    $H hooks test pre_tool_call --for-tool terminal
EOF
