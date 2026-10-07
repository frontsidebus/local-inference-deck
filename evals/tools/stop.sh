#!/usr/bin/env bash
# Stop a running eval plan cleanly: writes STOP (the plan starts no further steps), sends SIGINT to its
# evals/run.py processes (they write partial summaries), and TERM after 60 s.
# Resume: delete $EVAL_STATE_DIR/STOP and start the plan again; finished items are skipped.
# Usage: EVAL_RUN=<prefix> evals/tools/stop.sh ["reason"]
set -u
. "$(dirname "$0")/lib.sh"
if ! pgrep -f "$RUN_PATTERN" >/dev/null; then
  printf '%s\n%s\n' "$(date -u +%FT%TZ)" "${1:-manual stop}" > "$STOP_FILE"
  echo "no run.py running for $EVAL_RUN; wrote $STOP_FILE so the plan starts no further steps"
  exit 0
fi
eval_stop "${1:-manual stop}"
pgrep -f "$RUN_PATTERN" >/dev/null && echo "still running after TERM; check: pgrep -af evals/run.py" || echo "stopped"
