#!/usr/bin/env bash
# Eval run (b), day 1: the coding pair on 200-item samples, big on 100-item subsets of the same items, thinking
# on/off, and big grading the free-text answers. About 10 h on two RTX 3090s; see evals/plans/README.md.
#
#   evals/plans/run-b-day1.sh prepare     # fetch the data and build the derived sets (network; run once)
#   nohup evals/plans/run-b-day1.sh >/dev/null 2>&1 &     # run (resumable: start it again after a stop)
#   EVAL_RUN=evalb evals/tools/status.sh  # watch
#   EVAL_RUN=evalb evals/tools/stop.sh "reason"           # stop cleanly; resume: rm STOP, start again
#
# Env: EVAL_GATEWAY=tunnel|edge (default tunnel: SSH to the backend's LiteLLM), NO_GUARD=1 (no GPU guard), CODER_SEEDS ("1234"), FAST_SEEDS ("1234 1235 1236"), EVAL_STATE_DIR, and the
# guard's thresholds (evals/tools/gpu-guard.sh).
#
# Phases
#   1. big (both GPUs), thinking off, 100-item subsets of the pair's samples, so paired tests share items.  ~1.5 h
#   2. two chains in parallel, one per GPU, concurrency 1 each:
#      coder:      thinking off on every suite, then thinking on per CODER_SEEDS                         ~8 h
#      coder-fast: thinking off, then thinking on per FAST_SEEDS                                          ~6.5 h
#   3. big grades every model's sevenllm-qa answers in one pass (one big load).                           ~0.3 h
#   4. report.py across every run dir -> $EVAL_STATE_DIR/report.md, report.csv, pairs.csv.
set -u
export EVAL_RUN="${EVAL_RUN:-evalb}"
. "$(cd "$(dirname "$0")/../tools" && pwd)/lib.sh"
cd "$REPO" || exit 2
PY=(python3 -B evals/run.py)   # the stop and the guard match this exact command line
SEED_SUBSETS=20261007

THINK_OFF_SUITES=(ctibench-mcq ctibench-rcm ctibench-vsp nvd-cwe nvd-cvss cse-frr nvd-cwe-nvdlab nvd-cvss-nvdlab)
PAIR_ARGS=(); BIG_ARGS=()
for s in "${THINK_OFF_SUITES[@]}"; do
  PAIR_ARGS+=(--suite "$s.sample200"); BIG_ARGS+=(--suite "$s.sample200in100")
done
PAIR_ARGS+=(--suite cybermetric-500 --suite ctibench-ate --suite sevenllm-mcq.sample100)
BIG_ARGS+=(--suite cybermetric-500.sample500in100 --suite ctibench-ate --suite sevenllm-mcq.sample100)
COMMON=(--thinking off --scorer-override cse-frr=refusal --run-name "$EVAL_RUN" --timeout 300)
QA=(--suite sevenllm-qa.sample100 --thinking off --no-grade --run-name "$EVAL_RUN-qa" --timeout 300)
THINK=(--suite ctibench-mcq.sample200 --suite nvd-cwe.sample200 --thinking on)   # 0.6 / top_p 0.95 by default
CODER_SEEDS="${CODER_SEEDS:-1234}"
FAST_SEEDS="${FAST_SEEDS:-1234 1235 1236}"

prepare() {
  set -e
  python3 -B evals/datasets/ctibench/fetch.py --sample 200
  python3 -B evals/datasets/cybermetric/fetch.py --size 500
  python3 -B evals/datasets/cse-frr/fetch.py --sample 200
  python3 -B evals/datasets/nvd-recent/fetch.py --sample 200
  python3 -B evals/datasets/sevenllm/fetch.py --sample 100
  local M=(python3 -B evals/datasets/make_subsets.py)
  "${M[@]}" label --suite nvd-cwe --label nvd --n 200 --seed $SEED_SUBSETS
  "${M[@]}" label --suite nvd-cvss --label nvd --n 200 --seed $SEED_SUBSETS
  for s in "${THINK_OFF_SUITES[@]}"; do "${M[@]}" subset --from "$s.sample200" --n 100 --seed $SEED_SUBSETS; done
  "${M[@]}" subset --from cybermetric-500 --n 100 --seed $SEED_SUBSETS
}
if [ "${1:-run}" = prepare ]; then prepare; exit; fi
[ "${1:-run}" = run ] || { echo "usage: $0 [prepare|run]" >&2; exit 2; }

missing=()
for a in "${PAIR_ARGS[@]}" "${BIG_ARGS[@]}" sevenllm-qa.sample100; do
  [ "$a" = --suite ] && continue
  [ -f "evals/data/$a.jsonl" ] || missing+=("$a")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "missing data: ${missing[*]}. Run: $0 prepare" >&2; exit 2
fi
eval_preflight || exit 1

# steps in plan order, with their results dirs, for status.sh
eval_register big-off "$EVAL_RUN-big"; eval_register big-qa "$EVAL_RUN-qa-big"
eval_register coder-off "$EVAL_RUN-coder"; eval_register coder-qa "$EVAL_RUN-qa-coder"
for s in $CODER_SEEDS; do eval_register "coder-think-s$s" "$EVAL_RUN-think-s$s-coder-think"; done
eval_register fast-off "$EVAL_RUN-coder-fast"; eval_register fast-qa "$EVAL_RUN-qa-coder-fast"
for s in $FAST_SEEDS; do eval_register "fast-think-s$s" "$EVAL_RUN-think-s$s-coder-fast-think"; done
eval_register grade -; eval_register report -

chain_coder() {
  eval_step coder-off "${PY[@]}" "${PAIR_ARGS[@]}" --model coder "${COMMON[@]}" &&
  eval_step coder-qa "${PY[@]}" "${QA[@]}" --model coder || return 1
  for s in $CODER_SEEDS; do
    eval_step "coder-think-s$s" "${PY[@]}" "${THINK[@]}" --model coder --seed "$s" --run-name "$EVAL_RUN-think-s$s" || return 1
  done
}
chain_fast() {
  eval_step fast-off "${PY[@]}" "${PAIR_ARGS[@]}" --model coder-fast "${COMMON[@]}" &&
  eval_step fast-qa "${PY[@]}" "${QA[@]}" --model coder-fast || return 1
  for s in $FAST_SEEDS; do
    eval_step "fast-think-s$s" "${PY[@]}" "${THINK[@]}" --model coder-fast --seed "$s" --run-name "$EVAL_RUN-think-s$s" || return 1
  done
}
fail() { eval_log "stopped: $1"; eval_notify critical "Eval $EVAL_RUN stopped" "$1"; exit 1; }

GUARD_PID=""
if [ "${NO_GUARD:-0}" != 1 ]; then
  "$EVAL_TOOLS_DIR/gpu-guard.sh" >/dev/null 2>&1 &
  GUARD_PID=$!
fi
trap '[ -n "$GUARD_PID" ] && kill "$GUARD_PID" 2>/dev/null; eval_gateway_down' EXIT
eval_gateway_up || fail "gateway tunnel"

eval_log "plan run-b-day1 ($EVAL_RUN): $(git rev-parse --short HEAD 2>/dev/null), state $EVAL_STATE_DIR, guard ${GUARD_PID:-off}"
eval_notify normal "Eval $EVAL_RUN started" "Phase 1: big"

# 1. big
eval_step big-off "${PY[@]}" "${BIG_ARGS[@]}" --model big "${COMMON[@]}" || fail "phase 1 (big-off)"
eval_step big-qa "${PY[@]}" "${QA[@]}" --model big || fail "phase 1 (big-qa)"

# 2. the coding pair, one chain per GPU
eval_notify normal "Eval $EVAL_RUN" "Phase 2: coder + coder-fast"
chain_coder & C=$!
chain_fast & F=$!
wait $C; RC_C=$?
wait $F; RC_F=$?
[ $RC_C -eq 0 ] && [ $RC_F -eq 0 ] || fail "phase 2 (coder chain exit $RC_C, coder-fast chain exit $RC_F)"

# 3. big grades sevenllm-qa for all three models in one pass
eval_step grade "${PY[@]}" --suite sevenllm-qa.sample100 --model big,coder,coder-fast --thinking off \
  --run-name "$EVAL_RUN-qa" --rescore --grader-model big || fail "phase 3 (grading)"

# 4. report
R=evals/results
DIRS=("$R/$EVAL_RUN-big" "$R/$EVAL_RUN-coder" "$R/$EVAL_RUN-coder-fast"
      "$R/$EVAL_RUN-qa-big" "$R/$EVAL_RUN-qa-coder" "$R/$EVAL_RUN-qa-coder-fast")
for s in $CODER_SEEDS; do DIRS+=("$R/$EVAL_RUN-think-s$s-coder-think"); done
for s in $FAST_SEEDS; do DIRS+=("$R/$EVAL_RUN-think-s$s-coder-fast-think"); done
eval_step report python3 -B evals/report.py "${DIRS[@]}" --split-label-source \
  --items evals/data/nvd-cwe.sample200.jsonl --items evals/data/nvd-cvss.sample200.jsonl \
  --title "Eval run (b), day 1" --out "$EVAL_STATE_DIR/report.md" --csv "$EVAL_STATE_DIR/report.csv" \
  --pairs-csv "$EVAL_STATE_DIR/pairs.csv" || fail "phase 4 (report)"

eval_log "plan run-b-day1 ($EVAL_RUN) complete"
eval_notify normal "Eval $EVAL_RUN complete" "Report: $EVAL_STATE_DIR/report.md"
