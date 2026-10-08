# Eval plans

A plan is a shell script that runs a fixed set of `evals/run.py` invocations in order, so that a long eval is reproducible from the repo: the suites, sample sizes, seeds, models and phase order are all recorded. The helpers in [`../tools/`](../tools/) handle the moving parts:
- `lib.sh`: step markers, logs, the STOP file, notifications;
- `gpu-guard.sh`: the GPU safety stop;
- `stop.sh` and `status.sh`.

## run-b-day1.sh

It compares the coding pair and `big` on the security suites, with thinking on and off.
- Run (a) used 50-item samples. That was too small to separate the models; gaps of 8–16 points came out at p = 0.08–0.34.
- This plan uses 200-item samples, which resolve a gap of about 10 points on paired items.

```bash
evals/plans/run-b-day1.sh prepare                    # once: fetch the data, build the derived sets (network)
nohup evals/plans/run-b-day1.sh >/dev/null 2>&1 &    # run; start it again to resume after a stop
EVAL_RUN=evalb evals/tools/status.sh                 # progress, latest GPU sample, maxima
EVAL_RUN=evalb evals/tools/stop.sh "reason"          # clean stop; resume: rm the STOP file, start again
```

The state lives in `evals/results/_runs/evalb/` (gitignored):
- markers, step logs and `launch.log`;
- `gpu.csv` and `STOP`;
- the final `report.md`, `report.csv` and `pairs.csv`.

Set `EVAL_STATE_DIR` to keep it elsewhere.

| Phase | Runs | Items | Time (2× RTX 3090) |
|---|---|---|---|
| 1 | `big`, thinking off, on 100-item subsets of the pair's samples, so the paired tests compare the same items | 992 + 100 qa | ~1.5 h |
| 2 | Chain 1, `coder` on its own GPU: thinking off, then thinking on (`CODER_SEEDS`, default 1234) | 2092 + 100 qa + 400 per seed | ~8 h |
| 2 | Chain 2, `coder-fast` on the other GPU, at the same time as chain 1: thinking off, then thinking on (`FAST_SEEDS`, default 1234 1235 1236) | 2092 + 100 qa + 400 per seed | ~6.5 h |
| 3 | `big` grades every model's `sevenllm-qa` answers in one pass, so it loads once | 300 | ~0.3 h |
| 4 | `report.py` across every run dir, with `--split-label-source` and paired comparisons | | |

The times are estimates from run (a), ±30%. `big` evicts `coder` and `coder-fast` during phases 1 and 3.

**Settings.** Thinking-off runs use temperature 0 and `--timeout 300`. Thinking-on runs use the 0.6 / top-p 0.95 defaults.

**Suites.**
- CTIBench: mcq, rcm and vsp (200 each), plus ate (all 60).
- `nvd-cwe` and `nvd-cvss` (200 each).
- `nvd-cwe-nvdlab`: every NVD-labelled CWE item, 82.
- `nvd-cvss-nvdlab` (200).
- `cse-frr` (200). It is scored with the CyberSecEval keyword check, and the report adds the opener-refusal rate.
- `cybermetric-500` (all).
- SEvenLLM: mcq (50), and qa (100), graded by `big`.
- SecQA is left out because it is at ceiling.

**Derived sets** are built by `prepare` with [`make_subsets.py`](../datasets/make_subsets.py), seed 20261007. The same fetched data gives byte-identical files.
- `*-nvdlab`: the items labelled by NVD itself rather than the CNA, as their own suite with renamed ids.
- `*in100`: stratified subsets of the 200-item samples, for `big`.

**Safety.** The plan starts the GPU guard; `NO_GUARD=1` turns it off. The guard's defaults, which environment variables can change:

| Condition | Action |
|---|---|
| Core ≥ 84 °C | Notify. |
| Core ≥ 88 °C for 2 samples (60 s) | **Stop.** The RTX 3090's driver target is 83 °C (it throttles there, which is normal); max operating is 93 °C and slowdown 95 °C. |
| HW slowdown, HW thermal slowdown or HW power brake active | **Stop at once.** |
| Power more than 5% over the card's limit for 2 samples | **Stop.** |
| Fan reads 0% with the core ≥ 75 °C for 2 samples | **Stop.** |
| No GPU reading for 300 s | **Stop.** This is the fail-safe. |
| SW thermal slowdown for more than 50% of an interval | Notify. With a cool core, this points at VRAM or hotspot heat. |

GeForce cards don't report memory-junction or hotspot temperatures through `nvidia-smi`, so the guard can't watch them directly.

## run-b-day2.sh

This runs hermes and vision on the same 100-item subsets `big` used on day 1, so all five models can be compared on shared items.
- `big` then grades their `sevenllm-qa` answers.
- The report covers days 1 and 2. It reads day 1's dirs, named with `DAY1_RUN`, which defaults to `evalb`.
- Each model takes both GPUs. Coder and coder-fast are evicted for the whole run, which takes about 7 h.
- Thinking is off, and `--timeout` is 600 s, because these models decode at about 35–55 tok/s.

```bash
nohup evals/plans/run-b-day2.sh >/dev/null 2>&1 &
EVAL_RUN=evalb2 evals/tools/status.sh
```

## Gateway route: SSH tunnel (default)

Both plans start [`tools/gateway-tunnel.sh`](../tools/gateway-tunnel.sh) and point `run.py` at it through `EVAL_BASE_URL`, so eval traffic does not go through the public edge.

- **Why:** on day 1 of run (b), one request sat for 5 minutes on a dead TCP connection through the edge until `--timeout` fired.
- **How it reaches LiteLLM:** LiteLLM listens only on the backend's WireGuard address (`BACKEND_WG_IP:LITELLM_PORT`). The tunnel SSHes to the backend (`BACKEND_SSH_USER@BACKEND_LAN_IP`) and forwards a local port, by default `127.0.0.1:14000`, to that address.
  - Nothing changes on either firewall.
  - Requests still need the LiteLLM key, which travels inside SSH.
  - A small supervisor restarts ssh if the connection drops.
- **Opting out:** `EVAL_GATEWAY=edge` uses `https://$SPARK_API_HOST` as before.
- **Provenance:** `run.json` records the route actually used in its `gateway` field.

## Writing a plan
- Source `../tools/lib.sh` with `EVAL_RUN` set.
- Run every step with `eval_step NAME python3 -B evals/run.py … --run-name "$EVAL_RUN[-suffix]"`. The stop and the guard match exactly that command line.
- Register each step with `eval_register NAME RESULTS_DIR`, so that `status.sh` lists it.
- Call `eval_preflight` before the first step.
- Chain steps with `&&`. `eval_step` fails when its command fails or when STOP exists, so a stop or a failure ends the chain.
- Resume is free: `run.py` skips finished items, and a finished step makes no model calls.
