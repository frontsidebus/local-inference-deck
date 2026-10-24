# Upgrade

Nothing in this stack updates itself except Ubuntu security packages (unattended-upgrades). Every container image is pinned by digest and every binary by version and sha256. Upgrades are deliberate, one component at a time, driven by the weekly report.

## 1. Read the report

On Walter:

```bash
less /var/lib/spark-update-check/latest.md         # also summarised in the login banner
sudo systemctl start spark-update-check.service    # refresh now
```

| Exit / section | Meaning | Action |
|---|---|---|
| 20, "advisories affect current version" | A published security advisory covers what is running | Upgrade that component this week. Read the advisory first: some only affect features we do not expose (the edge allowlist blocks most of LiteLLM's admin surface). |
| 10, "digest drift" | The pinned tag now points to a rebuilt image (e.g. `postgres:17`, `server-cuda`) | Routine. Batch with the next maintenance. |
| 10, "new release" | A newer version exists | Read the release notes; upgrade when useful. |
| "check manually" | The advisory's version range could not be compared | Read it and decide. |
| apt / reboot-required | OS updates pending | See section 5. |

For Covenant, run `spark-edge-check` from the workstation (apt security count, certificate expiry, ufw, fail2ban, `nginx -t`).

## 2. General procedure (any compose service)

1. Back up the file: `sudo cp compose.yaml compose.yaml.bak-$(date +%Y%m%d-%H%M%S)`.
2. Resolve the new digest and record it **with the tag in the comment**, because the update check reads that comment:
   ```bash
   docker pull ghcr.io/<org>/<image>:<tag>
   docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/<org>/<image>:<tag>
   # image: ghcr.io/<org>/<image>@sha256:<digest>   # <image>:<tag>
   ```
   Verify the signature where the project publishes one (LiteLLM publishes cosign signatures).
3. For apps with a database (LiteLLM, Open WebUI, Pocket-ID), take a backup first: `sudo systemctl start spark-backup`. Schema migrations run on start and downgrades may not work.
4. `sudo docker compose pull <svc> && sudo docker compose up -d <svc>`, then read the logs.
5. Verify through the real path (below), not just the container health.
6. Keep the old image until the new one has run for a while. **Rollback** = put the old digest back, `docker compose up -d <svc>`, and if a migration ran, restore the DB from the backup taken in step 3.

The component-specific commands are in [walter/update-check/README.md.tmpl](../../walter/update-check/README.md.tmpl) (installed on Walter as `/var/lib/spark-update-check/README.md`) and [covenant/README.md](../../covenant/README.md).

Record every pin change in the repo too. `walter/deploy.sh` installs the repo's version over a hand edit, and it converges the whole host on every run (package installs, an `nvidia-persistenced` restart, telemetry and digest rebuilds): read [What a run does to a live host](../../walter/README.md#what-a-run-does-to-a-live-host) before using it for a one-component upgrade, or use a [narrow redeploy](../../walter/README.md#narrow-redeploys-one-component).

## 3. Component notes

| Component | Where pinned | Verify with | Watch out for |
|---|---|---|---|
| LiteLLM | `/srv/gateway/compose.yaml` | `curl http://${BACKEND_WG_IP}:4000/health/liveliness`, then one request each on chat, `/v1/messages` with thinking, `/v1/responses` | Re-check that `/v1/messages` still passes through (thinking blocks present) and that `spark_hooks.py` still loads (the startup log names it) and still rewrites mid-conversation system messages. |
| Postgres | same | healthcheck healthy | A new digest of `17` is fine. **Never** change the major version by editing the tag; that needs dump and restore. |
| Open WebUI | `/srv/webui/compose.yaml` | Passkey login, model list, one chat | Theme CSS selectors can break; check dark mode. Model access grants and OIDC settings. |
| Pocket-ID | same | Login at `https://${SPARK_ID_HOST}`, then Open WebUI and telemetry SSO | Back up before majors. |
| llama.cpp `server-cuda` | llama-swap config, macro `image` | Each alias loads and answers; `coder` still shows MTP acceptance in the log; `vision` reads a test image | CLI flags change (`--fit`, `-fa`, speculative options have changed before). `systemctl restart llama-swap` stops running models. `hermes` and `vision` run the **experimental** `-sm tensor` (with `--shm-size 2g`): re-run the tensor-split check in [BENCHMARKS](../../walter/llama-swap/BENCHMARKS.md) and fall back to layer split if it regresses. Row split did not load on build 11277; retest only after a bump. |
| llama-swap | `/usr/local/bin/llama-swap` (version + sha256) | `llama-swap --version`, matrix behaviour | Keep the old binary as `llama-swap.v<old>` for rollback. Config schema changes (matrix, hooks). |
| Monitoring stack | `/srv/monitoring/compose.yaml` | All Prometheus targets up, dashboards load | dcgm-exporter must match the driver. |
| oauth2-proxy (Covenant) | `covenant/` (version + sha256) | Telemetry login, `/api/*` still 401 JSON when logged out | Read the changelog for renamed flags. |
| nginx, fail2ban, certbot (Covenant) | Ubuntu packages | `nginx -t`, `fail2ban-client status`, `certbot renew --dry-run` | |

## 4. Hermes Agent: pin to release tags

Hermes runs on Walter and on the workstation (where the `hermes-gateway` user service powers the `hermes-agent` model).

- **Never run `hermes update`.** It moves the checkout to `main`, not the next stable release.
- Upgrade to a release tag:
  ```bash
  cd ~/.hermes/hermes-agent
  git status                          # stash local changes first
  git fetch --tags origin
  git tag before-update-$(date +%F)   # rollback point
  git checkout <release-tag>
  venv/bin/pip install -e .
  hermes --version && hermes-spark    # smoke test
  systemctl --user restart hermes-gateway   # workstation only
  ```
- Rollback: `git checkout before-update-<date>` and reinstall.
- Keep both hosts on the same tag. After upgrading, check that `security.redact_secrets` is still `true` and that the `custom:spark` provider still resolves.

## 5. OS and NVIDIA driver

- Security packages install automatically. For the rest: `sudo apt update && sudo apt upgrade`, then check `/var/run/reboot-required`.
- Reboot Walter and Covenant one at a time and run the end-to-end checks from [power-loss-recovery](power-loss-recovery.md#3-end-to-end-from-outside).
- **NVIDIA driver** upgrades need a maintenance window: DKMS rebuild, reboot, and every GPU container stops. Afterwards: `nvidia-smi`, `docker run --rm --gpus all --runtime nvidia <pinned server-cuda image> --version`, then load one model through llama-swap. Keep the driver, `nvidia-container-toolkit` and dcgm-exporter compatible. Roll back with `apt install nvidia-driver-<series>=<old>`; hold a known-good version with `apt-mark hold`.
- The hypervisor's NVIDIA packages do not matter while both GPUs belong to `vfio-pci`.

## 5a. Hardware (GPUs, risers, slots)

A hardware change on the hypervisor moves the GPUs' host PCI addresses, and Walter's `<hostdev>` entries name addresses. **Disable Walter's autostart before powering off**, fix the addresses, start Walter by hand, then re-enable autostart. A stale address can make libvirt seize a different host device (after the riser change, GPU1's old address belonged to the PCIe switch in front of the host's root disk). Procedure: [hypervisor/README.md](../../hypervisor/README.md#hardware-changes-gpus-risers-slots).

Afterwards: `nvidia-smi --query-gpu=index,pcie.link.gen.max,pcie.link.width.current --format=csv` in Walter (live: `4, 8` for both GPUs), one load of each split model, and the checks from [power-loss-recovery](power-loss-recovery.md#2-walter).

## 5b. Model, quant or llama.cpp changes: the upgrade gate

For a new llama.cpp build (`server-cuda` digest), a new GGUF or quant behind an alias, or changed serving flags, run the upgrade gate (sec-evals README, "Upgrade gate"; pointer: [evals/README.md](../../evals/README.md#upgrade-gate)) for each affected alias that has a manifest (`coder`, `coder-fast`). The owner approves the GPU time, and `--yes` is required. One run takes about 2.0 h for `coder` and 1.3 h for `coder-fast`, so run it with no other traffic.

Run the gate with **`bin/deck evals gate <calibrate|baseline|check> --model <alias> --yes`** from this repo. The harness is the sec-evals repo, checked out at `~/src/deck/sec-evals` (or `$SEC_EVALS_DIR`); once per checkout, run `bin/deck install evals` (it links the checkout's gitignored `site.env`) and add the checkout to `SPARK_EVAL_REPOS` before any model swap. deck runs `python3 -B evals/gate.py` from the sec-evals root with this checkout's llama-swap template, and makes sure the gate, the GPU guard and the gateway tunnel all read the same site.env (`--site-env` > `$SITE_ENV` > `~/.config/deck/site.env` > repo-root `site.env`; exit 2 if none). The gate refuses (exit 4) if the GPU guard dies at start; read `guard.log` in the gate dir. `bin/deck check` verifies the `site.env` link.

1. **Once per model**, and again after any caching or batching change (`-np`, prompt cache): `bin/deck evals gate calibrate --model <alias> --yes` (two runs). It also stores a baseline. If the A/A does not pass, fix determinism first; `check` refuses until it passes.
2. **Before the change**, if the alias has no current baseline: `bin/deck evals gate baseline --model <alias> --yes`.
3. Make the change.
4. `bin/deck evals gate check --model <alias> --yes`. The report is printed and written to the check's dir.
   - **FAIL** (exit 1): roll back (put the old digest, file or flags back) and keep the report.
   - **INCONCLUSIVE** (exit 2, hold): the owner decides from the per-suite table. This is common: a clean upgrade passes every suite only about half the time.
   - **PASS** (exit 0): keep the change, and take a new baseline on it.
   - Exit 3 (invalid run) or 4 (refused): fix the cause and rerun; it says nothing about the change.

## 6. After every upgrade

- Run `sudo systemctl start spark-backup` so a snapshot with the new versions exists, and check that the manifest records the new digest.
- Re-run the update check to confirm the finding is gone.
- Note anything surprising in [ARCHITECTURE.md](../../ARCHITECTURE.md#known-quirks-and-gotchas).
