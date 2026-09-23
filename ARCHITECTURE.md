# 343-guilty-spark.io — Architecture & Handoff

**Last updated:** 2026-09-22
**Repo:** `local-inference-deck` (private); see also `installation-343` (§9)
**Domain:** `343-guilty-spark.io`
**Status:** Live and serving — public edge verified, backend running but
**not autostarting** (§5)

> **Verification pass 2026-09-22.** Every host- and edge-level claim below
> was checked against the running system; drift was corrected in place and
> the corrections are called out inline. Two classes of claim remain
> unverified and are marked as such where they appear: anything **inside
> the guest** (guest driver/CUDA versions, Ollama override, model tags) and
> anything **on EC2** (site file, UI, WireGuard server config). Both need
> access this pass did not use.

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
   │  nginx 1.24.0 :80/:443 (HTTP/2)      │
   │    ├─ TLS (Let's Encrypt, certbot)   │
   │    ├─ :80 → 301 → :443               │
   │    ├─ auth_basic (server-level, so   │
   │    │   ALL routes inherit it)        │
   │    ├─ / → /var/www/spark/index.html  │
   │    ├─ /v1/llama/* → proxy_pass       │
   │    │               10.100.0.2:11434  │
   │    └─ /v1/* → :8000 [PARKED, vLLM]   │
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
- **nginx:** 1.24.0 (Ubuntu), serving HTTP/2
- **Web root:** `/var/www/spark/index.html`
- **nginx config:** `/etc/nginx/sites-available/343-guilty-spark`
  (symlinked from `sites-enabled/`)
- **WireGuard:** server-side, listening `51820/udp`, peer public key
  `SKGnl0A+fGx7t5iiltKRrqX8NGos2mzjhApyKc7Sugw=`

**Public edge — TLS and access control:**

Verified live 2026-09-22 against `https://343-guilty-spark.io/`:

- **TLS:** valid Let's Encrypt certificate, `CN = 343-guilty-spark.io`,
  issuer `Let's Encrypt YE2`, valid `2026-08-27` → `2026-11-25`. Issued by
  certbot; renewal is presumably on the standard `certbot.timer`, though
  the timer state has not been confirmed on the instance.
- **HTTP/2** negotiated on `:443`. Port `:80` redirects to HTTPS.
- **Access control:** HTTP Basic auth gates the entire site — the `401`
  comes back before any content is served, including the static UI.
  Realm is exactly `343 Guilty Spark`.

  ```
  HTTP/2 401
  www-authenticate: Basic realm="343 Guilty Spark"
  ```

  This is nginx's `auth_basic` + `auth_basic_user_file /etc/nginx/.htpasswd`,
  declared at the `server` level so every `location` inherits it.

  > **Note:** nginx has no `.htaccess` support — that is an Apache
  > mechanism. Per-directory override files are not read. The only
  > mechanism in play here is `auth_basic` plus an htpasswd file, and the
  > `.htpasswd` file is already in `.gitignore`.

Because basic auth is inherited by the `/v1/llama/` block, the inference
endpoint is not reachable from the public internet without credentials.
The browser's basic-auth header travels on to Ollama, which ignores it
(Ollama does no auth of its own — see §7).

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

**Parked `/v1/` catch-all:** a second block points at `10.100.0.2:8000`
with a bearer token for vLLM. The backend is not running, so the block is
inert — but it is **parked, not dead** (see §3.1: tensor-parallel vLLM is
blocked on the PCIe ×1 slot until the ProArt upgrade). Leave it in place.

It does not shadow the active route: nginx matches prefix `location`s
longest-first, so `/v1/llama/` always wins over `/v1/` regardless of
declaration order. The UI only ever calls `/v1/llama/*`.

### 2.2 WireGuard Mesh

- **Subnet:** `10.100.0.0/24`
- **EC2:** WG server, listens on `51820/udp`, public endpoint
  `13.217.253.94:51820`
- **VM:** WG peer at `10.100.0.2/24`, wg-quick@wg0.service enabled and
  active
- **Host (`bishop-X870-GAMING-WIFI6`):** WireGuard is **not installed at
  all** — no `wg` binary, no `/etc/wireguard/`, no `wg-quick@` unit, no
  packages. (Verified 2026-09-22; an earlier revision of this doc said
  "installed but inactive", which overstated what is on disk.) This is
  fine: the host is not on the mesh and does not need to be, because the
  VM holds its own tunnel. Host interfaces are `enp7s0` (192.168.1.83/24)
  and `virbr127-nat` (10.127.10.1/24).

  **Consequence for debugging:** from the host you cannot reach
  `10.100.0.2` at all — there is no route to `10.100.0.0/24`. Pinging it
  or curling `10.100.0.2:11434` from the host times out, and that is
  expected, not a fault. Reach Ollama either from the EC2 side of the
  tunnel or via the guest's libvirt NAT address.

### 2.3 VM: agent-sandbox-3090

- **libvirt name:** `agent-sandbox-3090`
- **User:** `operator`
- **OS:** Ubuntu (Linux guest, matching kernel driver 580.173.02)
- **Resources:** 32 GB RAM, 16 vCPUs, host-passthrough CPU
- **Networks:**
  - libvirt network **`iac127-nat`** (bridge `virbr127-nat`, gateway
    `10.127.10.1/24`); the VM leases `10.127.10.160`. This is a
    purpose-built network, **not** a customized `default` — the stock
    `default` network still exists, still holds `192.168.122.1/24`, and
    its `virbr0` is DOWN. Sibling network `iac127-isolated` also exists.
    All three autostart.
  - WireGuard `wg0` at `10.100.0.2`
- **GPUs:** Both RTX 3090s attached via four `<hostdev>` blocks (2 GPU
  functions + 2 audio functions, PCI IDs 10de:2204 and 10de:1aef)
- **Guest PCI addresses:** GPU 0 at `05:00.0` (host card at `01:00.0`,
  the ×16 slot), GPU 1 at `09:00.0` (host card at `05:00.0`, the ×1 slot)
- **NVIDIA driver in guest:** 580.173.02, CUDA 13.0

**Ollama configuration** (`/etc/systemd/system/ollama.service.d/override.conf`):

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_NUM_PARALLEL=2"
```

- `OLLAMA_KEEP_ALIVE=-1` keeps the model loaded forever (no eviction)
- `OLLAMA_NUM_PARALLEL=2` lets 2 concurrent requests share the loaded model

> ### ⚠ Correction: Ollama binds `0.0.0.0`, not the tunnel address
>
> Earlier revisions of this document stated `OLLAMA_HOST=10.100.0.2:11434`
> and described the bind as *"only to WireGuard interface (surgical, not
> `0.0.0.0`)"*. **That was wrong.** Verified in the guest 2026-09-22:
>
> ```
> $ cat /etc/systemd/system/ollama.service.d/override.conf
> Environment="OLLAMA_HOST=0.0.0.0:11434"
> $ sudo ss -tlnp | grep 11434
> LISTEN 0 4096 *:11434 *:*  users:(("ollama",pid=1029,fd=4))
> $ curl -o /dev/null -w '%{http_code}' http://10.127.10.160:11434/v1/models
> 200          # answers on the libvirt NAT address too
> ```
>
> Ollama listens on **every interface the VM has**. What actually prevents
> that from being reachable is **ufw** (§2.3.1) — its default incoming
> policy is deny, and the allow rule for 11434 is scoped to the `wg0`
> interface. From the host, `curl 10.127.10.160:11434` times out for
> precisely this reason.
>
> **This inverts the security story.** The protection is not the bind
> address; it is one firewall rule. `ufw disable`, a flushed ruleset, or
> losing the interface scope would immediately publish an unauthenticated
> 70B endpoint onto the libvirt network. Tightening the bind address would
> add a second, independent control — see the hardening note in
> `unit-files/ollama-override.conf`, including the wg0 startup-ordering
> caveat that is the likely reason it is `0.0.0.0` today. Not yet applied;
> tracked in §7.2.

**Model store:** service runs as `User=ollama`, so weights live under
`/usr/share/ollama/.ollama/`. (Resolves an item §9 previously flagged
unverified.)

**Loaded models:**
- `llama3.3-70b-fullgpu:latest` — custom Modelfile derived from
  `llama3.3:70b-instruct-q4_K_M` with `num_gpu 999` and `num_ctx 4096`.
  This is the model the UI targets.
- `llama3.3:70b-instruct-q4_K_M` — base model, kept as a reference. Ollama
  dedupes blob storage so actual disk cost is ~42 GB not 84.

#### 2.3.1 ufw on the VM — the actual access control

Previously undocumented, and load-bearing. Verified in the guest
2026-09-22:

```
$ sudo ufw status verbose
Status: active
Default: deny (incoming), allow (outgoing), deny (routed)

To                         Action      From
--                         ------      ----
8000/tcp                   ALLOW IN    10.100.0.0/24
22/tcp                     ALLOW IN    Anywhere
11434/tcp on wg0           ALLOW IN    Anywhere
8080/tcp on wg0            ALLOW IN    Anywhere
```

`ufw` is **active and enabled**, `iptables -S` confirms `-P INPUT DROP`.

| Rule | Purpose | Verdict |
|---|---|---|
| `11434/tcp on wg0` | Ollama, tunnel only | **required — do not remove** |
| `22/tcp Anywhere` | SSH | works; consider scoping (below) |
| `8000/tcp from 10.100.0.0/24` | parked vLLM (§3.1) | keep while vLLM is parked |
| `8080/tcp on wg0` | dead llama.cpp Docker port | **stale — removable** (§7.2) |

**Does the WireGuard tunnel itself need a rule? No.** The VM *dials out* to
EC2 (`13.217.253.94:51820`); EC2 is the listener. Outbound plus
established/related return traffic is permitted by the default
`allow (outgoing)` policy, so no inbound rule for `51820` is needed or
present.

**But traffic arriving over the tunnel does.** nginx on EC2 opens a *new*
inbound connection to `10.100.0.2:11434`. Under `deny (incoming)` that is
dropped unless explicitly allowed — which is what
`11434/tcp on wg0` does. **This rule is why inference works at all.** It is
also, given the `0.0.0.0` bind (§2.3), the only thing confining Ollama to
the tunnel.

The `on wg0` interface scope is the important part: it permits 11434 *only*
on the tunnel interface, not on the libvirt NAT or any future interface.
Were the rule written as a bare `ufw allow 11434/tcp`, Ollama would be
reachable from the libvirt network immediately. Preserve the scope.

**SSH note:** `22/tcp` is allowed from `Anywhere`, which is how this
verification pass reached the guest over the libvirt NAT
(`operator@10.127.10.160`). The VM has no public route, so this is not an
internet exposure, but scoping it to `wg0` and/or the libvirt subnet would
be consistent with how 11434 is handled. Operator's call — note that
tightening it removes the NAT path that currently serves as the
out-of-band way in if the tunnel breaks.

**Verified guest internals** (closing §10's previously-unverifiable items):

| Item | Verified |
|---|---|
| NVIDIA driver / CUDA | `580.173.02` / CUDA `13.0` — matches §2.3 |
| PCIe width per card | card 0 ×16, card 1 ×1 — **confirms §3.1** |
| VRAM in use | 21960 MiB + 21798 MiB — matches §4's "~21 GB per card" |
| Model tags | both `llama3.3-70b-fullgpu:latest` and the base tag present, 42 GB each |
| Residency | `ollama ps` → `100% GPU`, context `4096`, `UNTIL: Forever` — `KEEP_ALIVE=-1` working |
| `wg-quick@wg0` | active + enabled; handshake fresh, keepalive 25s, `allowed ips 10.100.0.0/24` |
| Preload unit | **not installed** — confirms §7.3 |

> **Caveat on `pcie.link.gen.current`:** at idle both cards report gen 1
> (ASPM power saving) even though card 0 negotiates ×16. Link *width* is the
> stable signal; *gen* only reads true under load. Sample it during
> generation when comparing before/after the ProArt upgrade.

---

### 2.4 Host: bishop-X870-GAMING-WIFI6

- **CPU:** AMD Ryzen 9950X3D
- **RAM:** 128 GB DDR5 (4×32 GB)
- **Motherboard:** Gigabyte X870 GAMING WIFI6
- **PSU:** be quiet! 1200W 80+ Gold ATX 3.1 (2000W transient headroom)
- **GPUs:** Dual ZOTAC RTX 3090 (subsystem `19da:1613`)
- **Cooling:** Open-air workbench, cards mounted horizontally with spacing
- **Storage:** single 931.5 GB `nvme1n1`, **all data partitions XFS**
  (an earlier revision of this doc called root ext4 — it is XFS):

  | Partition | FS | Size | Mount |
  |---|---|---|---|
  | `nvme1n1p1` | vfat | 1 G | `/boot/efi` |
  | `nvme1n1p2` | XFS | 93.1 G | `/` |
  | `nvme1n1p3` | XFS | 186.3 G | `/home` |
  | `nvme1n1p4` | XFS | 465.7 G | `/data` — also mounted at `/var/lib/docker/volumes/ollama/_data/models` (see §2.5) |
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

### 2.5 Undocumented: a second Ollama on the host

Found during verification 2026-09-22. Not part of the serving path, but it
exists and was previously undocumented:

```
$ systemctl is-active ollama && systemctl is-enabled ollama
active
enabled
$ ss -tlnp | grep 11434
LISTEN 0 4096 127.0.0.1:11434 0.0.0.0:*
$ curl -s http://127.0.0.1:11434/api/tags
{"models":[{"name":"qwen3-coder:30b", ... "parameter_size":"30.5B",
            "quantization_level":"Q4_K_M"}]}   # 18.5 GB
```

- Binds `127.0.0.1:11434` — loopback only, so it is not reachable from the
  mesh or the LAN, and it does **not** conflict with the VM's Ollama (that
  one binds `10.100.0.2:11434`, a different address, same port).
- Holds one model, `qwen3-coder:30b`, last modified 2025-09-12. This is the
  lineage of the `qwen3-coder-30b` tag that §6 Session 8 describes as the
  *old dead-architecture* target.
- **It is CPU-only.** vfio-pci owns both GPUs, so this Ollama has no CUDA
  device — a 30B model here would run on the 9950X3D at a small fraction
  of the VM's throughput.
- Its model store is on the `nvme1n1p4` mount at
  `/var/lib/docker/volumes/ollama/_data/models` (see §2.4), a Docker
  volume path — consistent with the retired llama.cpp/Docker era.

**Almost certainly a leftover.** It is enabled, so it starts on every boot
and holds a service on loopback :11434 forever. Note the tension with §10
("Don't try to run inference on the host") — that guidance is still
correct about *why* (no GPU access), but a host Ollama does exist and will
answer on loopback, which could mislead someone debugging. Decide whether
to remove it (see §7).

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

**This is why vLLM is parked.** The choice of serving engine is dictated by
this one wire:

| | inter-GPU traffic | ×1 link | status |
|---|---|---|---|
| Pipeline-parallel (Ollama/llama.cpp) | small per token — activations at layer boundaries | fine | **active** |
| Tensor-parallel (vLLM) | large per token — all-reduce every layer | bottleneck | **parked** |

vLLM is not a worse engine; it is the wrong engine *for this slot wiring*.
The repo deliberately retains the vLLM-generation artifacts
(`scripts/vllm-serve.bash`, `unit-files/vllm.service`,
`nginx-configs/nginx.conf`, and the `/v1/` nginx block) so the path can be
revived rather than rebuilt.

**Planned upgrade:** ASUS ProArt X870E-Creator WiFi board (~$550). Supports
×8/×8 bifurcation with both primary slots populated. PCIe 5.0 ×8 to each
card is bandwidth-equivalent to PCIe 4.0 ×16, saturating the 3090s'
capability. Would eliminate the asymmetry — and is therefore the gate on
un-parking vLLM.

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

**Reproduce it with `scripts/inference-baseline.sh`** (run inside the VM —
the host cannot, since vfio-pci owns the GPUs). It derives tok/s from
Ollama's own `eval_count`/`eval_duration` counters rather than wall-clock,
and reports `pcie.link.gen.current` / `pcie.link.width.current` per card —
which is the single clearest before/after signal for the upgrade, since the
whole point is turning ×16 + ×1 into ×8 + ×8. Cold-start measurement is
opt-in because it must evict the resident model, making the next real query
pay a 15-20s reload.

---

## 5. Boot & Recovery Behavior

**On host boot:**
- Kernel 6.14.0-37 boots (GRUB_DEFAULT pins it)
- vfio-pci claims both GPUs automatically
- libvirt starts
- **VM autostart is ON** as of 2026-09-22. It was found off during
  verification and enabled at operator request; the boot path below now
  completes unattended. Original finding, for the record:
  ```
  $ virsh -c qemu:///system dominfo agent-sandbox-3090
  State:          running
  Persistent:     yes
  Autostart:      disable      <-- confirmed
  ```
  `virsh list --autostart` returns an empty list and
  `/etc/libvirt/qemu/autostart/` does not exist, both corroborating.

  At that point an unattended reboot would **not** have brought inference
  back up. The then-running VM had been started by hand — the journal shows
  `virsh start agent-sandbox-3090` run via sudo 68 seconds after boot, not
  by autostart. (`libvirt-guests.service` is enabled but has nothing to
  resume: `Managed save: no`.)

  **Enabled 2026-09-22** (no sudo needed — `bishop` is in group `libvirt`):
  ```bash
  virsh -c qemu:///system autostart agent-sandbox-3090            # done
  virsh -c qemu:///system autostart --disable agent-sandbox-3090  # revert
  ```
  Confirmed by `dominfo` (`Autostart: enable`), `list --autostart`, and the
  symlink `/etc/libvirt/qemu/autostart/agent-sandbox-3090.xml`.

  Accepted consequence: the VM now claims both GPUs and ~42 GB of VRAM on
  every boot, at ~20-25 W/card idle. **Still to prove: an actual reboot.**
  The setting is verified; the end-to-end unattended recovery is not.

  > **Command correction:** earlier revisions of this doc said to check
  > with `virsh domautostart <domain>`. **That subcommand does not
  > exist** — on libvirt 10.0.0 it returns
  > `error: unknown command: 'domautostart'`. There is no `dom`-prefixed
  > form. Read the state with `virsh dominfo <domain>` or
  > `virsh list --autostart`; set it with `virsh autostart <domain>`.

**On VM boot:**
- Ubuntu boots, NVIDIA driver loads for both cards
- `wg-quick@wg0.service` starts WireGuard, connects to EC2
- `ollama.service` starts, binds `0.0.0.0:11434` (§2.3 — reachable only
  on `wg0` because of the ufw scope, §2.3.1)
- Model is NOT preloaded — first user query triggers ~15-20s load
- Once loaded, `OLLAMA_KEEP_ALIVE=-1` keeps it resident indefinitely

**Preload unit now exists in the repo, not yet installed.**
`unit-files/ollama-preload.service` eliminates the 15-20s first-query
penalty. Install instructions are in its header comment. Design notes:

- Polls `/v1/models` on a bounded 1s/60-attempt loop rather than
  `sleep`-ing, because `After=ollama.service` only means systemd started
  the process — the listening socket appears later, and here it is also
  gated on `wg0` having configured `10.100.0.2` (Ollama binds that address,
  not `0.0.0.0`).
- Both `Exec` lines are `-`-prefixed and dependencies are `Wants=` rather
  than `Requires=`, so a failed warm-up degrades to today's behaviour
  (first real query pays the cold start) and can never fail the boot.

Note it is `WantedBy=multi-user.target` **inside the VM**, so it only helps
once the VM is actually running — which currently requires a manual
`virsh start`. Enabling VM autostart above is the prerequisite for
unattended warm recovery.

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
- ~~To be added to `installation-343` repo~~ — **done.** Committed there
  as `scripts/patch-virt-host.sh` + `scripts/PATCH-VIRT-HOST-README.md`
  (commit `9d36bdd`, PR #23). The name `inference-host-patch.sh` above is
  historical and matches no file on disk; see §9.

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

Re-verified against reality 2026-09-22. Items are grouped by whether they
need a decision or just work.

### 7.1 Resolved / narrowed since last revision

- **~~VM autostart~~ → ENABLED 2026-09-22. DONE.** Was confirmed off, then
  turned on at operator request:
  ```
  $ virsh -c qemu:///system autostart agent-sandbox-3090
  Domain 'agent-sandbox-3090' marked as autostarted
  $ virsh -c qemu:///system dominfo agent-sandbox-3090 | grep Autostart
  Autostart:      enable
  ```
  Corroborated by the new symlink
  `/etc/libvirt/qemu/autostart/agent-sandbox-3090.xml`. No sudo was needed
  (`bishop` is in group `libvirt`). **Not yet proven by an actual reboot** —
  that is the real test, and worth doing deliberately rather than
  discovering at the next power cut.
- **~~Patching script not committed~~ → already committed, under a
  different name.** It lives in the *other* repo,
  `~/source/installation-343`, as `scripts/patch-virt-host.sh` plus
  `scripts/PATCH-VIRT-HOST-README.md` (commit `9d36bdd`, PR #23). §6
  Session 2 calls it `inference-host-patch.sh`; no file by that name
  exists anywhere on the host. The remaining repo TODO is therefore
  narrower than previously stated: **VM XML, nginx config, UI HTML, and
  the runbook.**
- **Public edge auth → present and verified.** TLS + basic auth gate the
  whole site (§2.1). This does **not** close the Ollama item below — see
  the layering note there.

### 7.2 Needs a decision from the operator

- **Host-side Ollama (§2.5): remove or keep?** `active` and `enabled` on
  loopback :11434 with a stale `qwen3-coder:30b`. CPU-only, so of little
  use, and a plausible source of confusion for a future debugger who
  curls `localhost:11434` and gets a *different* Ollama than the one
  serving traffic. Removal is `sudo systemctl disable --now ollama` on the
  **host** (never in the VM).
- **Ollama binds `0.0.0.0`; ufw is the only control (§2.3, §2.3.1).**
  The highest-value item on this list. **Fix is now staged**, not applied:
  `unit-files/ollama-override.conf` binds `10.100.0.2:11434` and adds a
  `[Unit]` section with `After=` + `Requires=wg-quick@wg0.service`.
  Apply during a maintenance window via
  `docs/reboot-validation-checklist.md`; a restart cannot validate it
  because it passes while wg0 is already up.

  Two details worth carrying forward:
  - The ordering directives must sit under **`[Unit]`**, not `[Service]`.
    A drop-in may carry both sections, and `After=`/`Requires=` under
    `[Service]` is silently ignored — which would leave the race unfixed
    while appearing fixed.
  - **`Requires=`, not `Wants=`.** The vendor unit ships
    `Restart=always` with `RestartUSec=3s`, so under `Wants=` a tunnel
    failure would let Ollama start, fail to bind, and restart every 3
    seconds indefinitely — the silent crash-loop §10 warns about.
    `Requires=` refuses to start it at all, making the failure loud and
    singular. The trade is that a transient wg0 failure keeps Ollama down
    until someone intervenes.
- **~~ufw rule for 11434~~ → already present and required. NOTHING TO DO.**
  Resolved 2026-09-22. The tunnel itself needs no inbound rule (the VM
  dials out), but traffic *arriving* over it does, and
  `11434/tcp on wg0 ALLOW IN` is what makes inference work. It was never
  missing — only undocumented. Now §2.3.1.
- **Stale ufw rule: `8080/tcp on wg0`.** Confirmed removable. Port 8080
  served the llama.cpp Docker container retired ~4 months ago (§6 Session
  8). Verified on the VM: nothing listening, Docker holds **0 containers,
  0 images, 0 volumes**, and there is no llama.cpp binary or systemd unit
  — so unlike the vLLM `8000` rule, no artifact exists to revive. That is
  the distinction between *parked* and *dead*.

  Worth removing rather than ignoring, because it is a **latent grant**:
  nothing listens today, but `~/.hermes/skills/creative/p5js/scripts/serve.sh`
  defaults to port 8080 and `python3 -m http.server` binds all interfaces,
  so running that helper would publish its working directory over the
  tunnel with no firewall change. Delete by rule *number* (highest first)
  since numbers shift and an IPv6 twin exists — see
  `docs/reboot-validation-checklist.md`.
- **Runbook is stale and uncommitted.** Two identical copies sit in
  `~/Downloads/` (`ubuntu-kernel-nvidia-troubleshooting.md` and
  `...(1).md`, 19426 bytes each). Dated 2026-04-19 and written when the
  box had a **single** 3090 with the AMD iGPU driving display — so it
  predates the dual-GPU and vfio-pci-both-cards reality. Needs updating
  before it is trusted, then committing.

### 7.3 Straightforward work

- **Preload service not installed.** Unit now exists at
  `unit-files/ollama-preload.service`; needs installing in the VM. Note
  it only helps after the VM is up, so it pairs with the autostart
  decision.
- **No auth on the Ollama endpoint itself.** Still true and still worth
  noting, despite the nginx basic auth. The two live at different layers:
  nginx protects the **public** path (`443 → /v1/llama/`), but a peer
  already on the WireGuard mesh reaching `10.100.0.2:11434` **directly**
  bypasses nginx entirely and meets no authentication, because Ollama has
  no API-key mechanism. Low risk while the mesh is just EC2 ⇄ VM;
  revisit before adding peers.
- **The parked `/v1/` catch-all.** Keep it — reclassified from "dead
  weight" to parked (§2.1, §3.1). It cannot shadow `/v1/llama/` because
  nginx matches prefixes longest-first.
- **Commented-out dropdown options in UI.** `/v1/fast` (Hermes-4 14B) and
  `/v1/big` (Hermes-4 70B) placeholders. Either wire up or delete. Note
  the VRAM budget (§3.2) only permits a small second model.
- **`llama3.3:70b-instruct-q4_K_M` base tag** can be removed once
  confident in the `-fullgpu` variant; disk cost is ~zero due to blob
  dedup, so this is cosmetic.
- **Baseline is now reproducible.** `scripts/inference-baseline.sh` exists
  to re-run §4's measurements; previously the numbers were recorded with
  no way to reproduce them.

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
# ---- On host (no sudo needed: user `bishop` is in group `libvirt`) ----
virsh -c qemu:///system list --all           # VM state
virsh -c qemu:///system dominfo agent-sandbox-3090   # incl. Autostart
virsh -c qemu:///system domblklist agent-sandbox-3090  # disk paths
lspci -nnk -s 01:00.0   # vfio-pci owns card 1 (x16 slot)
lspci -nnk -s 05:00.0   # vfio-pci owns card 2 (x1 slot)
ping -c2 10.127.10.160  # VM alive on libvirt NAT (iac127-nat)

# ---- On VM (via SSH or `virsh console`) ----
nvidia-smi                           # confirm both cards visible
ollama list                          # loaded models
sudo ss -tlnp | grep 11434           # Ollama binding
sudo systemctl status ollama         # service state
sudo wg show wg0                     # WireGuard status

# ---- On EC2 ----
sudo systemctl status nginx          # nginx state
sudo wg show wg0                     # WireGuard status
curl -s http://10.100.0.2:11434/v1/models | jq .  # Ollama over the tunnel

# ---- From anywhere: is the public edge alive? ----
curl -sS -o /dev/null -w '%{http_code}\n' https://343-guilty-spark.io/
# 401 is CORRECT and healthy -- basic auth is working (see 2.1).
```

> **Do not run the `10.100.0.2` curl on the host.** The host has no
> WireGuard (§2.2) and therefore no route to `10.100.0.0/24`; it will hang
> until timeout. That is expected, not a fault. Use the EC2 side, or reach
> the guest on `10.127.10.160`.
>
> Also note `curl localhost:11434` **on the host** answers — but that is
> the unrelated leftover host Ollama (§2.5), not the serving instance.

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

The VM's disk image is at
**`/data/iac127/disks/agent-sandbox-3090.qcow2`** (on the host's XFS
`/data` partition), with a cloud-init seed at
`/data/iac127/seeds/agent-sandbox-3090-seed.iso`. Confirm with:

```bash
virsh -c qemu:///system domblklist agent-sandbox-3090
```

> **Path correction:** earlier revisions said
> `/data/libvirt/images/agent-sandbox-3090.qcow2`. **That directory does
> not exist.** Storage is namespaced under `/data/iac127/` with libvirt
> pools `disks`, `isos`, `seeds`, `tmp`. Do not follow the old path — a
> rebuild guided by it would not find the disk.

Back up the XML with `virsh dumpxml agent-sandbox-3090 > backup.xml`
before any risky operations. Note `/etc/libvirt/qemu/agent-sandbox-3090.xml`
is `0600 root:root`, so read it via `virsh dumpxml` rather than `cat`.

Full rebuild would require:
1. `virsh undefine agent-sandbox-3090` and delete qcow2
2. Install fresh Ubuntu VM (or restore from a backup image if available)
3. Re-attach GPUs via 4 `<hostdev>` blocks (see Session 5 notes)
4. Reinstall NVIDIA driver in guest, then Ollama
5. Reconfigure WireGuard peer
6. Recreate Modelfile and pull models

---

## 9. Repos & Files of Interest

### Repos

- **`local-inference-deck`** (this repo) — holds this document plus the
  serving artifacts:

  | Path | Generation | Status |
  |---|---|---|
  | `ARCHITECTURE.md` | — | this document |
  | `nginx-configs/spark-ollama.conf` | current | template matching live EC2 |
  | `unit-files/ollama-override.conf` | current | **hardened target, not yet live** (§7.2) |
  | `docs/reboot-validation-checklist.md` | current | apply + verify procedure |
  | `unit-files/ollama-preload.service` | current | **not yet installed** (§7.3) |
  | `models/Modelfile.llama3.3-70b-fullgpu` | current | matches served model |
  | `scripts/inference-baseline.sh` | current | re-runs §4 measurements |
  | `nginx-configs/nginx.conf` | vLLM | **parked** (§3.1) |
  | `unit-files/vllm.service` | vLLM | **parked** |
  | `scripts/vllm-serve.bash` | vLLM | **parked** |
  | `server/index.html` | stale | **drifted from live** — see below |
  | `wireguard-configs/` | — | empty; real confs live on the hosts, uncommitted |

  > **`server/index.html` is not the live UI.** The committed copy has a
  > hardcoded `ENDPOINT = "/v1/chat/completions"` and no model selector.
  > The live `/var/www/spark/index.html` uses a `MODELS` dict targeting
  > `/v1/llama` and a different persona (humor 85 / honesty 95 vs 75 / 90).
  > Treat the live file as source of truth and re-export it.

- **`installation-343`** — the other private repo, at
  `~/source/installation-343`. Already holds
  `scripts/patch-virt-host.sh` and `scripts/PATCH-VIRT-HOST-README.md`
  (§7.1). §6 Session 2 refers to this script as
  `inference-host-patch.sh`; that name does not exist on disk.

### Uncommitted, on disk

- `~/Downloads/ubuntu-kernel-nvidia-troubleshooting.md` (and a duplicate
  `...(1).md`) — the Session 1 runbook. Stale: single-GPU era (§7.2).

### On EC2

- `/etc/nginx/sites-available/343-guilty-spark` (symlinked from `sites-enabled/`)
- `/etc/nginx/.htpasswd` — basic auth credentials (gitignored; **never commit**)
- `/etc/letsencrypt/live/343-guilty-spark.io/` — certbot material
- `/var/www/spark/index.html` — the real UI
- `/etc/wireguard/wg0.conf`

### On host (`bishop-X870-GAMING-WIFI6`)

- `/etc/modprobe.d/vfio.conf` — verified byte-for-byte against §2.4
- `/etc/default/grub` — kernel pinning; also carries
  `amd_iommu=on iommu=pt`
- `/etc/libvirt/qemu/agent-sandbox-3090.xml` — `0600 root:root`; read via
  `virsh dumpxml`
- `/data/iac127/disks/agent-sandbox-3090.qcow2` — VM disk
- `/data/iac127/seeds/agent-sandbox-3090-seed.iso` — cloud-init seed
- `/var/lib/shim-signed/mok/MOK.der` — enrolled MOK
- *(no `/etc/wireguard/` — WireGuard is not installed here, §2.2)*

### On VM (`agent-sandbox-3090`, user `operator`)

- `/etc/systemd/system/ollama.service.d/override.conf`
- `/etc/wireguard/wg0.conf`
- Model store: `~/.ollama/` or `/usr/share/ollama/.ollama/` — depends on
  the service user; **still unverified** (needs guest access)

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

**There are two Ollamas on this hardware — do not confuse them.**

| | binds | GPUs | role |
|---|---|---|---|
| **VM** `agent-sandbox-3090` | `10.100.0.2:11434` | both 3090s | **serves production traffic** |
| **Host** (leftover, §2.5) | `127.0.0.1:11434` | none (CPU-only) | stale `qwen3-coder:30b` |

Same port, different addresses, so they coexist without conflict — but
`curl localhost:11434` **on the host** answers from the wrong one. When
debugging inference, confirm you are talking to the VM's instance. Running
`systemctl restart ollama` on the host restarts the leftover and will
appear to do nothing, because production Ollama is inside the guest.
