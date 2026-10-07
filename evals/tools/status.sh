#!/usr/bin/env bash
# Progress of an eval plan: each step's state and item count (from the steps the plan registered), the last log
# line, and the GPU guard's latest sample, maxima and SW-thermal-slowdown share.
# Usage: EVAL_RUN=<prefix> evals/tools/status.sh
set -u
. "$(dirname "$0")/lib.sh"
D="$EVAL_STATE_DIR"; R="$REPO/evals/results"
[ -e "$STOP_FILE" ] && echo "*** STOPPED: $(tail -1 "$STOP_FILE") ($(head -1 "$STOP_FILE"))"
if [ -s "$D/steps.tsv" ]; then
printf '%-20s %-9s %-11s %s\n' step state items last
while IFS=$'\t' read -r n rd; do
  st=pending
  if [ -e "$D/$n.start" ]; then
    st=running
    if [ -e "$D/$n.end" ] && ! [ "$(cat "$D/$n.end")" \< "$(cat "$D/$n.start")" ]; then st="exit $(cat "$D/$n.status")"; fi
  fi
  items="-"
  if [ "$rd" != "-" ] && [ -f "$R/$rd/run.json" ]; then
    items=$(python3 - "$R/$rd" <<'PY'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
done = set()
for line in (d / "responses.jsonl").read_text(encoding="utf-8").splitlines() if (d / "responses.jsonl").exists() else []:
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if not r.get("error"):
        done.add(r["id"])
print(f"{len(done)}/{json.loads((d / 'run.json').read_text()).get('items_selected', '?')}")
PY
)
  fi
  printf '%-20s %-9s %-11s %s\n' "$n" "$st" "$items" "$(grep -v '^ *$' "$D/$n.log" 2>/dev/null | tail -1 | cut -c1-90)"
done < "$D/steps.tsv"
else
  echo "no steps registered in $D yet"
fi
if [ -s "$D/gpu.csv" ]; then
  echo
  echo "GPUs (latest guard sample; guard $(pgrep -f 'evals/tools/gpu-guard.sh' >/dev/null && echo running || echo NOT running)):"
  tail -2 "$D/gpu.csv" | awk -F, '{printf "  GPU%s %s C  %s/%s W  fan %s%%  util %s%%  sm %s MHz  reasons %s  (%s)\n",$2,$3,$4,$5,$7,$8,$10,$6,$1}'
  awk -F, 'NR>1{g=$2; if($3>m[g])m[g]=$3; if($4>p[g])p[g]=$4; if(!(g in f)){f[g]=$12; ft[g]=$1}; l[g]=$12; lt[g]=$1}
    END{for(g in m){cmd="date -d " ft[g] " +%s"; cmd|getline a; close(cmd); cmd="date -d " lt[g] " +%s"; cmd|getline b; close(cmd)
      el=b-a; th=(l[g]-f[g])/1e6
      printf "  GPU%s max so far: %s C, %s W; SW thermal slowdown %.0fs of %.0fs logged (%.0f%%)\n",g,m[g],p[g],th,el,(el>0?100*th/el:0)}}' "$D/gpu.csv"
fi
