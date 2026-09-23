# 343 Guilty Spark

A minimal self-hosted chat UI backed by a private inference endpoint on your own hardware, fronted by nginx on AWS, and connected over a single WireGuard tunnel. Single-page terminal interface, streaming completions, basic auth, and an OpenAI-compatible API surface.

Currently serving **Llama 3.3 70B Instruct (Q4_K_M)** via **Ollama** across dual RTX 3090s in a GPU-passthrough VM, at roughly **19 tokens/second**.

> "I am the Monitor of Installation 04. I am 343 Guilty Spark."

For the deep internals — full component inventory, hardware specs, burn-in data, boot/recovery behavior, and the session-by-session work log — see [`ARCHITECTURE.md`](ARCHITECTURE.md). This README is the front door: what it is, why it's built this way, and what it takes to reproduce.

## Why

Most "host your own LLM" tutorials either expose your GPU to the public internet or leave you with a setup that only works on `localhost`. This is the middle path: the GPU stays on your local network, the public surface area is a small EC2 instance you control, and the link between them is a single UDP tunnel. No third-party tunneling service, no API keys leaking through reverse proxies, no GPU sitting on a public IP.

## Architecture

```
                    ┌──────────────────────────────────────────┐
                    │  User on laptop or phone                 │
                    │  https://343-guilty-spark.io             │
                    └──────────────────┬───────────────────────┘
                                       │ HTTPS (TLS + basic auth)
                                       ▼
              ┌────────────────────────────────────────────────┐
              │  AWS EC2 (Elastic IP, public)                  │
              │                                                │
              │   nginx :80/:443                               │
              │   ├─ TLS via Let's Encrypt                     │
              │   ├─ auth_basic, realm "343 Guilty Spark"      │
              │   ├─ /          → /var/www/spark/index.html    │
              │   └─ /v1/llama/ → rewrite to /v1/ and          │
              │                   proxy_pass upstream          │
              │                   (SSE, proxy_buffering off)   │
              │                                                │
              │   WireGuard wg0 (server, UDP/51820)            │
              └──────────────────┬─────────────────────────────┘
                                 │ WireGuard, 10.100.0.0/24
                                 │ (EC2 listens, VM dials out)
                                 ▼
              ┌────────────────────────────────────────────────┐
              │  VM: agent-sandbox-3090  (behind home NAT)     │
              │  ── this is where all the work happens ──      │
              │                                                │
              │   WireGuard wg0 = 10.100.0.2                   │
              │                                                │
              │   Ollama, bound to 10.100.0.2:11434            │
              │   ├─ OpenAI-compatible API                     │
              │   ├─ llama3.3-70b-fullgpu:latest               │
              │   │    (Llama 3.3 70B Instruct, Q4_K_M)        │
              │   └─ OLLAMA_KEEP_ALIVE=-1 (never evicted)      │
              │                                                │
              │   Both RTX 3090s attached via vfio-pci         │
              └──────────────────┬─────────────────────────────┘
                                 │ KVM/QEMU device passthrough
                                 ▼
              ┌────────────────────────────────────────────────┐
              │  Host workstation (Ubuntu 24.04 LTS)           │
              │  ── passthrough platform only ──               │
              │                                                │
              │   AMD 9950X3D, 128 GB DDR5                     │
              │   Dual RTX 3090, open-air workbench            │
              │   vfio-pci owns both GPUs at boot              │
              │   libvirt/QEMU hosts the VM                    │
              │                                                │
              │   nvidia-smi on the host FAILS BY DESIGN       │
              └────────────────────────────────────────────────┘
```

The local sandbox initiates the tunnel outbound — no port forwarding on your home router, no inbound rules on the GPU host, no exposure of the inference endpoint to anything outside the tunnel.

**The host/VM split matters.** The host boots, hands both GPUs to `vfio-pci`, and starts libvirt. That's its whole job. The VM owns the cards, the NVIDIA driver, WireGuard, and Ollama. Debugging inference means getting into the VM — not the host. CUDA on the host is intentionally broken, because `vfio-pci` has the GPUs.

## Components

| Component | Where | Purpose |
|---|---|---|
| EC2 instance | AWS | Stable public IP, TLS termination, static UI hosting, reverse proxy |
| nginx | EC2 | TLS, basic auth, static files, `/v1/llama/` proxy to the tunnel |
| Let's Encrypt | EC2 | Free certs via certbot, auto-renewing |
| WireGuard | Both ends | Encrypted tunnel, `10.100.0.0/24`, EC2 listens on UDP/51820 |
| libvirt/QEMU + vfio-pci | Host | Hands both RTX 3090s to the VM |
| Ollama | VM | OpenAI-compatible inference server, bound to `10.100.0.2:11434` |
| `llama3.3-70b-fullgpu:latest` | VM | Llama 3.3 70B Instruct Q4_K_M, fully GPU-offloaded |
| `index.html` | EC2 | Single-file streaming chat UI, mobile-friendly |

## Why Ollama and not vLLM — the ×1 slot problem

This is the most interesting technical constraint in the build, and it's the reason the backend is what it is.

The two GPUs are **not** wired symmetrically. On this consumer motherboard:

- **Card 1** sits in the primary slot: PCIe 4.0 **×16**, full bandwidth.
- **Card 2** sits in the second slot, which is chipset-attached with only **one lane physically wired**: PCIe 4.0 **×1**.

That's a 16× reduction in inter-GPU bandwidth on one side of the pair. What that does — and doesn't — break is the whole story:

- **Tensor-parallel inference (vLLM's model) would bottleneck on the ×1 link.** Tensor parallelism splits individual layers across cards and exchanges activations constantly. It needs fat, symmetric interconnect. It does not have that here.
- **Pipeline-parallel inference (the Ollama / llama.cpp default) is largely unaffected.** Pipeline parallelism splits the model into contiguous layer blocks, one per card, and passes a single small activation tensor across the boundary per token. Per-token inter-GPU traffic is tiny, and ×1 handles it fine.
- Model **loading** to card 2 is roughly 10-20× slower, but that happens once at service start and is invisible thereafter.
- Measured result: **19 tok/s on 70B Q4_K_M, consistent across runs.** No thermal throttling problems in practice (validated by a 30-minute dual-card burn-in).

So vLLM is **parked, not dead.** The repo deliberately keeps the vLLM-generation launcher, unit file, and nginx config as templates (see [Repository layout](#repository-layout)). The gate on bringing them back is a motherboard: a planned **ASUS ProArt X870E-Creator WiFi** (~$550) supports **×8/×8 bifurcation** with both primary slots populated. PCIe 5.0 ×8 per card is bandwidth-equivalent to PCIe 4.0 ×16 and saturates what a 3090 can use. That upgrade removes the asymmetry and makes tensor-parallel serving viable again.

The generalizable lesson: **before you pick a serving stack, check how your second slot is actually wired.** A board spec sheet that says "two PCIe ×16 slots" is describing the connectors, not the lanes behind them.

## Performance baseline

Measured on the current setup (Sep 2026) — see [`ARCHITECTURE.md` §4](ARCHITECTURE.md) for the full table.

| Metric | Value |
|---|---|
| Model | Llama 3.3 70B Instruct, Q4_K_M |
| Token generation | ~19 tokens/second |
| VRAM used by the model | ~42 GB (~21 GB per card, near-symmetric) |
| VRAM budget | 48 GB total (24 GB per card) |
| Cold start (Ollama load) | ~15-20 seconds |
| Warm start (already resident) | 0 seconds |
| Idle power per card | ~20-25 W |
| Sustained inference power per card | ~280 W |

Honest constraint that follows from those numbers: **~42 GB of a 48 GB budget leaves about 6 GB free, which is not enough to hot-load a second large model.** Small models (7B and under) can coexist alongside the 70B. A second *large* model means one of: reducing the 70B's `num_ctx` to free VRAM, accepting a swap (roughly 30 seconds to unload and load), or more/bigger cards.

`OLLAMA_KEEP_ALIVE=-1` keeps the 70B resident indefinitely once loaded, so the 15-20s cold start is a once-per-boot cost, not a per-request one.

## Access control

There are two independent layers, at opposite ends of the tunnel.

**At the public edge (EC2)** — verified live:

- **TLS** — valid Let's Encrypt certificate. (Renewal is presumably the standard certbot timer; the cert is current, but the timer's state was not confirmed on the instance.)
- **HTTP basic auth** with realm exactly `343 Guilty Spark`, implemented in nginx as `auth_basic` plus `auth_basic_user_file /etc/nginx/.htpasswd`, declared at `server` level so every route — the static UI *and* the inference proxy — inherits it.

**At the inference box (the VM)** — verified in the guest:

- **ufw**, active and enabled, default `deny (incoming)`, with the inference port allowed *only* on the tunnel interface:

  ```
  11434/tcp on wg0   ALLOW IN   Anywhere
  ```

- This rule is **required**: nginx opens a new inbound connection over the tunnel, which default-deny would otherwise drop. It is also the *only* thing confining Ollama to the tunnel, because `OLLAMA_HOST` is `0.0.0.0` — the daemon listens on every interface the VM has. The `on wg0` scope is doing the work a bind address would normally do.

  If you reproduce this, prefer `OLLAMA_HOST=<tunnel-ip>:11434` so the kernel refuses off-tunnel connections too, and treat the firewall rule as the second layer rather than the only one. Note that binding to a WireGuard address makes the Ollama unit depend on the tunnel being up first, so order the units accordingly and test with a reboot.

- Note the tunnel itself needs **no** inbound rule: the VM dials out to EC2, so return traffic is already permitted by the default outgoing policy.

Two clarifications, because both are common sources of confusion:

- This is **not** `.htaccess`. nginx has no `.htaccess` support at all — that is an Apache mechanism. The credentials live in an htpasswd-format file referenced explicitly by an nginx directive, and there is no per-directory override file anywhere in the stack.
- The **bearer-token injection is not active.** It belonged to the retired vLLM path, where nginx added an `Authorization` header server-side so the token never reached the browser. That behavior lives only in the parked `nginx-configs/nginx.conf` template. The live Ollama backend does not require a token.

## Threat model

What this protects against:

- Public exposure of the inference endpoint — reachable only through the tunnel, enforced by an interface-scoped firewall rule (`11434/tcp on wg0`). Note that this is the firewall's doing, not the bind address': the live Ollama listens on `0.0.0.0`. See Access control above.
- Casual internet traffic hitting the UI — basic auth gate on the public routes.
- Traffic capture in transit — TLS on the public hop, WireGuard on the private hop.
- Inbound exposure of the home network — the VM dials out; nothing is forwarded in.

What this does *not* protect against:

- Anyone holding htpasswd credentials (treat them like SSH keys).
- A compromised EC2 instance — it is the trusted entry point to the tunnel.
- **Anything already on the WireGuard mesh.** There is no auth on the Ollama endpoint itself; any peer can hit it unauthenticated. The mesh is currently just EC2 ↔ VM, which is why this is acceptable. Adding peers means revisiting it.
- **A firewall that stops doing its job.** Because the live Ollama binds `0.0.0.0`, the interface-scoped ufw rule is the single control keeping it off the VM's other networks. `ufw disable`, a flushed ruleset, or an unscoped `allow 11434/tcp` would expose an unauthenticated 70B endpoint to anything that can route to the VM. One mistake, no second layer — which is the argument for binding to the tunnel address as well.
- XSS or browser-side compromise of an authenticated session.
- Abuse by authenticated users — no per-user rate limiting.

For a single-user or trusted-group setup this is fine. For anything broader, swap basic auth for OAuth2 Proxy, add per-user rate limiting at nginx, and put authentication in front of Ollama.

One known piece of dead weight: the live nginx config still carries a `/v1/` catch-all block pointing at the retired vLLM port 8000. Nothing hits it — the UI only calls `/v1/llama/` — but it should either be rewired or removed.

## Repository layout

Files are labelled by generation, because both generations are kept on purpose.

```
.
├── README.md                                 ← you are here
├── ARCHITECTURE.md                           ← deep internal handoff doc
│
│   CURRENT — the live Ollama generation
├── unit-files/
│   ├── ollama-override.conf                  ← systemd drop-in: OLLAMA_HOST,
│   │                                            KEEP_ALIVE=-1, NUM_PARALLEL=2
│   └── ollama-preload.service                ← warms the model at boot
│                                                (template; not yet installed)
├── models/
│   └── Modelfile.llama3.3-70b-fullgpu        ← num_gpu 999, num_ctx 4096
├── nginx-configs/
│   └── spark-ollama.conf                     ← live server block: TLS, basic
│                                                auth, /v1/llama/ → :11434
├── scripts/
│   └── inference-baseline.sh                 ← reproduces the perf numbers above
│
│   PARKED — the vLLM generation, retained as templates
├── scripts/vllm-serve.bash                   ← launcher for kill-restart swaps
├── unit-files/vllm.service                   ← systemd unit
├── nginx-configs/nginx.conf                  ← includes the bearer-token
│                                                injection and the :8000 proxy
│
│   OTHER
├── server/index.html                         ← see the drift note below
└── wireguard-configs/                        ← empty; the real wg0.conf files
                                                live on the hosts, uncommitted
```

**Drift note on `server/index.html`:** the copy in this repo is the **old** UI. It hardcodes `const ENDPOINT = "/v1/chat/completions"` and has no model selector. The UI actually serving at `/var/www/spark/index.html` on EC2 is a later revision with a `MODELS` dict targeting `/v1/llama`:

```javascript
const MODELS = {
  "/v1/llama": "llama3.3-70b-fullgpu:latest",
};
```

Do not treat the committed file as current. Syncing the live UI back into the repo is an open item.

## Getting started

The order matters. Each step assumes the previous one works.

### Prerequisites

- A domain you control (we'll use `343-guilty-spark.io` in examples)
- An AWS account with the CLI configured
- A Linux box with one or more NVIDIA GPUs, enough VRAM for your target model, and IOMMU available in BIOS if you want the passthrough-VM layout
- Comfort with a terminal

You do **not** need a 70B-capable machine to build this shape. The EC2 + WireGuard + nginx half is identical whatever you serve; only the VRAM budget and model tag change.

### Step 1: EC2 instance

Launch a small Ubuntu 24.04 instance (a `t3.small` is plenty — it never touches the model), allocate an Elastic IP, and associate it.

Security group inbound rules:

| Type | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| SSH | TCP | 22 | your IP | bootstrap only — drop after setup |
| HTTP | TCP | 80 | 0.0.0.0/0 | Let's Encrypt HTTP-01 challenge |
| HTTPS | TCP | 443 | 0.0.0.0/0 | the UI |
| Custom UDP | UDP | 51820 | 0.0.0.0/0 | WireGuard handshake |

CLI shortcut if you forgot any:

```bash
aws ec2 authorize-security-group-ingress --group-id sg-xxx --protocol tcp --port 80  --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id sg-xxx --protocol tcp --port 443 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id sg-xxx --protocol udp --port 51820 --cidr 0.0.0.0/0
```

### Step 2: DNS

In whichever registrar holds your nameservers, add:

| Type | Host | Value |
|---|---|---|
| A | `@`   | your EIP |
| A | `www` | your EIP |

Wait until both `dig +short 343-guilty-spark.io` and `dig +short 343-guilty-spark.io @1.1.1.1` return your EIP before moving on.

### Step 3: WireGuard

On both EC2 and the inference box:

```bash
sudo apt update && sudo apt install -y wireguard
umask 077
wg genkey | sudo tee /etc/wireguard/privatekey | wg pubkey | sudo tee /etc/wireguard/publickey
```

The private key never leaves the host that generated it. Only public keys cross between machines.

**EC2 — `/etc/wireguard/wg0.conf`:**

```ini
[Interface]
Address    = 10.100.0.1/24
ListenPort = 51820
PrivateKey = <EC2_PRIVATE_KEY>

[Peer]
PublicKey  = <SANDBOX_PUBLIC_KEY>
AllowedIPs = 10.100.0.2/32
```

**Inference sandbox — `/etc/wireguard/wg0.conf`:**

```ini
[Interface]
Address    = 10.100.0.2/24
PrivateKey = <SANDBOX_PRIVATE_KEY>

[Peer]
PublicKey           = <EC2_PUBLIC_KEY>
Endpoint            = <EC2_EIP>:51820
AllowedIPs          = 10.100.0.0/24
PersistentKeepalive = 25
```

Bring it up on both:

```bash
sudo systemctl enable --now wg-quick@wg0
```

Verify from the sandbox:

```bash
ping -c3 10.100.0.1
sudo wg                 # both ends should show a recent handshake and nonzero transfer
```

If `sudo wg` on EC2 shows no `peer:` line, the `[Peer]` block is missing — re-check the config.

### Step 4 (optional): GPU passthrough VM

If you want the isolation this build uses — inference in a VM, host kept clean — bind the GPUs to `vfio-pci` on the host and attach them to a libvirt guest. ID-based binding is the trick worth copying: both cards share a vendor:device ID, so one line claims both at boot with no per-slot configuration.

`/etc/modprobe.d/vfio.conf` on the host:

```
options vfio-pci ids=10de:2204,10de:1aef
softdep nvidia pre: vfio-pci
softdep nouveau pre: vfio-pci
```

(`10de:2204` is the RTX 3090 GPU function, `10de:1aef` its audio function. Find yours with `lspci -nn`. Each card needs **both** functions passed through, so a dual-GPU guest takes four `<hostdev>` blocks.)

Confirm with `lspci -nnk -s <bus>` that `vfio-pci` is the kernel driver in use. After this, `nvidia-smi` on the host will fail — that is the expected end state, not a problem to fix. Full detail in [`ARCHITECTURE.md` §2.3-2.4](ARCHITECTURE.md).

Skipping this step and running Ollama directly on the metal works fine; everything downstream is unchanged.

### Step 5: Ollama

On the inference box (or inside the VM):

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.3:70b-instruct-q4_K_M
```

Force full GPU offload with a Modelfile — this is what prevents CPU spillover and the throughput collapse that comes with it:

```bash
ollama create llama3.3-70b-fullgpu -f models/Modelfile.llama3.3-70b-fullgpu
```

Bind Ollama to the tunnel address only, and keep the model resident. Install `unit-files/ollama-override.conf` as a systemd drop-in:

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo cp unit-files/ollama-override.conf /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

It sets:

```ini
[Service]
Environment="OLLAMA_HOST=10.100.0.2:11434"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_NUM_PARALLEL=2"
```

Binding to `10.100.0.2` rather than `0.0.0.0` is the recommended choice: the kernel then refuses the connection on any other interface, so the endpoint is unreachable except over WireGuard even if the firewall is misconfigured.

Two caveats if you follow this. First, it creates a real startup-ordering dependency — `bind()` fails with `EADDRNOTAVAIL` if `wg0` has not configured the address yet, so the Ollama unit needs `After=`/`Wants=wg-quick@wg0.service`, and you should test with a reboot rather than a `systemctl restart` (a restart succeeds while `wg0` is already up and hides the race). Second, be aware the live deployment this README documents does **not** currently do this — it binds `0.0.0.0` and leans on the ufw rule alone. Doing both is the belt-and-braces version.

Verify from EC2:

```bash
curl -s http://10.100.0.2:11434/v1/models | jq .
```

Optionally install `unit-files/ollama-preload.service` to warm the model at boot and eliminate the 15-20s first-query latency.

### Step 6: nginx + TLS

On EC2:

```bash
sudo apt install -y nginx apache2-utils certbot python3-certbot-nginx
sudo mkdir -p /var/www/spark

# basic auth credentials
sudo htpasswd -c /etc/nginx/.htpasswd youruser
sudo chmod 640 /etc/nginx/.htpasswd
sudo chown root:www-data /etc/nginx/.htpasswd

# install the server block
sudo cp nginx-configs/spark-ollama.conf /etc/nginx/sites-available/343-guilty-spark
sudo ln -s /etc/nginx/sites-available/343-guilty-spark /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# initial run is plain HTTP — certbot will rewrite it
sudo nginx -t && sudo systemctl reload nginx

# get a cert
sudo certbot --nginx -d 343-guilty-spark.io -d www.343-guilty-spark.io
```

The proxy block that does the work:

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

`proxy_buffering off` is not optional — without it nginx batches the SSE chunks and streaming arrives in jerks. The long timeouts cover slow generations on a 70B.

### Step 7: deploy the UI

```bash
scp server/index.html ubuntu@343-guilty-spark.io:/tmp/
ssh ubuntu@343-guilty-spark.io 'sudo mv /tmp/index.html /var/www/spark/'
```

Point the UI's `MODELS` dict at your model tag before deploying. (Reminder: the committed `server/index.html` is the older revision — see the drift note above.)

Open `https://343-guilty-spark.io`, authenticate at the `343 Guilty Spark` prompt, type something, watch tokens stream.

## Operations

### Adding a user

```bash
sudo htpasswd /etc/nginx/.htpasswd newuser
# DO NOT use -c again — it wipes the file
```

No nginx reload needed.

### Swapping the served model

```bash
# on the inference box
ollama pull <new-model-tag>

cat > /tmp/Modelfile <<'EOF'
FROM <new-model-tag>
PARAMETER num_gpu 999
PARAMETER num_ctx 4096
EOF
ollama create <new-alias> -f /tmp/Modelfile
```

Then update the `MODELS` dict in `/var/www/spark/index.html` on EC2 to point at the new alias. **No nginx changes needed** — the model tag travels in the request body, so one proxy block serves any number of models.

Adding a *small* second model alongside the 70B works within the ~6 GB of headroom. Adding a second large one does not; see [Performance baseline](#performance-baseline).

### Restarting the stack

```bash
# host: cycle the whole VM
sudo virsh shutdown agent-sandbox-3090   # wait for shutoff
sudo virsh start agent-sandbox-3090

# VM: just Ollama
sudo systemctl restart ollama
```

Either way the model reloads on the next query (~15-20s) unless the preload service is installed.

### Logs

```bash
# EC2
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log

# inference box
sudo journalctl -u ollama -f
```

### Cert renewal

Certbot installs a systemd timer automatically. Verify:

```bash
sudo systemctl list-timers | grep certbot
sudo certbot renew --dry-run
```

## Troubleshooting

**Browser hangs on page load.** Check that DNS resolves to the EIP and that the EC2 security group allows 443.

**Basic auth doesn't prompt.** `sudo nginx -T | grep auth_basic` should show both `auth_basic` and `auth_basic_user_file`. If empty, the config didn't reload, or you edited a file that isn't reachable from `sites-enabled`. Note that dropping an `.htaccess` file anywhere will do nothing — nginx does not read them.

**Tunnel shows `0 B received` on EC2.** The peer's public key isn't configured on EC2's side. `sudo wg` on EC2 must show a `peer:` block matching the sandbox's public key.

**502 or `No route to host` from `/v1/llama/`.** Either the tunnel is down (no recent handshake in `sudo wg`), or Ollama isn't bound where you think — `sudo ss -tlnp | grep 11434` should show it on `10.100.0.2`, not `127.0.0.1`.

**Ollama keeps dying, or restarts in a loop, silently.** Check for a stray foreground `ollama serve` holding port 11434 while systemd tries to start its own: `sudo ss -tlnp | grep 11434`, then `sudo journalctl -u ollama`. The resulting crash-loop produces no user-visible error, only failures at the proxy.

**Throughput is far below expectations.** Confirm the model is fully on the GPUs and hasn't spilled to CPU — that is what `num_gpu 999` in the Modelfile is for. `nvidia-smi` inside the VM should show the model's VRAM split across both cards.

**Streaming feels chunky or stalls.** `proxy_buffering` must be `off` in the `/v1/llama/` location.

**`nvidia-smi` fails on the host.** Working as intended, if you're using the passthrough layout. `vfio-pci` owns the cards. Run it inside the VM.

**Cert renewal fails with a timeout.** Same root cause as initial issuance — port 80 must be reachable from the public internet for HTTP-01 validation.

## Roadmap

- [ ] Install `ollama-preload.service` so the first query after a boot isn't a 15-20s wait
- [ ] Verify VM autostart on the host (`virsh list --autostart`)
- [ ] Sync the live UI back into `server/index.html` (repo copy has drifted)
- [ ] Remove or rewire the dead `/v1/` catch-all still pointing at vLLM's port 8000
- [ ] ASUS ProArt X870E-Creator upgrade for ×8/×8 bifurcation — the gate on unparking vLLM
- [ ] Rerun `scripts/inference-baseline.sh` after that upgrade to quantify the gain
- [ ] Tool calling end-to-end
- [ ] Conversation persistence (localStorage, then server-side)
- [ ] Per-user rate limiting at nginx
- [ ] OAuth2 Proxy in place of basic auth
- [ ] Authentication in front of Ollama, if the WireGuard mesh ever grows past two peers
- [ ] Audit logging of all `/v1/` requests to a separate pipeline

## License

MIT or whatever you prefer — adjust before publishing.

## Acknowledgements

The naming and stylistic cues for 343 Guilty Spark are from Bungie's *Halo*. The UI's persona framing owes a debt to TARS in Christopher Nolan's *Interstellar*. No affiliation, just affection.
