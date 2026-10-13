# Add a model

The path is: GGUF on disk → llama-swap entry → matrix set → LiteLLM alias → Open WebUI grant → harness catalogs. Each step is verifiable on its own.

## 0. Will it fit?

- **One GPU (24 GB):** weights + KV cache + compute buffers must fit with about 1–2 GB headroom. With `-ctk q8_0 -ctv q8_0`, KV is about half of f16. Every entry runs `--fit off`, so a model that does not fit fails at load instead of quietly shrinking its context. Both GPUs are on PCIe Gen4 x8 links, so either loads at 10–13 GB/s; cold loads are limited by the models disk, not the link.
- **Both GPUs (48 GB):** start with `-sm layer -ts 1,1`. For a **dense** model, also benchmark `-sm tensor` (experimental in llama.cpp) with `--shm-size 2g` on the docker side, as `hermes` and `vision` do: it gave them +39–67 % decode at the cost of 12–22 % prompt processing on long prompts. It does nothing for a 3B-active MoE such as `big`. Without the larger `/dev/shm` the container crashes at the first allreduce. `-sm row` does not load at all on the pinned build ("does not support split buffers").
- Ampere has no FP8. Use GGUF quants (Q4_K_M, UD-Q4_K_XL, IQ4_NL and so on).
- The model's chat template must work with `--jinja` and tool calling, or harnesses will fail. Check the model card for llama.cpp support.

## 1. Put the GGUF on `/models`

On Walter:

```bash
sudo -u ${BACKEND_SSH_USER} mkdir -p ${MODELS_DIR}/gguf/<model-id>
cd ${MODELS_DIR}/gguf/<model-id>
# e.g. with huggingface-cli or curl; verify the sha256 from the model card
sha256sum *.gguf
```

Use a lowercase, version-specific `<model-id>` (e.g. `qwen3.8-27b`). It becomes the llama-swap model ID, the container name and the `local/<model-id>` name in LiteLLM.

## 2. Add the llama-swap entry

Edit `walter/llama-swap/config.yaml.tmpl` (see [walter/](../../walter/README.md); deployed to `/etc/llama-swap/config.yaml`). Copy the closest existing entry and change:

- `name`, `aliases` (only if it takes over an existing alias), the `-m` path and `--mmproj` if any;
- `--gpus "\"device=N\""` and `-sm none` for one GPU, or `device=0,1` and `-sm layer -ts 1,1` for a split (or `${tp}` before `${image}` plus `-sm tensor`, see above);
- `-c` (context), sampling defaults from the model card;
- `capabilities` (`in`, `tools`, `context`);
- `ttl: 1800` for a split model, so it frees both GPUs when idle.

Keep the `${run}`, `${common}` and `${stop}` macros; they carry the pinned image, loopback port binding, read-only `/models` mount and the safe stop.

## 3. Place it in the matrix

Add a variable under `routing.router.settings.matrix.vars` and put it in a set:

- a single-GPU model that should coexist with the coding pair needs a set that does not over-commit that GPU (e.g. `"c & n"` if it replaces `coder-fast` on GPU1);
- a split model gets its own set (e.g. `newbig: "n"`), which evicts the pair;
- add an `evict_costs` entry if it is slow to load (large weights: about 2.8 GB/s from a cold models disk).

## 4. Reload and test llama-swap

Install the new config with `walter/deploy.sh` (it converges the whole host: read its dry run) or with the narrow llama-swap install in [walter/README.md](../../walter/README.md#narrow-redeploys-one-component). `--watch-config` reloads it.

```bash
sudo systemctl restart llama-swap          # safe: the coding pair comes back via preload
journalctl -u llama-swap -f
K=$(sudo cat /etc/llama-swap/api-key)
curl -s http://${BACKEND_WG_IP}:8080/v1/chat/completions -H "Authorization: Bearer $K" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<model-id>","max_tokens":2048,"messages":[{"role":"user","content":"Say pong"}]}' | jq .
nvidia-smi                                  # VRAM per GPU as expected
```

Test tool calling and, if it is a thinking model, a generous `max_tokens`.

## 5. Add the LiteLLM alias

In the gateway config (`litellm.yaml`, shipped in [walter/](../../walter/README.md)), add a `model_list` entry for the new alias pointing at `openai/<model-id>` on llama-swap. **Copy `model_info.supported_endpoints` from an existing alias**: without `["/v1/chat/completions","/v1/responses","/v1/messages"]`, Anthropic requests go through the Responses adapter and lose reasoning. The `local/*` wildcard already exposes the raw ID.

**Add the alias to the key allow-lists.** Keys carry explicit `models` lists, so a new alias is unreachable until it is in `SERVED` (or the relevant class in `KEY_CLASSES`) in `walter/gateway/provision-keys.py.tmpl`; `walter/gateway/tests` fails while the two disagree. Deploy, then `sudo /srv/gateway/provision-keys.py --dry-run` shows the alias being added to each key, and `sudo /srv/gateway/provision-keys.py` applies it. Also add the alias's output maximum to `MODEL_MAX_OUTPUT` in `hooks/spark_hooks.py`.

**Lab models** (research models, not served to everyone) are named `lab/<name>` in LiteLLM and `lab-<name>` in llama-swap, with no llama-swap `aliases`. Do not add them to `SERVED`: the gateway hook refuses them, also as `local/lab-<name>`, for every key without `metadata.lab=true` (`LAB_KEY_ALIASES`, see [walter/README.md](../../walter/README.md#after-the-first-deploy)).

```bash
cd /srv/gateway && sudo docker compose up -d litellm   # or restart if only the config changed
curl -s http://${BACKEND_WG_IP}:4000/v1/models -H "Authorization: Bearer <a key>" | jq -r '.data[].id'
```

Then test each API the alias will serve: chat, `/v1/messages` (Claude Code) and `/v1/responses` (Codex).

## 5a. Regression gate (when the model replaces an alias)

If the new model, quant or flags take over an alias that has a gate manifest (`coder`, `coder-fast`), run the [upgrade gate](../../evals/README.md#upgrade-gate). Before the switch, make sure the alias has a passing A/A (`python3 evals/gate.py calibrate --model <alias> --yes`, once per model) and a current baseline (`gate.py baseline`). After the switch, run `python3 evals/gate.py check --model <alias> --yes` (about 2.0 h for `coder`, 1.3 h for `coder-fast`). On FAIL, roll back to the old entry. On INCONCLUSIVE (hold, which is common), the owner decides from the per-suite table. A brand-new alias has nothing to regress against; it needs its own manifest (`evals/tools/gate_select.py`) before the gate covers it.

## 6. Open WebUI visibility

A new model is visible to admins only until it has an access grant. In Admin Panel → Settings → Models, open the alias and give it public read access (or a group grant).

## 7. Harness catalogs

- **Codex:** add the alias with its real context window to the model catalog in [clients/](../../clients/README.md), or Codex assumes 272K.
- **Claude Code, OpenCode, Hermes:** only needed if the new alias should be selectable by name or replaces a tier (e.g. the Haiku tier → `coder-fast`).

## 8. Record it

Update the model table in the [README](../../README.md#models) and the matrix in [ARCHITECTURE](../../ARCHITECTURE.md#llama-swap-matrix-and-gpu-placement). If you benchmarked it, add the numbers to [`walter/llama-swap/BENCHMARKS.md`](../../walter/llama-swap/BENCHMARKS.md), and the file to [`walter/models.md`](../../walter/models.md) and `fetch-models.sh`.

## Retiring a model

Reverse order: remove harness references, the Open WebUI grant, the LiteLLM alias, the matrix variable and the llama-swap entry, then delete the GGUF directory.
