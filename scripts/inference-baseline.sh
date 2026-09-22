#!/usr/bin/env bash
set -euo pipefail

# inference-baseline.sh — reproduce the Performance Baseline table in
# ARCHITECTURE.md section 4.
#
# RUN THIS ON THE VM (agent-sandbox-3090, user `operator`), not on the host.
# On bishop-X870-GAMING-WIFI6 vfio-pci owns both 3090s, so `nvidia-smi`
# there fails by design and there are no NVML counters to read. The VM is
# where the cards are visible and where Ollama listens (10.100.0.2:11434).
#
# It measures the metrics section 4 records, using the same row names, so a
# run before and after the ASUS ProArt X870E-Creator upgrade can be diffed
# line for line. The crux of that comparison is the PCIe link: today card 1
# is 4.0 x16 and card 2 is 4.0 x1 (chipset slot, one lane wired); x8/x8
# bifurcation should show as x8 on both.
#
# No sudo, and nothing is hardcoded — every number printed is read from
# Ollama's own counters or from NVML via nvidia-smi.
#
# Usage:
#   scripts/inference-baseline.sh [options]
#
# Options (env var equivalent in brackets):
#   --url URL            Ollama endpoint           [OLLAMA_URL]
#   --model TAG          model tag to benchmark    [MODEL]
#   --label NAME         label for this run        [LABEL]
#   --iterations N       generation passes         [ITERATIONS]
#   --num-predict N      tokens per pass           [NUM_PREDICT]
#   --prompt-reps N      long-prompt length        [PROMPT_REPS]
#   --seed N             sampler seed              [SEED]
#   --interval SECONDS   nvidia-smi sample period  [SAMPLE_INTERVAL]
#   --out-dir DIR        artifact directory        [OUT_DIR]
#   --cold-start         also measure a cold load  [COLD_START=1]
#   -h, --help
#
# Examples:
#   scripts/inference-baseline.sh --label pre-proart
#   LABEL=post-proart scripts/inference-baseline.sh --cold-start
#
# Caveats worth knowing before trusting a run:
#   - OLLAMA_NUM_PARALLEL=2 in production, so a real user query landing
#     mid-run shares the GPUs and drags tok/s down. Run it quiet.
#   - Throttle seconds are sampled, not integrated: they are
#     (samples seen Active) x SAMPLE_INTERVAL, so they resolve to roughly
#     one sample period. Section 4's 30-min burn figures came from a much
#     longer, heavier run; this is the inference-load equivalent.

OLLAMA_URL="${OLLAMA_URL:-http://10.100.0.2:11434}"
MODEL="${MODEL:-llama3.3-70b-fullgpu:latest}"
LABEL="${LABEL:-baseline}"
ITERATIONS="${ITERATIONS:-3}"
NUM_PREDICT="${NUM_PREDICT:-200}"
PROMPT_REPS="${PROMPT_REPS:-40}"
SEED="${SEED:-42}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-1}"
OUT_DIR="${OUT_DIR:-./baseline-results}"
COLD_START="${COLD_START:-0}"
IDLE_SAMPLES="${IDLE_SAMPLES:-5}"
CURL_MAX_TIME="${CURL_MAX_TIME:-900}"
# keep_alive to restore after a cold-start test. Production sets
# OLLAMA_KEEP_ALIVE=-1 (pin forever); keep that unless you know better.
RESTORE_KEEP_ALIVE="${RESTORE_KEEP_ALIVE:--1}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
warn() { printf 'warning: %s\n' "$*" >&2; }
note() { printf '  %s\n' "$*"; }
hr() { printf -- '--------------------------------------------------------------------\n'; }
usage() { awk 'NR > 3 && /^#/ { sub(/^# ?/, ""); print; next } NR > 3 { exit }' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) OLLAMA_URL="${2:?--url needs a value}"; shift 2 ;;
    --model) MODEL="${2:?--model needs a value}"; shift 2 ;;
    --label) LABEL="${2:?--label needs a value}"; shift 2 ;;
    --iterations) ITERATIONS="${2:?--iterations needs a value}"; shift 2 ;;
    --num-predict) NUM_PREDICT="${2:?--num-predict needs a value}"; shift 2 ;;
    --prompt-reps) PROMPT_REPS="${2:?--prompt-reps needs a value}"; shift 2 ;;
    --seed) SEED="${2:?--seed needs a value}"; shift 2 ;;
    --interval) SAMPLE_INTERVAL="${2:?--interval needs a value}"; shift 2 ;;
    --out-dir) OUT_DIR="${2:?--out-dir needs a value}"; shift 2 ;;
    --cold-start) COLD_START=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

OLLAMA_URL="${OLLAMA_URL%/}"
[[ "$ITERATIONS"  =~ ^[1-9][0-9]*$ ]] || die "--iterations must be a positive integer"
[[ "$NUM_PREDICT" =~ ^[1-9][0-9]*$ ]] || die "--num-predict must be a positive integer"
[[ "$PROMPT_REPS" =~ ^[1-9][0-9]*$ ]] || die "--prompt-reps must be a positive integer"
[[ "$SEED"        =~ ^[0-9]+$ ]]      || die "--seed must be a non-negative integer"
[[ "$IDLE_SAMPLES" =~ ^[1-9][0-9]*$ ]] || die "IDLE_SAMPLES must be a positive integer"
[[ "$SAMPLE_INTERVAL" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "--interval must be a number"

# ----------------------------------------------------------------- deps ---
# Fail loudly and early: a missing dependency must never become a zero.

for cmd in curl jq awk sed sort date mktemp nvidia-smi; do
  command -v "$cmd" >/dev/null 2>&1 || die "missing dependency: $cmd"
done

if ! nvidia-smi -L >/dev/null 2>&1; then
  die "nvidia-smi is installed but cannot talk to a GPU.
  On the host that is expected — vfio-pci owns both 3090s there.
  Run this inside the agent-sandbox-3090 VM instead."
fi
[[ "$(nvidia-smi -L | grep -c '^GPU ' || true)" -ge 1 ]] || die "nvidia-smi reported no GPUs"

if ! curl -fsS --max-time 10 -o /dev/null "$OLLAMA_URL/api/tags"; then
  die "cannot reach Ollama at $OLLAMA_URL
  Check: systemctl status ollama / ss -tlnp | grep 11434 / wg show wg0"
fi

TAGS_JSON="$(curl -fsS --max-time 15 "$OLLAMA_URL/api/tags")" \
  || die "Ollama answered but /api/tags could not be read"
if ! jq -e --arg m "$MODEL" '[.models[]?.name] | index($m)' >/dev/null 2>&1 <<<"$TAGS_JSON"; then
  printf 'error: model %s is not present on %s\navailable:\n' "$MODEL" "$OLLAMA_URL" >&2
  jq -r '.models[]?.name | "  " + .' <<<"$TAGS_JSON" >&2 || true
  exit 1
fi

# Driver 580 prefers clocks_event_reasons.*; clocks_throttle_reasons.* is
# kept as a deprecated alias on most builds but not all, so probe rather
# than assume, and carry on without throttle data if neither works.
THROTTLE_NS=""
for ns in clocks_throttle_reasons clocks_event_reasons; do
  if probe="$(nvidia-smi --query-gpu="${ns}.active" --format=csv,noheader 2>/dev/null)" \
     && [[ -n "$probe" && "$probe" != *"N/A"* && "$probe" != *"ot Supported"* ]]; then
    THROTTLE_NS="$ns"
    break
  fi
done
if [[ -z "$THROTTLE_NS" ]]; then
  warn "nvidia-smi exposes no usable clocks_{throttle,event}_reasons fields; throttle rows will read n/a"
fi

mkdir -p "$OUT_DIR" || die "cannot create output directory: $OUT_DIR"
[[ -w "$OUT_DIR" ]] || die "output directory is not writable: $OUT_DIR"

TMP_DIR="$(mktemp -d)"
SAMPLER_PID=""
cleanup() {
  [[ -z "$SAMPLER_PID" ]] || kill "$SAMPLER_PID" 2>/dev/null || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

IDLE_CSV="$TMP_DIR/idle.csv"
LOAD_CSV="$TMP_DIR/load.csv"
IDLE_TSV="$TMP_DIR/idle.tsv"
LOAD_TSV="$TMP_DIR/load.tsv"
STATIC_CSV="$TMP_DIR/static.csv"
ITER_FILE="$TMP_DIR/iterations.ndjson"
: >"$IDLE_CSV"; : >"$LOAD_CSV"; : >"$IDLE_TSV"; : >"$LOAD_TSV"; : >"$ITER_FILE"

# -------------------------------------------------------------- helpers ---

trim() { printf '%s' "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'; }

# Print a number to N decimals; "n/a" for empty or JSON null.
fmt() {
  local v="${1-}" d="${2:-2}"
  if [[ -z "$v" || "$v" == "null" ]]; then printf 'n/a'; return 0; fi
  printf '%.*f' "$d" "$v"
}

# index, power.draw, temperature.gpu, memory.used [, 3 throttle flags, mask]
sample_gpus() {
  local q="index,power.draw,temperature.gpu,memory.used"
  if [[ -n "$THROTTLE_NS" ]]; then
    q+=",${THROTTLE_NS}.sw_power_cap,${THROTTLE_NS}.hw_thermal_slowdown"
    q+=",${THROTTLE_NS}.sw_thermal_slowdown,${THROTTLE_NS}.active"
  fi
  nvidia-smi --query-gpu="$q" --format=csv,noheader,nounits
}

# Poll nvidia-smi in the background so power and temperature are captured
# *while* the generation request is in flight. The first sample is taken
# after one interval, so it lands inside the request instead of on the
# idle edge just before it.
start_sampler() {
  local out="$1"
  while :; do
    sleep "$SAMPLE_INTERVAL"
    sample_gpus >>"$out" 2>/dev/null || true
  done &
  SAMPLER_PID=$!
}

stop_sampler() {
  [[ -n "$SAMPLER_PID" ]] || return 0
  kill "$SAMPLER_PID" 2>/dev/null || true
  wait "$SAMPLER_PID" 2>/dev/null || true
  SAMPLER_PID=""
}

# Reduce a sample file to one TSV row per card:
#  1 idx  2 p_mean  3 p_peak  4 t_mean  5 t_peak  6 mem_peak
#  7 swcap_s  8 hwtherm_s  9 swtherm_s  10 samples  11 masks
gpu_stats() {
  awk -F'[[:space:]]*,[[:space:]]*' -v iv="$SAMPLE_INTERVAL" -v thr="${THROTTLE_NS:-}" '
    function isnum(v) { return (v ~ /^-?[0-9]+(\.[0-9]+)?$/) }
    function f1(v) { return sprintf("%.1f", v) }
    function f0(v) { return sprintf("%.0f", v) }
    $1 ~ /^[0-9]+$/ {
      i = $1 + 0; seen[i] = 1; n[i]++
      if (isnum($2)) { pn[i]++; ps[i] += $2; if (!(i in pk) || $2 + 0 > pk[i]) pk[i] = $2 + 0 }
      if (isnum($3)) { tn[i]++; ts[i] += $3; if (!(i in tk) || $3 + 0 > tk[i]) tk[i] = $3 + 0 }
      if (isnum($4) && (!(i in mk) || $4 + 0 > mk[i])) mk[i] = $4 + 0
      if (thr != "") {
        if ($5 == "Active") cap[i]++
        if ($6 == "Active") hwt[i]++
        if ($7 == "Active") swt[i]++
        if (NF >= 8 && !((i "|" $8) in maskseen)) {
          maskseen[i "|" $8] = 1
          masks[i] = ((i in masks) ? masks[i] "," $8 : $8)
        }
      }
    }
    END {
      for (i in seen) {
        if (thr == "") { c = "null"; h = "null"; s = "null"; m = "n/a" }
        else {
          c = f1((i in cap ? cap[i] : 0) * iv)
          h = f1((i in hwt ? hwt[i] : 0) * iv)
          s = f1((i in swt ? swt[i] : 0) * iv)
          m = ((i in masks) ? masks[i] : "none")
        }
        printf "%d\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%d\t%s\n", i,
          (pn[i] ? f1(ps[i] / pn[i]) : "null"), ((i in pk) ? f1(pk[i]) : "null"),
          (tn[i] ? f1(ts[i] / tn[i]) : "null"), ((i in tk) ? f0(tk[i]) : "null"),
          ((i in mk) ? f0(mk[i]) : "null"), c, h, s, n[i], m
      }
    }' "$1" | sort -n
}

# "card 1: <col><suffix>  |  card 2: ..." for the summary table.
cards_col() {
  local file="$1" col="$2" suffix="${3:-}"
  if [[ ! -s "$file" ]]; then printf 'n/a'; return 0; fi
  awk -F'\t' -v c="$col" -v suf="$suffix" '
    {
      v = ($c == "null" ? "n/a" : $c suf)
      printf "%scard %d: %s", (NR > 1 ? "  |  " : ""), $1 + 1, v
    }
    END { printf "\n" }' "$file"
}

per_card_json() {
  local file="$1"
  if [[ ! -s "$file" ]]; then printf '[]'; return 0; fi
  awk -F'\t' '
    function jnum(v) { return (v == "null" ? "null" : v) }
    {
      printf "{\"card\":%d,\"gpu_index\":%d,\"power_w_mean\":%s,\"power_w_peak\":%s,", $1 + 1, $1, jnum($2), jnum($3)
      printf "\"temp_c_mean\":%s,\"temp_c_peak\":%s,\"vram_used_mib_peak\":%s,", jnum($4), jnum($5), jnum($6)
      printf "\"throttle_sw_power_cap_s\":%s,\"throttle_hw_thermal_s\":%s,", jnum($7), jnum($8)
      printf "\"throttle_sw_thermal_s\":%s,\"samples\":%d,\"throttle_masks_seen\":\"%s\"}\n", jnum($9), $10, $11
    }' "$file" | jq -s .
}

pcie_summary() {
  # static.csv: idx, name, bus, mem_total, gen_max, gen_cur, width_max, width_cur
  awk -F'[[:space:]]*,[[:space:]]*' '
    NF >= 8 { printf "%scard %d: PCIe %d x%d", (NR > 1 ? "  |  " : ""), $1 + 1, $6 + 0, $8 + 0 }
    END { printf "\n" }' "$STATIC_CSV"
}

# A long, fixed prompt, so prompt_eval_count is big enough for prompt
# processing to be measurable. num_ctx is 4096, so 40 reps (~1.6k tokens)
# plus num_predict leaves headroom.
#
# The pass number goes at the very front on purpose: llama.cpp keeps a
# prompt-prefix cache, so re-sending an identical prompt returns
# prompt_eval_count ~0 and a meaningless prompt rate. A differing first
# token invalidates the prefix and forces a real prefill every pass, while
# staying deterministic (pass N is identical from run to run).
build_prompt() {
  local pass="$1" i out
  out="Pass ${pass}. "
  for ((i = 1; i <= PROMPT_REPS; i++)); do
    out+="Paragraph ${i}: the inference host runs a Llama 3.3 70B Instruct model quantised to Q4_K_M, served by Ollama inside a GPU-passthrough virtual machine across two RTX 3090 cards joined by an asymmetric PCIe fabric, reached over a WireGuard tunnel from an EC2 frontend. "
  done
  out+=$'\n''Answer in exactly one short sentence: which machine performs the inference?'
  printf '%s' "$out"
}

# $1 keep_alive ("default" omits the field so the server's
# OLLAMA_KEEP_ALIVE applies), $2 num_predict, $3 prompt. Prints response JSON.
generate() {
  local keep="$1" npredict="$2" prompt="$3" payload resp
  payload="$(jq -n --arg m "$MODEL" --arg p "$prompt" \
    --argjson n "$npredict" --argjson s "$SEED" \
    '{model: $m, prompt: $p, stream: false,
      options: {num_predict: $n, temperature: 0, seed: $s, top_k: 1, top_p: 1, repeat_penalty: 1}}')"
  if [[ "$keep" != "default" ]]; then
    payload="$(jq --argjson k "$keep" '. + {keep_alive: $k}' <<<"$payload")"
  fi
  resp="$(curl -fsS --max-time "$CURL_MAX_TIME" -H 'Content-Type: application/json' \
    -d "$payload" "$OLLAMA_URL/api/generate")" || return 1
  jq -e 'has("eval_count")' >/dev/null 2>&1 <<<"$resp" || return 1
  printf '%s' "$resp"
}

model_resident() {
  curl -fsS --max-time 10 "$OLLAMA_URL/api/ps" 2>/dev/null \
    | jq -e --arg m "$MODEL" '[.models[]?.name] | index($m)' >/dev/null 2>&1
}

# ----------------------------------------------------------------- run ----

TS="$(date -u +%Y%m%dT%H%M%SZ)"
LABEL_SAFE="${LABEL//[^A-Za-z0-9._-]/_}"
JSON_OUT="$OUT_DIR/baseline-${LABEL_SAFE}-${TS}.json"
TSV_OUT="$OUT_DIR/baseline-${LABEL_SAFE}-${TS}.tsv"
PROMPT_SAMPLE="$(build_prompt 1)"

printf 'inference baseline — label=%s  %s\n' "$LABEL" "$TS"
hr
note "endpoint    $OLLAMA_URL"
note "model       $MODEL"
note "generation  $ITERATIONS pass(es) x $NUM_PREDICT tokens, temperature 0, seed $SEED"
note "prompt      $PROMPT_REPS reps, ${#PROMPT_SAMPLE} chars (~$(( ${#PROMPT_SAMPLE} / 4 )) tokens, rough)"
note "sampling    nvidia-smi every ${SAMPLE_INTERVAL}s"
if [[ "$COLD_START" == "1" ]]; then
  note "cold start  ENABLED — the model will be evicted and reloaded"
else
  note "cold start  skipped (pass --cold-start to measure it)"
fi
hr

# PCIe link first: it is the single clearest before/after signal.
nvidia-smi --format=csv,noheader,nounits \
  --query-gpu=index,name,pci.bus_id,memory.total,pcie.link.gen.max,pcie.link.gen.current,pcie.link.width.max,pcie.link.width.current \
  >"$STATIC_CSV" || die "nvidia-smi could not read PCIe link state"
[[ -s "$STATIC_CSV" ]] || die "nvidia-smi returned no GPU rows"

printf 'PCIe link (x1 on card 2 today; the ProArt upgrade should show x8/x8)\n'
while IFS=',' read -r s_idx s_name s_bus s_memtot s_genmax s_gencur s_widmax s_widcur; do
  [[ -n "${s_idx// /}" ]] || continue
  printf '  card %d  %-24s %s  %s MiB  current PCIe %s x%s  (max PCIe %s x%s)\n' \
    "$(( $(trim "$s_idx") + 1 ))" "$(trim "$s_name")" "$(trim "$s_bus")" \
    "$(trim "$s_memtot")" "$(trim "$s_gencur")" "$(trim "$s_widcur")" \
    "$(trim "$s_genmax")" "$(trim "$s_widmax")"
done <"$STATIC_CSV"
hr

# Idle power and thermals, before anything is asked of the GPUs. In
# production the model is already resident (OLLAMA_KEEP_ALIVE=-1), so this
# is "idle with ~42 GB pinned" — exactly what section 4 recorded.
printf 'sampling idle power (%s samples, %ss apart)...\n' "$IDLE_SAMPLES" "$SAMPLE_INTERVAL"
for ((k = 1; k <= IDLE_SAMPLES; k++)); do
  sample_gpus >>"$IDLE_CSV" 2>/dev/null || true
  [[ "$k" -eq "$IDLE_SAMPLES" ]] || sleep "$SAMPLE_INTERVAL"
done
gpu_stats "$IDLE_CSV" >"$IDLE_TSV"
[[ -s "$IDLE_TSV" ]] || warn "no usable idle samples from nvidia-smi"

# ------------------------------------------------------------ cold start ---
# Opt-in, because evicting the production model makes the next real user
# wait 15-20s for a reload.
#
# OLLAMA_KEEP_ALIVE=-1 pins the model forever, so waiting will never evict
# it and an ordinary request will not either. Eviction has to be explicit:
# POST /api/generate with keep_alive 0 and an empty prompt, which unloads
# immediately and returns without generating. That is what `ollama stop
# <model>` does, but over the API, so this works from anywhere on the mesh
# rather than only where the CLI lives. The reload is then issued with
# keep_alive=$RESTORE_KEEP_ALIVE so the production pin is put back and the
# eviction costs exactly one load, not an unpinned model that drops out
# again five minutes later.

COLD_JSON='null'
COLD_LOAD_S=""
if [[ "$COLD_START" == "1" ]]; then
  printf 'cold start: evicting %s ...\n' "$MODEL"
  curl -fsS --max-time 120 -o /dev/null -H 'Content-Type: application/json' \
    -d "$(jq -n --arg m "$MODEL" '{model: $m, prompt: "", stream: false, keep_alive: 0}')" \
    "$OLLAMA_URL/api/generate" || die "eviction request failed; model left as it was"

  for ((k = 1; k <= 60; k++)); do
    model_resident || break
    sleep 1
  done
  if model_resident; then
    die "$MODEL is still resident after 60s; refusing to report a fake cold start"
  fi
  note "evicted, VRAM released"

  printf 'cold start: timing reload (keep_alive=%s)...\n' "$RESTORE_KEEP_ALIVE"
  cold_t0="$(date +%s%N)"
  if ! cold_resp="$(generate "$RESTORE_KEEP_ALIVE" 1 'Reply with the single word: ready')"; then
    die "cold reload failed — the model may now be unloaded. Re-warm it with:
  curl -s $OLLAMA_URL/api/generate -d '{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"keep_alive\":$RESTORE_KEEP_ALIVE}'"
  fi
  cold_t1="$(date +%s%N)"
  COLD_LOAD_S="$(jq -r '(.load_duration // 0) / 1e9' <<<"$cold_resp")"
  cold_wall_s="$(awk -v a="$cold_t0" -v b="$cold_t1" 'BEGIN { printf "%.3f", (b - a) / 1e9 }')"
  COLD_JSON="$(jq -n --argjson load "$COLD_LOAD_S" --argjson wall "$cold_wall_s" \
    --arg restored "$RESTORE_KEEP_ALIVE" \
    '{load_duration_s: $load, wall_clock_s: $wall, keep_alive_restored_to: $restored}')"
  note "load_duration $(fmt "$COLD_LOAD_S" 2)s (wall clock $(fmt "$cold_wall_s" 2)s)"
elif ! model_resident; then
  # Warm up first, otherwise pass 1 would silently include a 15-20s load.
  printf 'model not resident; warming it (this load is NOT timed)...\n'
  generate default 1 'Reply with the single word: ready' >/dev/null \
    || die "warm-up request failed"
fi
hr

# ------------------------------------------------------------ generation ---

printf 'running %s generation pass(es)...\n' "$ITERATIONS"
for ((i = 1; i <= ITERATIONS; i++)); do
  iter_prompt="$(build_prompt "$i")"
  start_sampler "$LOAD_CSV"
  iter_t0="$(date +%s%N)"
  if ! resp="$(generate default "$NUM_PREDICT" "$iter_prompt")"; then
    stop_sampler
    die "generation pass $i failed against $OLLAMA_URL"
  fi
  iter_t1="$(date +%s%N)"
  stop_sampler

  wall_s="$(awk -v a="$iter_t0" -v b="$iter_t1" 'BEGIN { printf "%.3f", (b - a) / 1e9 }')"
  # tok/s comes from the server's own eval_count / eval_duration (ns), so
  # network, JSON and curl overhead are excluded — no wall-clock guessing.
  jq -c --argjson iter "$i" --argjson wall "$wall_s" '{
      iteration: $iter,
      eval_count: .eval_count,
      eval_duration_ns: .eval_duration,
      prompt_eval_count: .prompt_eval_count,
      prompt_eval_duration_ns: .prompt_eval_duration,
      load_duration_ns: .load_duration,
      total_duration_ns: .total_duration,
      wall_clock_s: $wall,
      tok_per_s: (if ((.eval_duration // 0) > 0) then (.eval_count / (.eval_duration / 1e9)) else null end),
      prompt_tok_per_s: (if ((.prompt_eval_duration // 0) > 0) then (.prompt_eval_count / (.prompt_eval_duration / 1e9)) else null end)
    }' <<<"$resp" >>"$ITER_FILE"

  note "$(jq -r '"pass \(.iteration): \(.eval_count // 0) tok in \(((.eval_duration_ns // 0) / 1e7 | round) / 100)s = \(((.tok_per_s // 0) * 100 | round) / 100) tok/s | prompt \(.prompt_eval_count // 0) tok @ \(((.prompt_tok_per_s // 0) * 10 | round) / 10) tok/s"' \
    <<<"$(tail -n 1 "$ITER_FILE")")"
done

gpu_stats "$LOAD_CSV" >"$LOAD_TSV"
[[ -s "$LOAD_TSV" ]] || warn "no nvidia-smi samples landed during generation; try --interval 0.5"

# --------------------------------------------------------------- summary ---

mean_of() {
  jq -s --arg k "$1" '[.[][$k] | select(. != null)] | if length > 0 then (add / length) else null end' "$ITER_FILE"
}

GEN_MEAN="$(mean_of tok_per_s)"
PROMPT_MEAN="$(mean_of prompt_tok_per_s)"
WARM_LOAD_MEAN="$(jq -s '[.[].load_duration_ns | select(. != null)]
  | if length > 0 then (add / length / 1e9) else null end' "$ITER_FILE")"
GEN_EACH="$(jq -rs '[.[].tok_per_s | select(. != null) | (. * 100 | round) / 100 | tostring]
  | if length > 0 then join(", ") else "n/a" end' "$ITER_FILE")"
PROMPT_EACH="$(jq -rs '[.[].prompt_tok_per_s | select(. != null) | (. * 10 | round) / 10 | tostring]
  | if length > 0 then join(", ") else "n/a" end' "$ITER_FILE")"
PROMPT_TOKENS="$(jq -rs '[.[].prompt_eval_count | select(. != null) | tostring] | unique | join("/")' "$ITER_FILE")"

hr
printf 'Performance Baseline — %s (compare against ARCHITECTURE.md section 4)\n\n' "$LABEL"
row() { printf '| %-38s | %s\n' "$1" "$2"; }
row "Metric" "Value"
printf '|-%s-|-%s\n' "$(printf '%38s' '' | tr ' ' '-')" "----------------------------------------"

row "Model" "$MODEL"
row "PCIe link per card" "$(pcie_summary)"
row "VRAM allocation" "$(cards_col "$LOAD_TSV" 6 " MiB")"
if [[ "$COLD_START" == "1" ]]; then
  row "Cold start (Ollama load)" "$(fmt "$COLD_LOAD_S" 2) s"
else
  row "Cold start (Ollama load)" "not measured (pass --cold-start)"
fi
row "Warm start (already loaded)" "$(fmt "$WARM_LOAD_MEAN" 3) s"
row "Token generation rate" "$(fmt "$GEN_MEAN" 2) tok/s mean [$GEN_EACH]"
row "Prompt processing" "$(fmt "$PROMPT_MEAN" 1) tok/s mean [$PROMPT_EACH] over $PROMPT_TOKENS tok"
row "Idle power per card" "$(cards_col "$IDLE_TSV" 2 " W")"
row "Sustained inference power per card" "$(cards_col "$LOAD_TSV" 2 " W")"
row "Peak inference power per card" "$(cards_col "$LOAD_TSV" 3 " W")"
row "Idle thermal per card" "$(cards_col "$IDLE_TSV" 4 " C")"
row "Peak thermal per card under load" "$(cards_col "$LOAD_TSV" 5 " C")"
row "Throttle, sw power cap (this run)" "$(cards_col "$LOAD_TSV" 7 " s")"
row "Throttle, hw thermal (this run)" "$(cards_col "$LOAD_TSV" 8 " s")"
row "Throttle, sw thermal (this run)" "$(cards_col "$LOAD_TSV" 9 " s")"
row "Throttle reason masks seen" "$(cards_col "$LOAD_TSV" 11)"
row "Load samples per card" "$(cards_col "$LOAD_TSV" 10)"
printf '\n'

# ------------------------------------------------------------- artifacts ---

DRIVER_VER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n 1 | tr -d '[:space:]')"
OLLAMA_VER="$(curl -fsS --max-time 10 "$OLLAMA_URL/api/version" 2>/dev/null \
  | jq -r '.version // "unknown"' 2>/dev/null || true)"
OLLAMA_VER="${OLLAMA_VER:-unknown}"

GPU_STATIC_JSON="$(awk -F'[[:space:]]*,[[:space:]]*' '
  BEGIN { printf "[" }
  NF >= 8 {
    printf "%s{\"card\":%d,\"gpu_index\":%d,\"name\":\"%s\",\"pci_bus_id\":\"%s\",", (rows++ ? "," : ""), $1 + 1, $1, $2, $3
    printf "\"vram_total_mib\":%d,\"pcie_gen_current\":%d,\"pcie_gen_max\":%d,", $4 + 0, $6 + 0, $5 + 0
    printf "\"pcie_width_current\":%d,\"pcie_width_max\":%d,", $8 + 0, $7 + 0
    printf "\"pcie_link_current\":\"PCIe %d x%d\"}", $6 + 0, $8 + 0
  }
  END { printf "]\n" }' "$STATIC_CSV")"

jq -n \
  --arg schema "inference-baseline/1" \
  --arg label "$LABEL" \
  --arg ts "$TS" \
  --arg runner "${HOSTNAME:-unknown}" \
  --arg endpoint "$OLLAMA_URL" \
  --arg model "$MODEL" \
  --arg driver "$DRIVER_VER" \
  --arg ollama "$OLLAMA_VER" \
  --arg throttle_ns "${THROTTLE_NS:-unsupported}" \
  --argjson config "$(jq -n --argjson it "$ITERATIONS" --argjson np "$NUM_PREDICT" \
      --argjson seed "$SEED" --argjson reps "$PROMPT_REPS" --argjson iv "$SAMPLE_INTERVAL" \
      --argjson cold "$([[ "$COLD_START" == "1" ]] && printf true || printf false)" \
      '{iterations: $it, num_predict: $np, seed: $seed, temperature: 0,
        prompt_reps: $reps, sample_interval_s: $iv, cold_start_measured: $cold}')" \
  --argjson gpus "$GPU_STATIC_JSON" \
  --argjson cold_start "$COLD_JSON" \
  --argjson iterations "$(jq -s . "$ITER_FILE")" \
  --argjson idle "$(per_card_json "$IDLE_TSV")" \
  --argjson load "$(per_card_json "$LOAD_TSV")" \
  --argjson gen_mean "$GEN_MEAN" \
  --argjson prompt_mean "$PROMPT_MEAN" \
  --argjson warm_mean "$WARM_LOAD_MEAN" \
  '{schema: $schema, label: $label, timestamp_utc: $ts, runner_host: $runner,
    endpoint: $endpoint, model: $model, nvidia_driver: $driver, ollama_version: $ollama,
    nvidia_smi_throttle_namespace: $throttle_ns, config: $config, gpus: $gpus,
    cold_start: $cold_start,
    summary: {token_generation_tok_s_mean: $gen_mean,
              prompt_processing_tok_s_mean: $prompt_mean,
              warm_load_s_mean: $warm_mean},
    iterations: $iterations, per_card_idle: $idle, per_card_load: $load}' >"$JSON_OUT"

{
  printf 'metric\tvalue\n'
  printf 'label\t%s\n' "$LABEL"
  printf 'timestamp_utc\t%s\n' "$TS"
  printf 'model\t%s\n' "$MODEL"
  printf 'pcie_link_per_card\t%s\n' "$(pcie_summary)"
  printf 'vram_used_mib_per_card\t%s\n' "$(cards_col "$LOAD_TSV" 6)"
  printf 'cold_start_s\t%s\n' "$(fmt "$COLD_LOAD_S" 2)"
  printf 'warm_start_s\t%s\n' "$(fmt "$WARM_LOAD_MEAN" 3)"
  printf 'token_generation_tok_s\t%s\n' "$(fmt "$GEN_MEAN" 2)"
  printf 'prompt_processing_tok_s\t%s\n' "$(fmt "$PROMPT_MEAN" 1)"
  printf 'idle_power_w_per_card\t%s\n' "$(cards_col "$IDLE_TSV" 2)"
  printf 'sustained_power_w_per_card\t%s\n' "$(cards_col "$LOAD_TSV" 2)"
  printf 'peak_power_w_per_card\t%s\n' "$(cards_col "$LOAD_TSV" 3)"
  printf 'peak_temp_c_per_card\t%s\n' "$(cards_col "$LOAD_TSV" 5)"
  printf 'throttle_sw_power_cap_s_per_card\t%s\n' "$(cards_col "$LOAD_TSV" 7)"
  printf 'throttle_hw_thermal_s_per_card\t%s\n' "$(cards_col "$LOAD_TSV" 8)"
  printf 'throttle_sw_thermal_s_per_card\t%s\n' "$(cards_col "$LOAD_TSV" 9)"
} >"$TSV_OUT"

note "wrote $JSON_OUT"
note "wrote $TSV_OUT"

if [[ "$COLD_START" == "1" ]]; then
  if model_resident; then
    note "model resident again, keep_alive restored to $RESTORE_KEEP_ALIVE"
  else
    warn "$MODEL is not resident after the run; the next user query pays a reload"
  fi
fi
