# 343 Guilty Spark

A minimal self-hosted chat UI backed by a private vLLM inference endpoint, fronted by nginx on AWS, and connected over a WireGuard tunnel. Single-page TARS-style terminal interface, streaming completions, basic auth, and an OpenAI-compatible API surface.

> "I am the Monitor of Installation 04. I am 343 Guilty Spark."

## Why

Most "host your own LLM" tutorials either expose your GPU to the public internet or leave you with a setup that only works on `localhost`. This is the middle path: the GPU stays on your local network, the public surface area is a small EC2 instance you control, and the link between them is a single UDP tunnel. No third-party tunneling service, no API keys leaking through reverse proxies, no GPU sitting on a public IP.

## Architecture

```
                    ┌──────────────────────────────────────┐
                    │  User on laptop or phone             │
                    │  https://343-guilty-spark.io         │
                    └──────────────┬───────────────────────┘
                                   │ HTTPS (basic auth)
                                   ▼
              ┌────────────────────────────────────────────┐
              │  AWS EC2 (Elastic IP, public)              │
              │                                            │
              │   nginx                                    │
              │   ├─ TLS via Let's Encrypt                 │
              │   ├─ Basic auth on all routes              │
              │   ├─ /         → static index.html         │
              │   └─ /v1/*     → injects bearer token,     │
              │                  proxies upstream          │
              │                                            │
              │   wg0 = 10.100.0.1                         │
              └──────────────┬─────────────────────────────┘
                             │ WireGuard
                             │ UDP/51820
                             │ (EC2 listens, VM dials out)
                             ▼
              ┌────────────────────────────────────────────┐
              │  Local GPU sandbox (behind home NAT)       │
              │                                            │
              │   wg0 = 10.100.0.2                         │
              │                                            │
              │   vLLM                                     │
              │   ├─ Bound to 10.100.0.2:8000              │
              │   ├─ Bearer token required                 │
              │   ├─ OpenAI-compatible API                 │
              │   └─ Currently: Qwen2.5-7B-Instruct        │
              │                                            │
              │   ufw: only allow tcp/8000 on wg0          │
              └────────────────────────────────────────────┘
```

The local sandbox initiates the tunnel outbound — no port forwarding on your home router, no inbound rules on the GPU host, no exposure of the inference endpoint to anything outside the tunnel.

## Components

| Component | Where | Purpose |
|---|---|---|
| EC2 instance | AWS | Stable public IP, TLS termination, static UI hosting, reverse proxy |
| nginx | EC2 | TLS, basic auth, static files, bearer token injection |
| Let's Encrypt | EC2 | Free certs via certbot, auto-renewing |
| WireGuard | Both | Encrypted tunnel between EC2 and the GPU sandbox |
| vLLM | Local sandbox | OpenAI-compatible inference server |
| Qwen2.5-7B-Instruct | Local sandbox | Default model — swappable |
| `index.html` | EC2 | Single-file streaming chat UI, mobile-friendly |

## Threat model

What this protects against:

- Public exposure of the inference endpoint (it's only reachable through the tunnel)
- Casual internet traffic hitting the UI (basic auth gate)
- Bearer token leaking to the browser (nginx injects it server-side)
- Token capture in transit (TLS on the public hop, WireGuard on the private hop)

What this does *not* protect against:

- Anyone with htpasswd credentials and basic auth (treat them like SSH keys)
- A compromised EC2 instance — the bearer token lives in the nginx config
- XSS or browser-side compromise of an authenticated session
- Abuse by authenticated users (no per-user rate limiting in this POC)

For a single-user or trusted-group setup this is fine. For anything broader, swap basic auth for OAuth2 Proxy and add per-user rate limiting at nginx.

## Repository layout

```
.
├── README.md                  ← you are here
├── nginx/
│   └── 343-guilty-spark.conf  ← server block reference
├── wireguard/
│   ├── ec2.wg0.conf.example   ← server-side WireGuard config
│   └── local.wg0.conf.example ← client-side WireGuard config
├── ui/
│   └── index.html             ← single-file chat UI
└── scripts/
    └── vllm-serve             ← launcher for kill-restart model swaps
```

## Getting started

The order matters. Each step assumes the previous one works.

### Prerequisites

- A domain you control (we'll use `343-guilty-spark.io` in examples)
- An AWS account with CLI configured
- A local machine with a GPU and Ubuntu/Debian (anything that runs vLLM)
- Comfort with a terminal

### Step 1: EC2 instance

Launch a `t3.small` Ubuntu 24.04 instance, allocate an Elastic IP, and associate it.

Security group inbound rules:

| Type | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| SSH | TCP | 22 | your IP | bootstrap only — drop after setup |
| HTTP | TCP | 80 | 0.0.0.0/0 | Let's Encrypt HTTP-01 challenge |
| HTTPS | TCP | 443 | 0.0.0.0/0 | the UI |
| Custom UDP | UDP | 51820 | 0.0.0.0/0 | WireGuard handshake |

CLI shortcut to add 80 and 443 if you forgot:

```bash
aws ec2 authorize-security-group-ingress --group-id sg-xxx --protocol tcp --port 80  --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id sg-xxx --protocol tcp --port 443 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id sg-xxx --protocol udp --port 51820 --cidr 0.0.0.0/0
```

### Step 2: DNS

In your registrar (Namecheap, Route53, Cloudflare — whichever holds your nameservers), add:

| Type | Host | Value |
|---|---|---|
| A | `@`   | your EIP |
| A | `www` | your EIP |

Wait until both `dig +short 343-guilty-spark.io` and `dig +short 343-guilty-spark.io @1.1.1.1` return your EIP before moving on.

### Step 3: WireGuard

On both EC2 and the local sandbox:

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
PublicKey  = <LOCAL_VM_PUBLIC_KEY>
AllowedIPs = 10.100.0.2/32
```

**Local sandbox — `/etc/wireguard/wg0.conf`:**

```ini
[Interface]
Address    = 10.100.0.2/24
PrivateKey = <LOCAL_VM_PRIVATE_KEY>

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

Verify from the local sandbox:

```bash
ping -c3 10.100.0.1
sudo wg                 # both ends should show a recent handshake and nonzero transfer
```

If `sudo wg` on EC2 shows no `peer:` line, the `[Peer]` block is missing — re-check the config.

### Step 4: vLLM

On the local sandbox:

```bash
# generate a token first so you can paste it into nginx later
mkdir -p ~/.config/spark
openssl rand -hex 24 | sed 's/^/sk-/' > ~/.config/spark/api_key
chmod 600 ~/.config/spark/api_key

# install vllm (use uv, pip, or your preferred tool)
pip install vllm

# launcher script
cp scripts/vllm-serve ~/bin/vllm-serve
chmod +x ~/bin/vllm-serve

# fire it up
export VLLM_API_KEY="$(cat ~/.config/spark/api_key)"
vllm-serve Qwen/Qwen2.5-7B-Instruct
```

Lock down the inference port to the tunnel only:

```bash
sudo ufw default deny incoming
sudo ufw allow ssh
sudo ufw allow in on wg0 to any port 8000 proto tcp
sudo ufw enable
```

Verify from EC2:

```bash
nc -zv 10.100.0.2 8000
curl http://10.100.0.2:8000/v1/models -H "Authorization: Bearer $(cat /path/to/key)"
```

### Step 5: nginx + TLS

On EC2:

```bash
sudo apt install -y nginx apache2-utils certbot python3-certbot-nginx
sudo mkdir -p /var/www/spark

# basic auth credentials
sudo htpasswd -c /etc/nginx/.htpasswd youruser
sudo chmod 640 /etc/nginx/.htpasswd
sudo chown root:www-data /etc/nginx/.htpasswd

# install the server block
sudo cp nginx/343-guilty-spark.conf /etc/nginx/sites-available/343-guilty-spark
sudo ln -s /etc/nginx/sites-available/343-guilty-spark /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# initial run is plain HTTP — certbot will rewrite it
sudo nginx -t && sudo systemctl reload nginx

# get a cert
sudo certbot --nginx -d 343-guilty-spark.io -d www.343-guilty-spark.io
```

Edit `/etc/nginx/sites-available/343-guilty-spark` and replace the placeholder bearer token in the `proxy_set_header Authorization` line with the value from `~/.config/spark/api_key` on the local sandbox. Reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### Step 6: deploy the UI

From your laptop:

```bash
scp ui/index.html ec2-user@343-guilty-spark.io:/tmp/
ssh ec2-user@343-guilty-spark.io 'sudo mv /tmp/index.html /var/www/spark/'
```

Open `https://343-guilty-spark.io` in a browser. Authenticate with the basic auth credentials. Type something. Watch tokens stream.

## Operations

### Adding a user

```bash
sudo htpasswd /etc/nginx/.htpasswd newuser
# DO NOT use -c again — it wipes the file
```

No nginx reload needed — `.htpasswd` is read on every request.

### Swapping models

```bash
vllm-serve meta-llama/Llama-3.1-8B-Instruct
```

The launcher kills any running vLLM process and starts the new one. Bookmark a couple of favorites:

- `Qwen/Qwen2.5-7B-Instruct` — general chat, tool calling
- `Qwen/Qwen2.5-Coder-7B-Instruct` — code
- `meta-llama/Llama-3.1-8B-Instruct` — alternative general
- `mistralai/Mistral-7B-Instruct-v0.3` — lightweight

A 24GB GPU (e.g. RTX 3090) comfortably runs ~7-8B models at FP16 with reasonable context. AWQ/GPTQ quantization lets you push to 14B+ at the cost of quality.

### Logs

```bash
# nginx access + error
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log

# vLLM (if running under tmux/systemd)
sudo journalctl -u vllm -f
```

### Cert renewal

Certbot installs a systemd timer automatically. Verify:

```bash
sudo systemctl list-timers | grep certbot
sudo certbot renew --dry-run
```

## Troubleshooting

**Browser hangs on the page load.** Check that DNS resolves to the EIP and that the EC2 security group allows 443.

**Basic auth doesn't prompt.** `sudo nginx -T | grep auth_basic` should show your directives. If empty, the config didn't reload, or you edited a file outside `sites-enabled`.

**Tunnel shows `0 B received` on EC2.** The peer's public key isn't configured on EC2's side. `sudo wg` on EC2 must show a `peer:` block matching the local sandbox's public key.

**`No route to host` on `nc 10.100.0.2 8000`.** Either the tunnel isn't up (no recent handshake in `sudo wg`), vLLM isn't bound to `10.100.0.2` (`ss -tlnp | grep 8000` should show it), or ufw is rejecting (`sudo ufw status verbose`).

**Streaming feels chunky or stalls.** nginx `proxy_buffering` must be `off` in the `/v1/` location. Without it, nginx batches SSE chunks and the UI updates in jerks.

**Cert renewal fails with timeout.** Same root cause as initial issuance — port 80 must be reachable from the public internet for HTTP-01 validation.

## Roadmap

- [ ] Tool calling end-to-end (vLLM supports it; UI doesn't yet wire it)
- [ ] Conversation persistence (localStorage, then server-side)
- [ ] Model dropdown in the UI
- [ ] Per-user rate limiting at nginx
- [ ] OAuth2 Proxy in place of basic auth
- [ ] Skill loading with cryptographic provenance (TCIL pattern)
- [ ] Audit logging of all `/v1/` requests to a separate pipeline

## License

MIT or whatever you prefer — adjust before publishing.

## Acknowledgements

The "humor setting 75%" framing is from TARS in Christopher Nolan's *Interstellar*. The naming and stylistic cues for 343 Guilty Spark are from Bungie's *Halo*. No affiliation, just affection.
