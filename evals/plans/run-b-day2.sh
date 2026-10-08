#!/usr/bin/env bash
# Eval run (b), day 2: hermes and vision on the same 100-item subsets big used on day 1, so all five models can be
# compared on shared items; big then grades their sevenllm-qa answers, and the report covers days 1 and 2.
# About 7 h on two RTX 3090s; both models take both GPUs, so coder and coder-fast are evicted for the whole run.
#
#   evals/plans/run-b-day2.sh prepare     # only if day 1's data isn't there (same as run-b-day1.sh prepare)
#   nohup evals/plans/run-b-day2.sh >/dev/null 2>&1 &     # run (resumable)
#   EVAL_RUN=evalb2 evals/tools/status.sh
#   EVAL_RUN=evalb2 evals/tools/stop.sh "reason"
#
# Env: EVAL_GATEWAY=tunnel|edge (default tunnel), NO_GUARD=1, DAY1_RUN (day 1's run name, default evalb), and the
# guard's thresholds. hermes and vision have no thinking switch in this harness's sense, so thinking is off.
#
# Phases
#   1. hermes (Hermes 4.3 36B, tensor split), thinking off: 992 items + 100 sevenllm-qa         ~3.5 h
#   2. vision (Gemma 4 31B), thinking off: the same                                            ~3.3 h
#   3. big grades hermes's and vision's sevenllm-qa answers (one big load)                     ~0.2 h
#   4. report.py across day 1 and day 2 -> $EVAL_STATE_DIR/report.md, report.csv, pairs.csv
set -u
export EVAL_RUN="${EVAL_RUN:-evalb2}"
DAY1_RUN="${DAY1_RUN:-evalb}"
. "$(cd "$(dirname "$0")/../tools" && pwd)/lib.sh"
cd "$REPO" || exit 2
PY=(python3 -B evals/run.py)

if [ "${1:-run}" = prepare ]; then exec "$(dirname "$0")/run-b-day1.sh" prepare; fi
[ "${1:-run}" = run ] || { echo "usage: $0 [prepare|run]" >&2; exit 2; }

SUBSET_SUITES=(ctibench-mcq ctibench-rcm ctibench-vsp nvd-cwe nvd-cvss cse-frr nvd-cwe-nvdlab nvd-cvss-nvdlab)
ARGS=()
for s in "${SUBSET_SUITES[@]}"; do ARGS+=(--suite "$s.sample200in100"); done
ARGS+=(--suite cybermetric-500.sample500in100 --suite ctibench-ate --suite sevenllm-mcq.sample100)
COMMON=(--thinking off --scorer-override cse-frr=refusal --run-name "$EVAL_RUN" --timeout 600)
QA=(--suite sevenllm-qa.sample100 --thinking off --no-grade --run-name "$EVAL_RUN-qa" --timeout 600)

missing=()
for a in "${ARGS[@]}" sevenllm-qa.sample100; do
  [ "$a" = --suite ] && continue
  [ -f "evals/data/$a.jsonl" ] || missing+=("$a")
done
[ ${#missing[@]} -eq 0 ] || { echo "missing data: ${missing[*]}. Run: $0 prepare" >&2; exit 2; }
eval_preflight || exit 1

eval_register hermes-off "$EVAL_RUN-hermes"; eval_register hermes-qa "$EVAL_RUN-qa-hermes"
eval_register vision-off "$EVAL_RUN-vision"; eval_register vision-qa "$EVAL_RUN-qa-vision"
eval_register grade -; eval_register report -

fail() { eval_log "stopped: $1"; eval_notify critical "Eval $EVAL_RUN stopped" "$1"; exit 1; }

GUARD_PID=""
if [ "${NO_GUARD:-0}" != 1 ]; then
  "$EVAL_TOOLS_DIR/gpu-guard.sh" >/dev/null 2>&1 &
  GUARD_PID=$!
fi
trap '[ -n "$GUARD_PID" ] && kill "$GUARD_PID" 2>/dev/null; eval_gateway_down' EXIT
eval_gateway_up || fail "gateway tunnel"

eval_log "plan run-b-day2 ($EVAL_RUN): $(git rev-parse --short HEAD 2>/dev/null), state $EVAL_STATE_DIR, guard ${GUARD_PID:-off}"
eval_notify normal "Eval $EVAL_RUN started" "Phase 1: hermes"

# 1-2. one model at a time: each takes both GPUs (concurrency 1)
eval_step hermes-off "${PY[@]}" "${ARGS[@]}" --model hermes "${COMMON[@]}" || fail "phase 1 (hermes-off)"
eval_step hermes-qa "${PY[@]}" "${QA[@]}" --model hermes || fail "phase 1 (hermes-qa)"
eval_notify normal "Eval $EVAL_RUN" "Phase 2: vision"
eval_step vision-off "${PY[@]}" "${ARGS[@]}" --model vision "${COMMON[@]}" || fail "phase 2 (vision-off)"
eval_step vision-qa "${PY[@]}" "${QA[@]}" --model vision || fail "phase 2 (vision-qa)"

# 3. big grades both in one pass
eval_step grade "${PY[@]}" --suite sevenllm-qa.sample100 --model hermes,vision --thinking off \
  --run-name "$EVAL_RUN-qa" --rescore --grader-model big || fail "phase 3 (grading)"

# 4. report across both days (day 1 dirs that exist)
R=evals/results
DIRS=()
for d in "$DAY1_RUN-big" "$DAY1_RUN-coder" "$DAY1_RUN-coder-fast" "$EVAL_RUN-hermes" "$EVAL_RUN-vision" \
         "$DAY1_RUN-qa-big" "$DAY1_RUN-qa-coder" "$DAY1_RUN-qa-coder-fast" "$EVAL_RUN-qa-hermes" "$EVAL_RUN-qa-vision"; do
  [ -f "$R/$d/run.json" ] && DIRS+=("$R/$d")
done
eval_step report python3 -B evals/report.py "${DIRS[@]}" --split-label-source \
  --items evals/data/nvd-cwe.sample200.jsonl --items evals/data/nvd-cvss.sample200.jsonl \
  --title "Eval run (b), days 1 and 2 (thinking off)" --out "$EVAL_STATE_DIR/report.md" \
  --csv "$EVAL_STATE_DIR/report.csv" --pairs-csv "$EVAL_STATE_DIR/pairs.csv" || fail "phase 4 (report)"

eval_log "plan run-b-day2 ($EVAL_RUN) complete"
eval_notify normal "Eval $EVAL_RUN complete" "Report: $EVAL_STATE_DIR/report.md"
