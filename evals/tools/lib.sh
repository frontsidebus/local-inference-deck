# Shared helpers for evals/tools/*.sh and evals/plans/*.sh (source it; not executable on its own).
#
# Inputs (environment):
#   EVAL_RUN        run-name prefix of the plan, e.g. evalb (required). Every run.py the plan starts uses
#                   --run-name EVAL_RUN or EVAL_RUN-<suffix>; the stop and the guard match on it.
#   EVAL_STATE_DIR  where markers, logs, gpu.csv and STOP live (default evals/results/_runs/$EVAL_RUN, gitignored)
#   REPO            the repo checkout (default: the one this file is in)
EVAL_TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd "$EVAL_TOOLS_DIR/../.." && pwd)}"
: "${EVAL_RUN:?set EVAL_RUN to the run-name prefix of the plan, e.g. EVAL_RUN=evalb}"
EVAL_STATE_DIR="${EVAL_STATE_DIR:-$REPO/evals/results/_runs/$EVAL_RUN}"
mkdir -p "$EVAL_STATE_DIR"
STOP_FILE="$EVAL_STATE_DIR/STOP"
# Anchored to the command line plans use (python3 -B evals/run.py), so it can't match an unrelated shell or
# editor whose command line merely contains the text.
RUN_PATTERN="^python3 -B evals/run.py .*--run-name ${EVAL_RUN}(-[^ ]*)?( |\$)"

# site_value KEY: a value from site.env (KEY=value, trailing comment stripped), else empty
site_value() {
  local f="${SITE_ENV:-$REPO/site.env}"
  [ -f "$f" ] || return 0
  sed -n "s/^$1=//p" "$f" | tail -1 | sed 's/[[:space:]]*#.*$//; s/^"\(.*\)"$/\1/; s/[[:space:]]*$//'
}

eval_log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$EVAL_STATE_DIR/launch.log"; }

eval_notify() {  # urgency title body
  command -v notify-send >/dev/null && notify-send -u "$1" -a "eval $EVAL_RUN" "$2" "$3" 2>/dev/null || true
}

# eval_register NAME RESULTS_DIR_NAME: lists a step for status.sh (in plan order); RESULTS_DIR_NAME may be "-"
eval_register() {
  grep -q "^$1	" "$EVAL_STATE_DIR/steps.tsv" 2>/dev/null || printf '%s\t%s\n' "$1" "$2" >> "$EVAL_STATE_DIR/steps.tsv"
}

# eval_step NAME CMD...: runs CMD from $REPO with markers NAME.start/.end/.status and output in NAME.log.
# Fails when CMD fails or STOP exists, so a plan's `step && step` chains end at the first problem.
eval_step() {
  local name="$1"; shift
  [ -e "$STOP_FILE" ] && return 1
  eval_log "start $name"
  date -u +%FT%TZ > "$EVAL_STATE_DIR/$name.start"
  (cd "$REPO" && "$@") >> "$EVAL_STATE_DIR/$name.log" 2>&1
  local rc=$?
  echo "$rc" > "$EVAL_STATE_DIR/$name.status"; date -u +%FT%TZ > "$EVAL_STATE_DIR/$name.end"
  eval_log "end $name (exit $rc)"
  [ $rc -eq 0 ] && [ ! -e "$STOP_FILE" ]
}

# eval_preflight: refuse to start over a STOP file or next to a running plan with the same EVAL_RUN
eval_preflight() {
  if [ -e "$STOP_FILE" ]; then
    echo "STOP file present ($(tail -1 "$STOP_FILE")). Delete $STOP_FILE to resume." >&2; return 1
  fi
  if pgrep -f "$RUN_PATTERN" >/dev/null; then
    echo "a run.py with --run-name $EVAL_RUN* is already running" >&2; return 1
  fi
}

# eval_stop REASON: write STOP (no further steps start), SIGINT run.py (it saves partial results), TERM after 60 s
eval_stop() {
  printf '%s\n%s\n' "$(date -u +%FT%TZ)" "$1" > "$STOP_FILE"
  pkill -INT -f "$RUN_PATTERN" || true
  local _
  for _ in $(seq 1 12); do pgrep -f "$RUN_PATTERN" >/dev/null || return 0; sleep 5; done
  pkill -TERM -f "$RUN_PATTERN" || true
}
