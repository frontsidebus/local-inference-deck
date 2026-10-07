#!/usr/bin/env bash
# GPU safety stop for long eval runs. Polls the backend's GPUs over SSH, logs every sample to
# $EVAL_STATE_DIR/gpu.csv, and stops the run cleanly (STOP file, then SIGINT to evals/run.py, which saves partial
# results; re-running the plan resumes) when:
#   - a GPU core is at or above STOP_TEMP_C for STOP_SAMPLES samples in a row;
#   - a hardware protection fires (HW slowdown, HW thermal slowdown, HW power brake): stop at once;
#   - power draw stays above the card's limit by POWER_MARGIN for STOP_SAMPLES samples;
#   - a fan reads 0% while the core is at or above FAN_DEAD_TEMP_C for STOP_SAMPLES samples;
#   - no reading arrives for STALE_S seconds (fail safe: we can't see the cards, so we stop).
# WARN_TEMP_C only notifies, and so does SW thermal slowdown covering more than SWT_WARN_PCT of an interval.
# RTX 3090 reference points: the driver's target is 83 C (it throttles there, which is normal), max operating
# 93 C, slowdown 95 C, shutdown 98 C (nvidia-smi -q -d TEMPERATURE). GeForce cards don't report memory-junction
# or hotspot temperature through nvidia-smi, so those are NOT monitored; SW thermal slowdown with a cool core is
# the only hint of them.
#
# Usage: EVAL_RUN=<prefix> evals/tools/gpu-guard.sh     (plans start it; it also runs on its own)
# Env:   EVAL_GPU_SSH (default BACKEND_SSH_USER@BACKEND_LAN_IP from site.env), INTERVAL (30), STOP_TEMP_C (88),
#        WARN_TEMP_C (84), STOP_SAMPLES (2), POWER_MARGIN (1.05), FAN_DEAD_TEMP_C (75), STALE_S (300),
#        SWT_WARN_PCT (50), DRY_RUN=1 (log and notify, never stop the run); see lib.sh for EVAL_STATE_DIR
set -u
. "$(dirname "$0")/lib.sh"
GPU_SSH="${EVAL_GPU_SSH:-}"
if [ -z "$GPU_SSH" ]; then
  u="$(site_value BACKEND_SSH_USER)"; h="$(site_value BACKEND_LAN_IP)"
  [ -n "$h" ] && GPU_SSH="${u:+$u@}$h"
fi
[ -n "$GPU_SSH" ] || { echo "set EVAL_GPU_SSH or BACKEND_LAN_IP in site.env" >&2; exit 2; }
INTERVAL="${INTERVAL:-30}"
STOP_TEMP_C="${STOP_TEMP_C:-88}"
WARN_TEMP_C="${WARN_TEMP_C:-84}"
STOP_SAMPLES="${STOP_SAMPLES:-2}"
POWER_MARGIN="${POWER_MARGIN:-1.05}"
FAN_DEAD_TEMP_C="${FAN_DEAD_TEMP_C:-75}"
STALE_S="${STALE_S:-300}"
SWT_WARN_PCT="${SWT_WARN_PCT:-50}"
DRY_RUN="${DRY_RUN:-0}"
CSV="$EVAL_STATE_DIR/gpu.csv"

# clocks_event_reasons bits that mean a hardware protection fired
HW_SLOWDOWN=$((0x08)); HW_THERMAL=$((0x40)); HW_BRAKE=$((0x80)); SW_THERMAL=$((0x20)); SW_POWER=$((0x04))

log() { echo "$(date -u +%FT%TZ) gpu-guard: $*" | tee -a "$EVAL_STATE_DIR/gpu-guard.log" >&2; }
notify() { eval_notify "$1" "GPU guard" "$2"; }  # urgency message

stop_run() {
  local reason="$1"
  log "STOP: $reason"
  notify critical "Stopping eval run $EVAL_RUN: $reason"
  if [ "$DRY_RUN" = 1 ]; then log "DRY_RUN=1: not stopping"; return; fi
  eval_stop "$reason"
  log "run stopped; see $STOP_FILE. Delete it and re-run the plan to resume."
  exit 0
}

[ -s "$CSV" ] || echo "ts,gpu,temp_c,power_w,power_limit_w,reasons,fan_pct,util_pct,mem_used_mib,sm_mhz,sw_power_cap_us,sw_thermal_us,hw_thermal_us,hw_brake_us" > "$CSV"
log "started: stop at ${STOP_TEMP_C} C x${STOP_SAMPLES}, warn at ${WARN_TEMP_C} C, power > limit x${POWER_MARGIN}, stale ${STALE_S}s, every ${INTERVAL}s, ssh ${GPU_SSH#*@}$([ "$DRY_RUN" = 1 ] && echo " (DRY RUN)")"

declare -A hot=() over=() fan0=() warned=() prev_swt=() prev_ts=() swt_warned=()
last_ok=$(date +%s)
fail_notified=0

while :; do
  [ -e "$STOP_FILE" ] && { log "STOP file present; exiting"; exit 0; }
  out=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "$GPU_SSH" '
    nvidia-smi --query-gpu=index,temperature.gpu,power.draw,power.limit,clocks_event_reasons.active,fan.speed,utilization.gpu,memory.used,clocks.sm --format=csv,noheader,nounits &&
    echo "--" &&
    nvidia-smi -q -d PERFORMANCE | grep -E "^GPU |SW Power Capping|SW Thermal Slowdown +: [0-9]|HW Thermal Slowdown +: [0-9]|HW Power Braking"' 2>/dev/null)
  now=$(date +%s); ts=$(date -u +%FT%TZ)
  if [ -z "$out" ] || ! grep -q -- '^--$' <<<"$out"; then
    age=$((now - last_ok))
    log "no reading (${age}s since the last good one)"
    [ $fail_notified = 0 ] && notify normal "Can't read the backend GPUs (will stop after ${STALE_S}s)" && fail_notified=1
    [ $age -ge "$STALE_S" ] && stop_run "no GPU reading for ${age}s"
    sleep "$INTERVAL"; continue
  fi
  last_ok=$now; fail_notified=0
  # counters: one block per GPU, in index order
  mapfile -t ctr < <(awk '/^GPU /{g++} /SW Power Capping/{p[g]=$(NF-1)} /SW Thermal Slowdown/{s[g]=$(NF-1)}
    /HW Thermal Slowdown/{h[g]=$(NF-1)} /HW Power Braking/{b[g]=$(NF-1)}
    END{for(i=1;i<=g;i++) print p[i]","s[i]","h[i]","b[i]}' <<<"${out#*--}")
  i=0
  while IFS=, read -r idx temp pw lim reasons fan util mem sm; do
    idx=${idx// /}; temp=${temp// /}; pw=${pw// /}; lim=${lim// /}; reasons=${reasons// /}; fan=${fan// /}
    echo "$ts,$idx,$temp,$pw,$lim,$reasons,$fan,${util// /},${mem// /},${sm// /},${ctr[$i]:-,,,}" >> "$CSV"
    # SW thermal slowdown with a cool core points at the memory junction or hotspot, which nvidia-smi can't read
    # on GeForce. Notify (once per hour per GPU) when it covers more than SWT_WARN_FRAC of an interval.
    swt=$(cut -d, -f2 <<<"${ctr[$i]:-}")
    if [[ $swt =~ ^[0-9]+$ ]] && [ -n "${prev_swt[$idx]:-}" ]; then
      dt=$(( now - ${prev_ts[$idx]} )); dswt=$(( (swt - ${prev_swt[$idx]}) / 1000000 ))
      if (( dt > 0 && dswt * 100 > dt * SWT_WARN_PCT )) && (( now - ${swt_warned[$idx]:-0} > 3600 )); then
        swt_warned[$idx]=$now
        log "warn: GPU$idx in SW thermal slowdown for ${dswt}s of the last ${dt}s at core ${temp} C (memory junction or hotspot?)"
        notify normal "GPU$idx thermally throttling at core ${temp} C: likely VRAM/hotspot heat (not readable). Check airflow."
      fi
    fi
    [[ $swt =~ ^[0-9]+$ ]] && { prev_swt[$idx]=$swt; prev_ts[$idx]=$now; }
    i=$((i + 1))
    r=$((reasons))
    if (( r & (HW_SLOWDOWN | HW_THERMAL | HW_BRAKE) )); then
      stop_run "GPU$idx hardware protection active (clocks_event_reasons $reasons) at ${temp} C, ${pw} W"
    fi
    if [[ $temp =~ ^[0-9]+$ ]]; then
      if (( temp >= STOP_TEMP_C )); then hot[$idx]=$(( ${hot[$idx]:-0} + 1 )); else hot[$idx]=0; fi
      (( ${hot[$idx]} >= STOP_SAMPLES )) && stop_run "GPU$idx at ${temp} C for ${hot[$idx]} samples (limit ${STOP_TEMP_C} C)"
      if (( temp >= WARN_TEMP_C )) && [ -z "${warned[$idx]:-}" ]; then
        warned[$idx]=1; log "warn: GPU$idx at ${temp} C"; notify normal "GPU$idx at ${temp} C (stop at ${STOP_TEMP_C} C)"
      fi
      (( temp < WARN_TEMP_C - 3 )) && unset "warned[$idx]"
      if [[ $fan =~ ^[0-9]+$ ]] && (( fan == 0 && temp >= FAN_DEAD_TEMP_C )); then
        fan0[$idx]=$(( ${fan0[$idx]:-0} + 1 ))
        (( ${fan0[$idx]} >= STOP_SAMPLES )) && stop_run "GPU$idx fan reads 0% at ${temp} C"
      else fan0[$idx]=0; fi
    fi
    if [[ $pw =~ ^[0-9.]+$ && $lim =~ ^[0-9.]+$ ]]; then
      if awk -v p="$pw" -v l="$lim" -v m="$POWER_MARGIN" 'BEGIN{exit !(p > l*m)}'; then over[$idx]=$(( ${over[$idx]:-0} + 1 )); else over[$idx]=0; fi
      (( ${over[$idx]} >= STOP_SAMPLES )) && stop_run "GPU$idx drawing ${pw} W over its ${lim} W limit"
    fi
  done < <(sed '/^--$/,$d' <<<"$out")
  sleep "$INTERVAL"
done
