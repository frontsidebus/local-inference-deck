# GPU modes and the gaming VM

The hypervisor can lend Walter's second GPU to a gaming VM (`linux-gaming`, Bazzite), streamed to the workstation with Sunshine (in the VM) and Moonlight (on the host). Three programs run on the hypervisor:

| File | Runs as | What it does |
|---|---|---|
| `gpu-mode` (with `game-mode` and `ai-mode` symlinks) | the operator (group `libvirt`), no sudo | switches the GPUs between the two modes below |
| `gaming-setup.sh` | root (`sudo`), plan by default, `--apply` to change | one-time host prep: guard firewall, NAT network, disk, libvirt hook, VM definitions, the exporter's firewall |
| `gaming-exporter` (`gaming-exporter.service`) | the operator, a `systemd --user` unit | telemetry for Walter's Prometheus (see [Telemetry](#telemetry)) |

All three read their site values from `~/.config/deck/site.env` (keys below). None of them sources it; each line is parsed.

## The two modes

| | **AI mode** (default, after every boot) | **Game mode** (split) |
|---|---|---|
| Walter's GPUs | GPU0 and GPU1 | GPU0 only |
| Walter's RAM | `WALTER_MEM_GIB` (80) | `WALTER_SPLIT_MEM_GIB` (64) |
| Walter's CPUs | all, unpinned | pinned to `GAMING_HOST_CPUS` |
| Models | everything (`coder` on CUDA 0, `coder-fast` on CUDA 1, split models across both) | `coder` only. A `coder-fast` request fails; a two-GPU model (`big`, `vision`, `hermes`, ...) unloads `coder` and then fails, and the next `coder` request reloads it (~20 s) |
| Agent judge | running | paused (`judge-review.path/.timer`, `judge-runaway-watch`) |
| Gaming VM | off | on GPU1, 24 GiB, `GAMING_GUEST_CPUS` (pinned, the host and Walter stay off them) |

Each switch restarts Walter once (chat, API and telemetry are down for a few minutes).

## Switching

```bash
~/bin/game-mode      # AI -> game: pause the judge, restart Walter on GPU0, start linux-gaming on GPU1
~/bin/ai-mode        # game -> AI: shut linux-gaming down cleanly, Walter back to both GPUs, resume the judge
~/bin/gpu-mode status   # read-only: mode, Walter's GPUs and RAM, gaming VM, GPU1 driver, judge, host CPUs
~/bin/gpu-mode check    # read-only: game mode's pre-checks
```

`--yes` skips the confirmation. Every step is checked. On a failure the script stops and says what state it left, and **`ai-mode` recovers from any partial state**: when in doubt, run `ai-mode`.

**game-mode** refuses to start unless:
- it isn't already in game mode (no state file in `~/.local/state/gpu-mode`);
- `linux-gaming` is defined as the setup or final variant and is shut off;
- Walter is running, and its live and saved config both match the baseline (both GPUs, full RAM);
- GPU1 is on `vfio-pci` and `gaming-guard.service` is active;
- no eval run or GPU guard is running;
- it's outside Walter's backup window (03:25–04:45 UTC);
- the host has at least 16 GiB `MemAvailable`;
- Walter is reachable and `coder` is loaded.

Then it:
1. pauses the judge, waiting up to 15 min for a running review;
2. waits up to 10 min for Walter to go idle, then shuts it down;
3. removes GPU1 from Walter's config and lowers its RAM;
4. starts Walter, pins it to `GAMING_HOST_CPUS`, and checks that it sees exactly GPU0 and that `coder` answers;
5. starts `gaming-nat` and `linux-gaming`.

**ai-mode**, from any state:
1. shuts `linux-gaming` down cleanly, asking before a forced stop;
2. checks that GPU1 is on `vfio-pci`;
3. gives Walter GPU1 and its full RAM back, then checks against the baseline;
4. starts Walter and checks the CUDA order and that `coder` and `coder-fast` answer;
5. stops `gaming-nat`, resumes the judge, and lists any "No review" placeholders written while it was paused;
6. warns if the host CPUs were not given back.

Rules:
- **Start the gaming VM only through `game-mode`.** Starting it from virt-manager in AI mode is refused by the hook ("... may run on the gaming cores"): Walter still holds GPU1 and every core.
- **Shutting the guest down from inside it is fine**, but you stay in game mode. Start it again with `virsh -c qemu:///system start linux-gaming` (or virt-manager), or run `ai-mode`.
- Don't use **Force Off** (`virsh destroy`) unless the guest is hung.

## What protects Walter and the host

- **The libvirt qemu hook** (`/etc/libvirt/hooks/qemu.d/10-linux-gaming`) acts only on `linux-gaming`:
  - **Strict allow-list on its XML:** GPU1's functions only; raw disks under `GAMING_VMS_DIR`, plus the one whole drive named in `GAMING_DATA_DISK` (if set); `gaming-nat` with `clean-traffic`; VNC on a unix socket only; no qemu command-line passthrough; no other host files or devices.
  - **The data disk:** the hook refuses any block device except `GAMING_DATA_DISK`, matched by its exact `/dev/disk/by-id` path. It also refuses that one if the host has anything on it mounted, uses it as swap, or has LVM, RAID or device-mapper on it. `gaming-setup.sh` checks the same, and that no other VM uses the drive, before it writes the hook or the VM definition.
  - **Before start:** GPU1 must be on `vfio-pci` and the guard table loaded. With GPU1, every other VM's threads must already be on `GAMING_HOST_CPUS`.
  - **While it runs**, host processes (`system.slice`, `user.slice`, `init.scope`) are moved onto `GAMING_HOST_CPUS`. On release every online CPU is written back first (systemd 255 keeps the last cpuset when `AllowedCPUs` is only cleared), then the property is cleared.
  - It guards against mistakes. It is not a security boundary: group `libvirt` is root-equivalent.
- **The `gaming_guard` nft table:** the guest on `virbr-gaming` reaches the public internet (libvirt NAT) and its own network's DHCP/DNS, nothing else: no host address, LAN, Walter, other VM networks or docker, and no IPv6. Moonlight works because the host opens the connections. `gaming-guard.service` loads it at boot; a libvirt network hook refuses to start `gaming-nat` without it.
- **Network and autostart:** `gaming-nat` and `linux-gaming` never autostart.

## One-time setup

1. **Site values.** All optional (defaults in [site.env.example](../../site.env.example), section "hypervisor: GPU modes"). Set at least `WALTER_DOMAIN` if Walter's libvirt domain is not called `walter`. The two scripts read the same keys: change them together and re-run `gaming-setup.sh hook` and `vm-setup`/`vm-final` afterwards.
2. **Baseline and hostdev snippets** (once, in AI mode, as the operator). `gpu-mode` restores Walter from these:
   ```bash
   install -d -m 700 ~/vm-backups && cd ~/vm-backups
   virsh -c qemu:///system dumpxml --inactive <walter-domain> > <walter-domain>-baseline.xml
   # GPU1's two functions as attachable snippets (hostdev-gpu1.xml = VGA .0, hostdev-gpu1a.xml = audio .1)
   python3 -I - <walter-domain>-baseline.xml 0000:03:00.0 0000:03:00.1 <<'EOF'
   import sys, xml.etree.ElementTree as ET
   want = sys.argv[2:]
   for h in ET.parse(sys.argv[1]).getroot().iter("hostdev"):
       a = h.find("source/address")
       bdf = "%04x:%02x:%02x.%x" % tuple(int(a.get(k), 16) for k in ("domain", "bus", "slot", "function"))
       if bdf in want:
           ET.ElementTree(h).write("hostdev-gpu1%s.xml" % ("" if bdf == want[0] else "a"))
   EOF
   ```
   Use the addresses from `GAMING_GPU_PCI`. Redo this after any change to Walter's XML (see [../README.md](../README.md#hardware-changes-gpus-risers-slots)).
3. **Install the switch scripts:** `ln -s <checkout>/hypervisor/gaming/{gpu-mode,game-mode,ai-mode} ~/bin/`. If `~/bin` isn't on your `PATH`, call them as `~/bin/game-mode`.
4. **Host prep:**
   - Download the Bazzite ISO (`bazzite-nvidia-open-stable-live-amd64.iso`) with its `-CHECKSUM`, `.sig` and `bazzite-cosign.pub` into `~/Downloads/gaming/`.
   - Run `sudo ./gaming-setup.sh` (plan), then `sudo ./gaming-setup.sh --apply`, which asks before each step.
   - The steps, in order:
     - **guard:** the nft table above;
     - **network:** `gaming-nat`;
     - **storage:** the preallocated 160 GiB raw disk, and the ISO, copied and verified (sha256 and the pinned cosign key);
     - **packages:** `vainfo`;
     - **guests:** a longer libvirt-guests shutdown timeout;
     - **hook:** the qemu hook;
     - **vm:** the install variant;
     - **exporter:** the nft guard for the telemetry exporter (see [Telemetry](#telemetry)).
   - Afterwards run `sudo systemctl reload libvirtd`, so libvirt picks up the hooks. Each step prints its undo, and backups go to `/root/gaming-setup-<time>/`.
5. **Install Bazzite:** start the install variant (no GPU, VNC console, 16 GiB, 8 vCPUs) while Walter runs normally, install, enable sshd and shut it down.
6. **First boot with the GPU:**
   - `sudo ./gaming-setup.sh --apply vm-setup` (GPU1 plus a VNC console), then `~/bin/game-mode`.
   - Set up the guest as below, then `~/bin/ai-mode`.
   - Then `sudo ./gaming-setup.sh --apply vm-final` (GPU1 only, no virtual GPU, no console). **Play on the final variant**: on the setup variant the virtual GPU is primary and the desktop renders in software, so the stream lags.

### Guest setup (Bazzite)

- **Image:** `bazzite-nvidia-open:stable`. Check the GPU with `nvidia-smi`.
- **Display:** a dummy plug (or EDID emulator) in GPU1 gives the guest a display to render to. The guest only offers the modes in the plug's EDID: KWin custom modes are rejected on this setup (see Troubleshooting).
- **Automatic login, so Sunshine runs without a console:**
  - `/etc/plasmalogin.conf.d/50-autologin.conf` with `[Autologin]`, `User=<guest user>`, `Session=plasma.desktop`;
  - no lock screen: `kwriteconfig6 --file kscreenlockerrc --group Daemon --key Autolock false`, and the same with `--key LockOnResume false`.
- **Sunshine** (Flatpak `dev.lizardbyte.app.Sunshine`):
  - **Config:** `~/.var/app/dev.lizardbyte.app.Sunshine/config/sunshine/sunshine.conf`
    ```
    capture = kwin
    output_name = HDMI-A-1            # the dummy plug's connector
    encoder = nvenc
    hevc_mode = 2
    origin_web_ui_allowed = lan
    address_family = ipv4
    upnp = disabled
    csrf_allowed_origins = https://<GAMING_SUBNET>.10:47990
    ```
  - **The KWin screencast permission is required:** copy the Flatpak's desktop file to `~/.local/share/applications/dev.lizardbyte.app.Sunshine.desktop` and add `X-KDE-Wayland-Interfaces=zkde_screencast_unstable_v1`. Don't use `KWIN_WAYLAND_NO_PERMISSION_CHECKS`.
  - **Service:** enable the user service `app-dev.lizardbyte.app.Sunshine.service`.
  - **Pairing:** pair from Moonlight on the host. Moonlight shows the PIN; enter it in Sunshine's web UI at `https://<GAMING_SUBNET>.10:47990`.
- **Updates:** Bazzite's `uupd.timer` updates the image in the background. Bazzite 45 (Plasma 6.8.2) breaks Flatpak Sunshine's KWin capture ([ublue-os/bazzite#6253](https://github.com/ublue-os/bazzite/issues/6253)).
  - **Until that is fixed:** stay on 44 (`sudo rpm-ostree rollback`, reboot), then `sudo systemctl disable --now uupd.timer`.
  - **Order matters:** each image keeps its own `/etc`, so disable the timer *after* booting the image you keep.
  - **Re-enable updates** once the bug is fixed.
- **Games:** Steam is preinstalled. If a game hangs the GPU (`Xid 109` in `journalctl -k`), try another Proton version before anything else (see Troubleshooting).

### Extra drive for the game library (optional)

A whole spare drive can go to the guest as a second disk (`vdb`), so games don't fill `linux-gaming.raw`. The guest owns the drive: it partitions and formats it.

1. **Empty the drive on the host** and make sure nothing on the host mounts it (no fstab entry).
2. **Name it in site.env** by its stable path, never `/dev/nvmeN` (those numbers change when drives are added):
   ```
   GAMING_DATA_DISK=/dev/disk/by-id/nvme-<model>_<serial>
   ```
   `ls -l /dev/disk/by-id/ | grep -v part` lists the names.
3. **In AI mode, with the VM shut off:** `sudo ./gaming-setup.sh --apply hook`, then `sudo systemctl reload libvirtd`, then `sudo ./gaming-setup.sh --apply vm-final`.
   - Both steps first check that the drive is a whole disk, that the host doesn't mount it or use it as swap, that no LVM, RAID or device-mapper is on it, and that no other VM uses it.
   - `vm-final` redefines the VM because its disks changed. The drive is attached raw with `discard='unmap'`, so the guest's TRIM reaches the drive (QLC drives need it to stay fast).
4. **In the guest**, the drive is `/dev/disk/by-id/virtio-games`:
   ```
   sudo mkfs.ext4 -L games /dev/disk/by-id/virtio-games
   sudo mkdir -p /var/mnt/games
   echo 'LABEL=games /var/mnt/games ext4 defaults,noatime,nofail 0 2' | sudo tee -a /etc/fstab
   sudo systemctl daemon-reload && sudo mount /var/mnt/games && sudo chown "$USER:" /var/mnt/games
   ```
   Then Steam, Settings, Storage, add `/var/mnt/games`. Enable `fstrim.timer` if it is off (`systemctl is-enabled fstrim.timer`).
5. **Undo:** remove the key from site.env, run steps 3 again (the hook then refuses the drive, and `vm-final` drops it), and remove the guest's fstab line.

## Telemetry

`gaming-exporter` lets Walter's telemetry page show the GPU mode, the gaming VM and the guest's GPU. It's a small Python (stdlib only) HTTP exporter. It runs on the hypervisor as the operator's `systemd --user` unit `gaming-exporter.service`, and Walter's Prometheus scrapes it as job `hypervisor`, every 5 s.

```
gaming-exporter (host, operator) --> ${HYPERVISOR_BRIDGE_IP}:9188/metrics <-- Walter's Prometheus <-- telemetry app
        | ssh (host -> guest only)
        v
  linux-gaming: nvidia-smi, pgrep sunshine, ss
```

**What it exports.** A background thread collects every 5 s. A request only gets the cached text and never runs a command.

| Metrics | Source |
|---|---|
| `hv_gpu_mode_state{mode}`, `hv_gpu_mode_phase{phase}`, `hv_gpu_mode_since_seconds` | `~/.local/state/gpu-mode` (absent = AI mode) |
| `hv_gpu_mode_switch_active{direction}` | the flock on `~/.local/state/gpu-mode.lock`, read passively from `/proc/locks`, and the `/proc/*/cmdline` of a running `game-mode` / `ai-mode` / `gpu-mode game\|ai` |
| `hv_gaming_vm_running`, `hv_gaming_vm_state{state}`, `hv_gaming_vm_vcpus`, `..._cpu_seconds_total`, `..._memory_bytes`, `..._disk_{read,write}_bytes_total`, `..._net_{rx,tx}_bytes_total` | `virsh -c qemu:///system domstats linux-gaming --raw` |
| `hv_gaming_guest_up`, `hv_gaming_guest_poll_seconds` | the last guest poll |
| `hv_gaming_gpu_info{name}`, `hv_gaming_gpu_{utilization,encoder_utilization}_percent`, `..._memory_{used,total}_bytes`, `..._temperature_celsius`, `..._power{,_limit}_watts`, `..._sm_clock_mhz`, `..._fan_percent`, `..._pcie_link_{gen,width}` | the guest's `nvidia-smi --query-gpu` |
| `hv_gaming_sunshine_running`, `hv_gaming_stream_active` | `pgrep -x sunshine` in the guest. A stream is active while Sunshine's video UDP port (base port + 9, so 47998) is bound: Sunshine opens it when a session starts and closes it when the last one ends |
| `hv_host_allowed_cpus{slice,cpus}` | `/sys/fs/cgroup/{system.slice,user.slice,init.scope}/cpuset.cpus.effective` |
| `hv_exporter_build_info`, `hv_exporter_collect_seconds` | the exporter |

**Guest metrics** are present only while the guest answers, so there are no stale values. The guest is polled only while `linux-gaming` runs. Each poll is one ssh command through a persistent master connection, whose control socket is in `$XDG_RUNTIME_DIR/gaming-exporter/`, mode 0700. The ssh options are `IdentitiesOnly`, `BatchMode`, no agent forwarding and no forwardings. **The guest's output is untrusted:**
- reads are capped at 16 KiB, and the output must have the exact section shape;
- only numbers, in range, are kept, and `[N/A]` drops that metric;
- the GPU name is reduced to `[A-Za-z0-9 ._-]`, at most 64 characters.

**Access.** The exporter listens on `${HYPERVISOR_BRIDGE_IP}:9188` only (`IP_FREEBIND`, so it can start before the bridge has its address). The nft table `inet gaming_exporter_guard` lets only `${BACKEND_LAN_IP}` reach that port; loopback is also allowed. `gaming-setup.sh`'s `exporter` step installs the table from `gaming-exporter-guard.nft.tmpl`, and `gaming-exporter-guard.service` loads it at boot. The step also adds the table to `nft-guard-check`. Paths other than `/metrics` and `/healthz` return 404.

**Install:**
1. Run `sudo ./gaming-setup.sh exporter` (plan), then `sudo ./gaming-setup.sh --apply exporter`.
2. As the operator, who needs lingering (`loginctl enable-linger`), install the unit:
   ```bash
   ln -s <checkout>/hypervisor/gaming/gaming-exporter ~/bin/
   install -m 644 <checkout>/hypervisor/gaming/gaming-exporter.service ~/.config/systemd/user/
   # the guest account, if it differs from yours (operator-local, never committed):
   printf 'GAMING_GUEST_USER=<guest user>\n' > ~/.config/deck/gaming-exporter.env
   systemctl --user daemon-reload && systemctl --user enable --now gaming-exporter
   ```
   The exporter reads `HYPERVISOR_BRIDGE_IP` and `GAMING_SUBNET` from `~/.config/deck/site.env` (parsed, never sourced). Optional overrides go in `~/.config/deck/gaming-exporter.env`:
   - `GAMING_GUEST_USER`;
   - `GAMING_GUEST_KEY` (default `~/.ssh/id_linux_gaming`);
   - `GAMING_SUNSHINE_PORT` (Sunshine's `port`, default 47989);
   - `GAMING_EXPORTER_BIND` (`ADDR:PORT`, for tests).

   The guest's host key must already be in `~/.ssh/known_hosts`: ssh it once by hand.
3. Walter's side (the `hypervisor` scrape job) is deployed with `walter/deploy.sh`.

**Verify:**
```bash
systemctl --user status gaming-exporter        # active; journal shows "listening on <bridge ip>:9188"
curl -s http://${HYPERVISOR_BRIDGE_IP}:9188/metrics | grep -v '^#'
curl -s http://${HYPERVISOR_BRIDGE_IP}:9188/healthz       # ok
sudo nft list table inet gaming_exporter_guard            # counters: Walter's scrapes accepted
```
In AI mode, `hv_gaming_vm_running` is 0 and there are no `hv_gaming_gpu_*` lines. In game mode, `hv_gaming_guest_up` is 1. On Walter, `up{job="hypervisor"}` is 1. From any other machine, the port times out.

**Rollback:**
- `systemctl --user disable --now gaming-exporter`, then `rm ~/.config/systemd/user/gaming-exporter.service ~/bin/gaming-exporter`, then `systemctl --user daemon-reload`;
- as root: `systemctl disable --now gaming-exporter-guard.service; rm /etc/gaming-exporter-guard.nft /etc/systemd/system/gaming-exporter-guard.service`, then `systemctl daemon-reload`. The `nft-guard-check` entry is skipped once the unit file is gone.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Starting `linux-gaming` fails: "hook ... may run on the gaming cores" | Started in AI mode. Use `~/bin/game-mode`. |
| Moonlight: "Server certificate mismatch (error 401)" | Usually Sunshine failed to start capture, not a certificate problem. Check `~/.var/app/dev.lizardbyte.app.Sunshine/config/sunshine/sunshine.log`. `zkde_screencast_unstable_v1 not found in registry`: the KWin permission file is missing, or the guest updated to Bazzite 45 (see Updates). |
| Stream is very laggy | The setup variant is defined (virtual GPU primary). Switch to `vm-final`. Then check Moonlight's stats overlay (Ctrl+Alt+Shift+S). |
| A resolution is missing, or `kscreen-doctor` says "The driver rejected the output configuration" | The guest only gets the plug's EDID modes. Most cheap HDMI "2.1" plugs advertise TMDS 600 MHz and no FRL: 5120x1440 needs about 940 MHz at 120 Hz. For an ultrawide monitor, use a DisplayPort EDID emulator that copies the monitor's EDID. It must store all EDID blocks: high-refresh ultrawide modes are often in a third, DisplayID block (384 bytes in all). DP 1.4 (HBR3) carries 5120x1440 at 120 Hz; 144 Hz needs DSC. |
| A game closes, with `NVRM: Xid ... 109 ... CTX SWITCH TIMEOUT` in the guest's `journalctl -k` | The GPU hung on the game's work. Seen with ray tracing on Proton Experimental; a stable Proton release fixed it. Check power and temperature during play with `nvidia-smi --query-gpu=temperature.gpu,power.draw,clocks_event_reasons.active --format=csv -l 2`. |
| `gpu-mode status` shows the host CPUs limited in AI mode | The hook's release did not restore them (fixed in the current hook). Run `for u in system.slice user.slice init.scope; do sudo systemctl set-property --runtime $u AllowedCPUs=0-$(( $(nproc --all) - 1 )) && sudo systemctl set-property --runtime $u AllowedCPUs=; done`, then `sudo ./gaming-setup.sh --apply hook`. |
| `gaming-setup.sh` stops: "GAMING_DATA_DISK ... has a mounted filesystem", "holds /...", or "is already used by domain ..." | The drive in `GAMING_DATA_DISK` is in use on the host or by another VM. Check the by-id name (`readlink -f` on it), unmount it and remove its fstab line, or pick the right drive. |
| Starting `linux-gaming` fails: "hook ... block disk ... (only GAMING_DATA_DISK may be a block device)" | The VM definition has a block device that isn't `GAMING_DATA_DISK`, or the key changed and the hook is older. Re-run `gaming-setup.sh --apply hook`, reload libvirtd, then `--apply vm-final`. |
| `ai-mode` stops: "Walter's config does not match the baseline" | Restore it: `virsh -c qemu:///system define ~/vm-backups/<walter-domain>-baseline.xml`, then re-run `ai-mode`. |

## Undo

`ai-mode` first. Then remove the exporter (see Telemetry, Rollback). Then, as root:
- `virsh undefine --nvram linux-gaming`;
- `virsh net-destroy gaming-nat; virsh net-undefine gaming-nat`;
- `rm /etc/libvirt/hooks/qemu.d/10-linux-gaming /etc/libvirt/hooks/network.d/10-gaming-guard`, then `systemctl reload libvirtd`;
- `systemctl disable --now gaming-guard.service; rm /etc/gaming-guard.nft /etc/systemd/system/gaming-guard.service`, then `systemctl daemon-reload`;
- `rm -r ${GAMING_VMS_DIR}`.

`gaming-setup.sh` prints each of these per step.
