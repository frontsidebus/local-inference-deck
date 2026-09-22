# 343-guilty-spark.io — Architecture & Handoff

**Last updated:** 2026-09-22
**Repo:** `frontsidebus/installation-343` (private)
**Domain:** `343-guilty-spark.io`
**Status:** Live and serving

Self-hosted LLM inference platform. Web UI on EC2 (public-facing), inference
backend on a homelab workstation reached over WireGuard. Currently serves
Llama 3.3 70B via Ollama across dual RTX 3090s in a GPU-passthrough VM.

---

## 1. High-Level Architecture

```
   Browser
      │  HTTPS
      ▼
   ┌──────────────────────────────────────┐
   │  EC2 (ip-172-31-85-93)               │
   │  ─────────────────────────           │
   │  nginx :80/:443                      │
   │    ├─ / → /var/www/spark/index.html  │
   │    └─ /v1/llama/* → proxy_pass       │
   │                    10.100.0.2:11434  │
   │  WireGuard wg0 (server, :51820)      │
   └──────────────────────────────────────┘
                    │
                    │  WireGuard tunnel
                    │  (encrypted, keyed)
                    │
   ┌──────────────────────────────────────┐
   │  VM: agent-sandbox-3090              │
   │  ─────────────────────────           │
   │  WireGuard wg0 (peer, 10.100.0.2)    │
   │  Ollama :11434                       │
   │    └─ llama3.3-70b-fullgpu:latest    │
   │       (Q4_K_M, ~42 GB VRAM)          │
   │  Both RTX 3090s via vfio-pci         │
   └──────────────────────────────────────┘
                    │
                    │  KVM/QEMU
                    ▼
   ┌──────────────────────────────────────┐
   │  Host: bishop-X870-GAMING-WIFI6      │
   │  ─────────────────────────           │
   │  Ubuntu 24.04 LTS                    │
   │  Kernel 6.14.0-37-generic            │
   │  AMD 9950X3D, 128 GB DDR5 (4×32)     │
   │  Gigabyte X870 GAMING WIFI6          │
   │  be quiet! 1200W 80+ Gold ATX 3.1    │
   │  Dual RTX 3090 (ZOTAC, 19da:1613)    │
   │  Open-air workbench, horizontal      │
   │  vfio-pci owns both GPUs             │
   │  libvirt/QEMU hosts the VM           │
   └──────────────────────────────────────┘
```

---

## 2. Component Details

### 2.1 EC2 Frontend

- **Instance:** `ip-172-31-85-93` (public IP `13.217.253.94`)
- **OS:** Ubuntu (version unconfirmed but recent)
- **Web root:** `/var/www/spark/index.html`
- **nginx config:** `/etc/nginx/sites-available/343-guilty-spark`
  (symlinked from `sites-enabled/`)
- **WireGuard:** server-side, listening `51820/udp`, peer public key
  `SKGnl0A+fGx7t5iiltKRrqX8NGos2mzjhApyKc7Sugw=`

**The UI (`index.html`) is:**
- Single-page, no framework, ~9 KB
- Vanilla JS + fetch API with SSE streaming
- Theme: 343 Guilty Spark (Halo lore, terminal aesthetic, `#f0a020` accent)
- Model dropdown supports multiple endpoints (only `/v1/llama` currently
  active)
- OpenAI-compatible client — sends `POST /v1/llama/chat/completions` with
  `{model, messages, stream: true}`
- Streams tokens with a blinking cursor, supports `/clear` command and
  ESC-to-abort
- System prompt is a Guilty Spark persona (humor: 85%, honesty: 95%)

**Active endpoint config:**

```javascript
const MODELS = {
  "/v1/llama": "llama3.3-70b-fullgpu:latest",
};
```

Commented-out dropdown entries (`/v1/fast` for Hermes-4 14B, `/v1/big` for
Hermes-4 70B) are placeholders from an earlier architecture; not currently
wired up.

**Active nginx location block:**

```nginx
location /v1/llama/ {
    rewrite ^/v1/llama/(.*) /v1/$1 break;
    proxy_pass         http://10.100.0.2:11434;
    proxy_http_version 1.1;
    proxy_set_header   Host       $host;
    proxy_set_header   Connection "";
    proxy_buffering    off;
    proxy_cache        off;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}
```

Also present but currently unused: a `/v1/` catch-all block pointing at
`10.100.0.2:8000` with a bearer token for vLLM (backend not running).

### 2.2 WireGuard Mesh

- **Subnet:** `10.100.0.0/24`
- **EC2:** WG server, listens on `51820/udp`, public endpoint
  `13.217.253.94:51820`
- **VM:** WG peer at `10.100.0.2/24`, wg-quick@wg0.service enabled and
  active
- **Host (`bishop-X870-GAMING-WIFI6`):** WireGuard installed but service
  currently inactive. Not required for the inference path (the VM has its
  own tunnel).

### 2.3 VM: agent-sandbox-3090

- **libvirt name:** `agent-sandbox-3090`
- **User:** `operator`
- **OS:** Ubuntu (Linux guest, matching kernel driver 580.173.02)
- **Resources:** 32 GB RAM, 16 vCPUs, host-passthrough CPU
- **Networks:**
  - libvirt default network at `10.127.10.160/24` (customized from the
    typical `192.168.122.0/24`)
  - WireGuard `wg0` at `10.100.0.2`
- **GPUs:** Both RTX 3090s attached via four `<hostdev>` blocks (2 GPU
  functions + 2 audio functions, PCI IDs 10de:2204 and 10de:1aef)
- **Guest PCI addresses:** GPU 0 at `05:00.0` (host card at `01:00.0`,
  the ×16 slot), GPU 1 at `09:00.0` (host card at `05:00.0`, the ×1 slot)
- **NVIDIA driver in guest:** 580.173.02, CUDA 13.0

**Ollama configuration** (`/etc/systemd/system/ollama.service.d/override.conf`):

```ini
[Service]
Environment="OLLAMA_HOST=10.100.0.2:11434"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_NUM_PARALLEL=2"
```

- Binds only to WireGuard interface (surgical, not `0.0.0.0`)
- Keeps model loaded forever (no eviction)
- Allows 2 concurrent requests to share the loaded model

**Loaded models:**
- `llama3.3-70b-fullgpu:latest` — custom Modelfile derived from
  `llama3.3:70b-instruct-q4_K_M` with `num_gpu 999` and `num_ctx 4096`.
  This is the model the UI targets.
- `llama3.3:70b-instruct-q4_K_M` — base model, kept as a reference. Ollama
  dedupes blob storage so actual disk cost is ~42 GB not 84.

### 2.4 Host: bishop-X870-GAMING-WIFI6

- **CPU:** AMD Ryzen 9950X3D
- **RAM:** 128 GB DDR5 (4×32 GB)
- **Motherboard:** Gigabyte X870 GAMING WIFI6
- **PSU:** be quiet! 1200W 80+ Gold ATX 3.1 (2000W transient headroom)
- **GPUs:** Dual ZOTAC RTX 3090 (subsystem `19da:1613`)
- **Cooling:** Open-air workbench, cards mounted horizontally with spacing
- **Storage:** `/dev/nvme1n1p2` ext4 root, `/dev/nvme1n1p3` /home,
  `/dev/nvme1n1p4` XFS /data
- **OS:** Ubuntu 24.04 LTS
- **Kernel:** 6.14.0-37-generic (default), 6.14.0-35-generic as fallback
- **HWE track:** version-locked at `linux-generic-6.14`
- **Secure Boot:** enabled with MOK enrolled at
  `/var/lib/shim-signed/mok/MOK.der`
- **Host NVIDIA stack:** 580.178.04 installed and DKMS-built for both
  kernels, signed with MOK. Present for future toggle-back capability;
  `nvidia-smi` on the host fails by design because vfio-pci owns the GPUs.

**vfio-pci configuration** (`/etc/modprobe.d/vfio.conf`):

```
options vfio-pci ids=10de:2204,10de:1aef
softdep amdgpu pre: vfio-pci
softdep nouveau pre: vfio-pci
softdep nvidia pre: vfio-pci
softdep nvidiafb pre: vfio-pci
softdep snd_hda_intel pre: vfio-pci
softdep i915 pre: vfio-pci
```

ID-based binding means both 3090s (same vendor:device ID) auto-bind on
boot without needing per-slot configuration.

---

## 3. Known Hardware Limitations

### 3.1 PCIe Bandwidth Asymmetry

- **Card 1** (host bus `01:00.0`, slot 1): PCIe 4.0 ×16 — full bandwidth
- **Card 2** (host bus `05:00.0`, slot 2): PCIe 4.0 ×1 — bandwidth-limited

Slot 2 is chipset-attached with only 1 lane wired on this consumer board.
16× reduction in inter-GPU communication bandwidth.

**Impact:**
- Model loading to Card 2 is ~10-20× slower (but happens once at
  container/service start; not user-visible thereafter)
- Tensor-parallel inference (vLLM-style) would bottleneck on ×1 link
- **Pipeline-parallel inference (Ollama/llama.cpp default) is largely
  unaffected** — inter-GPU traffic per token is small enough that ×1
  handles it fine
- Measured throughput: 19 tok/s on 70B Q4_K_M, consistent across runs
- No thermal throttling issues in practice (validated by 30-min burn-in)

**Planned upgrade:** ASUS ProArt X870E-Creator WiFi board (~$550). Supports
×8/×8 bifurcation with both primary slots populated. PCIe 5.0 ×8 to each
card is bandwidth-equivalent to PCIe 4.0 ×16, saturating the 3090s'
capability. Would eliminate the asymmetry.

**Longer-term consideration:** HEDT platform (Threadripper 7960X + TRX50
board) for true dual-×16 and expansion room for 3-4 GPUs. Deferred until
workload demands it.

### 3.2 Ollama Model Swap Limitation

VRAM budget is 48 GB total (24 GB per card). The 70B Q4_K_M model uses ~42
GB with KV cache. This leaves ~6 GB free — not enough to hot-load a second
large model. Small models (7B and under) can coexist.

For a second large model, either:
- Reduce 70B context (`num_ctx`) to free VRAM
- Accept swap latency (~30 seconds to unload 70B and load another)
- Wait for the ProArt upgrade + more VRAM or additional cards

---

## 4. Performance Baseline

Measured on the current setup (Sep 2026):

| Metric | Value |
|---|---|
| Model | Llama 3.3 70B Instruct, Q4_K_M |
| VRAM allocation | ~21 GB per card, near-symmetric |
| Cold start (Ollama load) | ~15-20 seconds |
| Warm start (already loaded) | 0 seconds |
| Token generation rate | ~19 tokens/second |
| Prompt processing | Fast, not measured |
| Idle power per card | ~20-25 W |
| Sustained inference power per card | ~280 W |
| Sustained gpu-burn power per card | ~340-350 W (validated) |
| Peak thermal per card under burn | 72-82°C (validated) |
| Thermal throttle during 30-min burn | Card 1: 0s / Card 2: 57s of 1800s |

Save this as the baseline. When the ProArt upgrade happens, rerun the
same test to quantify the improvement from proper PCIe wiring.

---

## 5. Boot & Recovery Behavior

**On host boot:**
- Kernel 6.14.0-37 boots (GRUB_DEFAULT pins it)
- vfio-pci claims both GPUs automatically
- libvirt starts
- VM autostart status: **needs verification** (`sudo virsh domautostart
  agent-sandbox-3090`). If not enabled, run:
  ```bash
  sudo virsh autostart agent-sandbox-3090
  ```

**On VM boot:**
- Ubuntu boots, NVIDIA driver loads for both cards
- `wg-quick@wg0.service` starts WireGuard, connects to EC2
- `ollama.service` starts, binds to `10.100.0.2:11434`
- Model is NOT preloaded — first user query triggers ~15-20s load
- Once loaded, `OLLAMA_KEEP_ALIVE=-1` keeps it resident indefinitely

**Optional but recommended:** add `/etc/systemd/system/ollama-preload.service`
to warm the model at boot. Config was discussed but not yet applied. Would
issue a curl to Ollama after service start to trigger model load.

**On EC2 boot:**
- nginx starts (`nginx.service` enabled)
- WireGuard server starts
- UI is served immediately

---

## 6. Recent Work Log (Sep 2026)

**Session 1 — Ubuntu recovery:**
- Broken `apt upgrade` on the host cascaded into NVIDIA driver, kernel,
  Secure Boot MOK, and HWE track issues
- Enrolled MOK, switched HWE to version-locked `linux-generic-6.14`,
  restored 580.126.20 driver stack
- Deliverable: `ubuntu-kernel-nvidia-troubleshooting.md` runbook

**Session 2 — Patching script:**
- Built `inference-host-patch.sh` — staged base/Docker/NVIDIA/cleanup
  passes with autoremove blocklist, dry-run default, vfio-pci awareness
- Companion `README.md`
- To be added to `installation-343` repo

**Session 3 — Real patch cycle:**
- Applied ~500 package backlog via the 3-pass model
- NVIDIA 580.126.20 → 580.178.04, Docker CE 29.4 → 29.8.1, kernel
  6.14.0-35 → 6.14.0-37
- Autoremove purged 401 orphans (Steam ecosystem, Debian Node.js)
- Reclaimed ~2 GB

**Session 4 — Dual GPU installation:**
- Added second RTX 3090, new PSU (be quiet! 1200W), open-air workbench
- Discovered PCIe ×1 limitation on slot 2 (accepted for now)
- IOMMU groups verified clean, both cards in separate groups
- vfio-pci already claimed both via ID-based binding (no config change
  needed)

**Session 5 — VM XML update:**
- Added 3 more `<hostdev>` blocks to `agent-sandbox-3090` for card 1
  audio, card 2 GPU, card 2 audio
- Bumped VM specs: 32 GB RAM, 16 vCPUs
- Both cards visible in guest, driver loaded correctly

**Session 6 — Burn-in validation:**
- Individual card FP32 burn: both cards pass, ~340W sustained
- Dual-card 30-minute burn: 0 errors, minor thermal throttle on card 2
  (~3% of run), no PSU trips, no crashes
- Hardware validated

**Session 7 — Ollama setup:**
- Installed Ollama in VM
- Pulled `llama3.3:70b-instruct-q4_K_M` (~40 GB)
- Created custom `llama3.3-70b-fullgpu:latest` via Modelfile with
  `num_gpu 999`
- Verified full GPU offload (no CPU spillover), 19 tok/s baseline

**Session 8 — UI cutover:**
- Diagnosed dead architecture: old llama.cpp Docker container had been
  stopped 4 months prior, `/v1/llama/` endpoint pointing at dead port
  8080
- Resolved zombie `ollama serve` process holding port 11434 (had been
  crash-looping systemd for hours)
- Updated nginx `/v1/llama/` block: port 8080 → 11434
- Updated `index.html` MODELS dict: `qwen3-coder-30b` →
  `llama3.3-70b-fullgpu:latest`
- End-to-end streaming confirmed working through browser

---

## 7. Known Gaps / Open Items

Nothing blocking, but worth eventually addressing:

- **Preload service not yet installed.** First query after VM boot has
  15-20s latency. Adding `ollama-preload.service` eliminates this.
- **VM autostart not verified.** Run `sudo virsh list --autostart` on the
  host to check. Enable with `sudo virsh autostart agent-sandbox-3090` if
  not on.
- **`installation-343` repo not updated with current state.** The
  patching script, README, VM XML, nginx config, and UI file should all
  be committed. Runbook document should be updated to reflect Sep 2026
  state.
- **No auth on Ollama endpoint.** Anyone on the WireGuard mesh can hit it
  unauthenticated. Current mesh appears to be just EC2 ↔ VM, so low risk.
  Would want to revisit if adding more peers.
- **The `/v1/` catch-all in nginx still points at dead vLLM (port 8000).**
  Not causing problems because the UI doesn't hit `/v1/` directly, only
  `/v1/llama/`. But it's dead weight in the config; either wire up vLLM
  again or remove the block.
- **Commented-out dropdown options in UI.** `/v1/fast` (Hermes-4 14B) and
  `/v1/big` (Hermes-4 70B) are placeholder options. Either wire them up
  or remove the comments.
- **`llama3.3:70b-instruct-q4_K_M` base tag can be removed if desired**
  once confident in the `llama3.3-70b-fullgpu:latest` model. Would tidy
  the model list; disk cost is ~zero due to blob dedup.

---

## 8. Common Operations

### Restart the inference stack

```bash
# On host, restart the VM
sudo virsh shutdown agent-sandbox-3090
# Wait for shutoff
sudo virsh start agent-sandbox-3090
# VM boots, WireGuard reconnects, Ollama starts
# First query after this triggers model load (~15-20s)
```

### Restart just Ollama (VM stays up)

```bash
# SSH into VM as operator
sudo systemctl restart ollama

# Model reloads on first query
```

### Check current state

```bash
# On host
sudo virsh list --all                # VM state
lspci -nnk -s 01:00.0                # confirm vfio-pci owns card 1
lspci -nnk -s 05:00.0                # confirm vfio-pci owns card 2

# On VM (via SSH)
nvidia-smi                           # confirm both cards visible
ollama list                          # loaded models
sudo ss -tlnp | grep 11434           # Ollama binding
sudo systemctl status ollama         # service state
sudo wg show wg0                     # WireGuard status

# On EC2
sudo systemctl status nginx          # nginx state
sudo wg show wg0                     # WireGuard status
curl -s http://10.100.0.2:11434/v1/models | jq .  # end-to-end Ollama reachability
```

### Update the model

```bash
# On VM
ollama pull <new-model-tag>

# If replacing the served model, create a new Modelfile-derived variant
cat > /tmp/Modelfile <<'EOF'
FROM <new-model-tag>
PARAMETER num_gpu 999
PARAMETER num_ctx 4096
EOF
ollama create <new-alias> -f /tmp/Modelfile

# Then on EC2, update /var/www/spark/index.html MODELS dict to point at
# the new alias. No nginx changes needed.
```

### Add a second model to serve concurrently

VRAM budget is tight (~6 GB free after 70B loads). Small models only.

```bash
# On VM
ollama pull qwen2.5:1.5b   # or similar small model

# On EC2, add to MODELS in index.html:
#   "/v1/fast": "qwen2.5:1.5b"
# And uncomment the <option value="/v1/fast"> line in the header select.
# Reload page — nginx routes /v1/fast/ to same Ollama backend, model tag
# in the request body picks which model responds.
```

### Rebuild the VM (if it gets wedged)

The VM's disk image is at `/data/libvirt/images/agent-sandbox-3090.qcow2`
(on the host's XFS `/data` partition). Back up the XML with `virsh
dumpxml agent-sandbox-3090 > backup.xml` before any risky operations.

Full rebuild would require:
1. `virsh undefine agent-sandbox-3090` and delete qcow2
2. Install fresh Ubuntu VM (or restore from a backup image if available)
3. Re-attach GPUs via 4 `<hostdev>` blocks (see Session 5 notes)
4. Reinstall NVIDIA driver in guest, then Ollama
5. Reconfigure WireGuard peer
6. Recreate Modelfile and pull models

---

## 9. Repos & Files of Interest

- **`installation-343`** — private GitHub repo, homelab configuration
  (should hold: `inference-host-patch.sh`, `README.md`, VM XML, nginx
  config, UI HTML, this document)
- **On EC2:**
  - `/etc/nginx/sites-available/343-guilty-spark`
  - `/var/www/spark/index.html`
  - `/etc/wireguard/wg0.conf`
- **On host:**
  - `/etc/modprobe.d/vfio.conf`
  - `/etc/default/grub` (kernel pinning)
  - `/etc/libvirt/qemu/agent-sandbox-3090.xml`
- **On VM:**
  - `/etc/systemd/system/ollama.service.d/override.conf`
  - `/etc/wireguard/wg0.conf`
  - `~/.ollama/` or `/usr/share/ollama/.ollama/` (model store — location
    depends on service user, needs verification)

---

## 10. Handoff Notes

If you're picking this up cold, the fastest way to grok the state:

1. Read Section 1 (architecture diagram)
2. Verify current state with commands in Section 8 — confirms nothing
   drifted between this document and reality
3. Look at Section 7 (open items) to see what could be worth doing next
4. Reference sections 2-4 as needed for component details

The single most important thing to know: **the VM does all the work.** The
host is just a passthrough platform. The EC2 is just a frontend. Debugging
inference issues means SSHing into the VM (`operator@agent-sandbox-3090`,
reachable via libvirt console from the host or over WireGuard as
`10.100.0.2`).

Don't try to run inference on the host — vfio-pci owns the GPUs, so
`nvidia-smi` on the host will fail and CUDA workloads won't work. This is
by design, not a bug.

If Ollama seems to be misbehaving, first check for zombie `ollama serve`
processes with `sudo ss -tlnp | grep 11434`. The systemd service and a
stray foreground process can conflict, and the resulting crash-loop is
silent unless you check `journalctl -u ollama`.
