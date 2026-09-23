# Reboot Validation Checklist

**Window:** next planned poweroff
**Scope:** prove unattended recovery, and apply two changes that only a
reboot can honestly validate.

Cross-references `ARCHITECTURE.md` §2.3 (Ollama config), §2.3.1 (ufw), §5
(boot behaviour), §7 (open items).

---

## What this window covers

| # | Change | State going in |
|---|---|---|
| 1 | VM autostart | **already enabled** 2026-09-22 — never yet proven by a real boot |
| 2 | `OLLAMA_HOST` → tunnel address | staged in `unit-files/ollama-override.conf`, not applied |
| 3 | Install `ollama-preload.service` | staged in `unit-files/`, not installed |
| 4 | Remove stale `8080/tcp` ufw rule | present on the VM, nothing listens |

**Why a reboot and not a restart.** Change 2 makes Ollama bind
`10.100.0.2`, which does not exist until `wg-quick@wg0` has run. A
`systemctl restart ollama` succeeds while `wg0` happens to already be up
and therefore proves nothing. Only a cold boot exercises the ordering. The
same applies to changes 1 and 3, which exist solely to make boot work
unattended.

---

## Read this before you change anything: the way back in

Changes 2 and 4 both touch reachability. If they go wrong, the WireGuard
path to the VM is exactly what breaks, so do not rely on it for recovery.

Two out-of-band paths, both from the **host** (`bishop-X870-GAMING-WIFI6`):

```bash
# 1. SSH over the libvirt NAT -- independent of WireGuard entirely
ssh operator@10.127.10.160

# 2. Serial console -- independent of guest networking entirely
virsh -c qemu:///system console agent-sandbox-3090     # ctrl-] to exit
```

Path 1 works because the VM allows `22/tcp` from `Anywhere` (§2.3.1). This
is the reason not to scope that rule to `wg0`: doing so would remove the
only network path that survives a tunnel or bind failure.

---

## Phase 0 — capture "before", on the VM

Do this first. Without it you cannot tell a regression from pre-existing
behaviour.

```bash
ssh operator@10.127.10.160

sudo ss -tlnp | grep 11434          # expect *:11434  (pre-hardening)
sudo ufw status numbered | tee ~/ufw-before.txt
systemctl is-enabled ollama wg-quick@wg0
ollama ps                           # expect model resident, UNTIL Forever
nvidia-smi --query-gpu=index,memory.used,pcie.link.width.current \
  --format=csv,noheader
```

On the **host**:

```bash
virsh -c qemu:///system dominfo agent-sandbox-3090 | grep -i autostart
# expect: Autostart:  enable
```

---

## Phase 1 — apply, while still up

### 1a. Install the preload unit (VM)

```bash
# from the repo checkout on the host
scp unit-files/ollama-preload.service operator@10.127.10.160:/tmp/

ssh operator@10.127.10.160
sudo install -m 0644 /tmp/ollama-preload.service \
    /etc/systemd/system/ollama-preload.service
sudo systemctl daemon-reload
sudo systemctl enable ollama-preload.service     # enable only -- do NOT --now
```

`enable` without `--now` is deliberate: its whole purpose is to run at
boot, and starting it now would only warm an already-warm model.

### 1b. Apply the hardened drop-in (VM)

```bash
scp unit-files/ollama-override.conf \
    operator@10.127.10.160:/tmp/

ssh operator@10.127.10.160
sudo cp /etc/systemd/system/ollama.service.d/override.conf \
        /etc/systemd/system/ollama.service.d/override.conf.bak   # rollback
sudo install -m 0644 /tmp/ollama-override.conf \
    /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload

# confirm systemd actually absorbed it BEFORE rebooting
systemctl show ollama -p Environment -p After -p Requires | tr ' ' '\n' \
  | grep -E "OLLAMA_HOST|wg-quick"
# expect: OLLAMA_HOST=10.100.0.2:11434  and wg-quick@wg0.service in BOTH
```

If `OLLAMA_HOST` still reads `0.0.0.0`, the drop-in did not take — stop and
fix that before rebooting.

> Do **not** `systemctl restart ollama` here. It will appear to work
> (`wg0` is up) and tells you nothing about the boot ordering. Let the
> reboot be the test.

### 1c. Remove the stale 8080 rule (VM)

Nothing listens on 8080: Docker holds 0 containers, 0 images, 0 volumes, and
there is no llama.cpp binary or unit. The rule is a latent grant — see
§2.3.1. Note `~/.hermes/skills/creative/p5js/scripts/serve.sh` defaults to
port 8080 and `python3 -m http.server` binds all interfaces, so running that
helper today would expose its working directory over the tunnel.

Delete by **number**, highest first, because numbers shift as you delete,
and there is an IPv6 twin:

```bash
sudo ufw status numbered | grep 8080     # note BOTH entries (v4 + v6)
sudo ufw delete <higher-number>
sudo ufw delete <lower-number>
sudo ufw status numbered | grep 8080 || echo "both removed"
```

Confirm 11434 survived — this is the rule inference depends on:

```bash
sudo ufw status | grep 11434     # expect: 11434/tcp on wg0  ALLOW IN
```

---

## Phase 2 — the reboot

```bash
# host
virsh -c qemu:///system shutdown agent-sandbox-3090   # graceful
# ...then the host poweroff / power on as planned
```

Do not hand-start the VM afterwards. The point is to observe whether it
comes up on its own.

---

## Phase 3 — verify, in this order

Each step gates the next, so a failure localises the cause.

**On the host:**

```bash
virsh -c qemu:///system list --all      # VM should be `running`, unstarted by you
lspci -nnk -s 01:00.0 | grep -i "driver in use"   # expect vfio-pci
lspci -nnk -s 05:00.0 | grep -i "driver in use"   # expect vfio-pci
```

**On the VM:**

```bash
ssh operator@10.127.10.160

# 1. tunnel up, address present
ip -brief addr show wg0                 # expect 10.100.0.2/24
sudo wg show wg0 | grep -i handshake

# 2. THE ORDERING TEST -- the whole reason for this window
sudo ss -tlnp | grep 11434
# PASS: 10.100.0.2:11434
# FAIL: *:11434            -> drop-in not applied
# FAIL: nothing listening  -> bind lost the race; see Phase 4
systemctl is-active ollama              # expect active, NOT activating/failed
systemctl show ollama -p NRestarts      # expect 0 -- anything higher means crash-loop

# 3. preload ran
systemctl status ollama-preload --no-pager | head -5   # active (exited)
journalctl -u ollama-preload -b --no-pager | tail -10

# 4. model already warm -- the payoff
ollama ps                               # resident, 100% GPU, UNTIL Forever

# 5. firewall as intended
sudo ufw status | grep -E "11434|8080"  # 11434 present, 8080 gone

# 6. both GPUs back
nvidia-smi --query-gpu=index,memory.used,pcie.link.width.current \
  --format=csv,noheader                 # widths 16 and 1
```

**End to end, from anywhere:**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://343-guilty-spark.io/
# expect 401 -- basic auth working
```

Then time a real first query through the UI. **Under ~2s means the preload
worked**; 15-20s means it did not fire and the cold start is still being
paid by the first user.

### Success criteria

- [ ] VM running without manual `virsh start`
- [ ] `ss` shows `10.100.0.2:11434`, not `*:11434`
- [ ] `ollama` active with `NRestarts=0`
- [ ] `ollama ps` shows the model resident before any query
- [ ] first query fast, not 15-20s
- [ ] `8080` gone, `11434 on wg0` intact
- [ ] edge returns 401

---

## Phase 4 — rollback

Independent, so revert only what misbehaved.

**Ollama not listening / crash-looping** (most likely failure — the bind
lost the race, or `wg0` did not come up):

```bash
ssh operator@10.127.10.160            # NAT path, unaffected
sudo cp /etc/systemd/system/ollama.service.d/override.conf.bak \
        /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
sudo ss -tlnp | grep 11434            # back to *:11434
```

That restores the pre-window state: working, but ufw as the only control.

**Preload misbehaving:**

```bash
sudo systemctl disable --now ollama-preload.service
```

Harmless by construction — both `Exec` lines are `-`-prefixed, so it cannot
fail the boot. Worst case it delays `multi-user.target` by up to 60s while
its readiness probe retries, then gives up.

**ufw rule wanted back:**

```bash
sudo ufw allow in on wg0 to any port 8080 proto tcp
```

**Autostart back off:**

```bash
virsh -c qemu:///system autostart --disable agent-sandbox-3090
```

---

## Known traps

**There are two Ollamas.** The host runs its own (`enabled`, loopback
`:11434`, stale `qwen3-coder:30b`, CPU-only — §2.5). On the host,
`systemctl status ollama` and `curl localhost:11434` both answer from the
*wrong* one. Every Ollama command above is meant to run **inside the VM**.

**The host has no WireGuard** (§2.2). `curl 10.100.0.2:11434` from the host
times out by design — that is not a symptom. Use the VM or the EC2 side.

**`virsh domautostart` does not exist** on libvirt 10.0.0. Read autostart
with `virsh dominfo`; set it with `virsh autostart`.

---

## After a clean run

- Record the outcome in `ARCHITECTURE.md` §7 and close the resolved items.
- Re-run `scripts/inference-baseline.sh` if you want a post-change baseline;
  sample PCIe *width*, not *gen* — gen reads 1 on both cards at idle from
  ASPM downshift and only reads true under load.
