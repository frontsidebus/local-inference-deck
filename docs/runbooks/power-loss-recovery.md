# Power-loss recovery

What happens when the home power fails (the hypervisor and Walter go down hard; Covenant stays up in the cloud), what comes back on its own, and what to check. Based on the outages of 2026-10-01 and 2026-10-02.

## While the power is out

- Covenant keeps serving TLS. `chat.`, `id.`, `api.`, `telemetry.` (and `digest.`, if enabled) return 502/504 because the tunnel peer is gone. Nothing needs doing on the edge.
- Harnesses fail fast with 5xx. No state is lost on the client side.

## What recovers on its own

| Layer | Mechanism |
|---|---|
| Hypervisor | Boots, binds both GPUs to `vfio-pci`, starts libvirt. (Needs the BIOS set to power on after AC loss.) |
| Walter VM | libvirt **autostart** is set on the domain, so it starts with libvirtd. (It is turned off on purpose during hardware changes; see [hypervisor/README.md](../../hypervisor/README.md#hardware-changes-gpus-risers-slots).) |
| `/models` | Mounted by UUID with `nofail`; XFS replays its journal on mount. |
| GPUs in Walter | `nvidia-persistenced` is wanted by `multi-user.target`; GPU-bound units are ordered after it. |
| WireGuard | `wg-quick@wg0` on Walter dials Covenant; keepalives re-establish the session within seconds. |
| llama-swap | Enabled systemd service; its startup hook preloads `coder` and `coder-fast` (logs a harmless `status 404`). |
| Gateway, WebUI, monitoring, telemetry, digest | Docker starts the compose stacks again from their restart policies (`unless-stopped`). |
| Postgres | Runs crash recovery from its WAL on start ("database system was not properly shut down; automatic recovery in progress"). No action needed. |
| SQLite (Open WebUI, Pocket-ID) | WAL is replayed on open. |
| Firewall | ufw and `docker-user-rules.service` (the `WALTER-PUBLISHED` chain) start at boot. |
| Backups | `spark-backup.timer` has `Persistent=true`: if the 03:30 UTC run was missed, it runs shortly after boot. `spark-offsite.timer` (04:30 UTC, if enabled) is `Persistent=true` too. |
| Update check | `spark-update-check.timer` is also `Persistent=true`. |
| Ollama (retired) | Stays off: its drop-in requires `/etc/ollama-enabled`, even if another unit `Wants=` it. |

## What to check

### 1. Hypervisor

```bash
virsh -c qemu:///system list --all              # Walter: running
virsh -c qemu:///system dominfo <walter-domain> | grep Autostart   # enable
lspci -nnk | grep -A3 NVIDIA                    # Kernel driver in use: vfio-pci
virsh -c qemu:///system dumpxml <walter-domain> | grep -A3 '<hostdev'   # source addresses = the GPUs' lspci addresses
systemctl --failed
journalctl -b -k | grep -iE 'xfs.*(error|corrupt)|nvme.*error'   # expect nothing
qemu-img check -U <walter-qcow2>                # "No errors were found on the image."
```

Expected and harmless after a hard power-off:
- XFS logs "Starting/Ending recovery" on the hypervisor's filesystems (log replay).
- journald renames and replaces the system journal ("corrupted or uncleanly shut down").
- libvirt warns "Invalid XATTR timestamp" on the domain's disk and NVRAM files.

Always use `/dev/disk/by-id` or `by-uuid` names on the hypervisor: the `nvmeXn1` names have swapped between boots.

`nvidia-cdi-refresh` is masked on the hypervisor on purpose (it fails when both GPUs belong to `vfio-pci`). It should not appear in `systemctl --failed`.

### 2. Walter

```bash
ssh ${BACKEND_SSH_USER}@${BACKEND_LAN_IP}
systemctl --failed
findmnt /models
nvidia-smi --query-gpu=index,name,persistence_mode,memory.used,pcie.link.gen.max,pcie.link.width.current --format=csv   # width 8 on both
sudo wg show wg0 latest-handshakes              # recent timestamp
systemctl status llama-swap --no-pager | head -5
sudo docker ps --format '{{.Names}}\t{{.Status}}'   # all Up / healthy
sudo docker compose -f /srv/gateway/compose.yaml logs db | grep -i 'recovery\|ready to accept'
systemctl list-timers 'spark-*'
journalctl -u spark-backup -b                   # the catch-up run, if one was due
cat /models/backups/LAST_OK
```

Expect `coder` on GPU0 and `coder-fast` on GPU1, both resident, about a minute after llama-swap starts (each model itself loads in 10–12 s from a cold disk). The telemetry dashboard should show each GPU as "PCIe Gen4 x8" under load; an idle GPU shows a lower generation with "power-save", which is normal. A "below xN" warning means a link came back narrower than expected ([telemetry](../../walter/telemetry/README.md.tmpl)).

Walter's own boot-time fsck handles its disks: ext4 `/` and `/boot` replay their journals, and the vfat EFI partition has its dirty bit cleared automatically ("Dirty bit is set... Automatically removing dirty bit"). The same fsck notes "differences between boot sector and its backup (offset 65)". Offset 65 is the in-use flag Linux sets while the partition is mounted, so it is harmless. To confirm it clears on a clean unmount:

```bash
sudo umount /boot/efi && sudo fsck.vfat -n /dev/vda15; sudo mount /boot/efi   # expect no differences reported
```

### 3. End to end, from outside

```bash
curl -sI https://${SPARK_DOMAIN} | head -1                  # 301 to chat.
curl -sI https://${SPARK_CHAT_HOST} | head -1               # 200
curl -sI https://${SPARK_ID_HOST} | head -1                 # Pocket-ID up
curl -s  -o /dev/null -w '%{http_code}\n' https://${SPARK_API_HOST}/v1/models   # 401 (no key)
curl -s  https://${SPARK_API_HOST}/v1/models -H "Authorization: Bearer $(cat ~/.config/spark/opencode.key)" | head -c 200
```

Then send one chat in Open WebUI and one request through a harness (`claude-spark -p ping`). Open the telemetry dashboard and confirm GPU and request panels update. In Grafana (SSH tunnel, see [ARCHITECTURE](../../ARCHITECTURE.md#monitoring-and-telemetry)), check that all Prometheus targets are up and no alerts fire.

## If something did not come back

| Symptom | Likely cause | Fix |
|---|---|---|
| Walter not running | Autostart lost (domain redefined), or left off after a hardware change | If the hardware changed, check the hostdev addresses first ([hypervisor](../../hypervisor/README.md#hardware-changes-gpus-risers-slots)). Then `virsh start <walter-domain>` and `virsh autostart <walter-domain>`. |
| Walter fails to start with a PCI / hostdev error | GPU addresses changed (hardware change) | Fix the `<hostdev>` addresses as in [hypervisor/README.md](../../hypervisor/README.md#hardware-changes-gpus-risers-slots); keep autostart off until it starts. |
| `/models` not mounted | Disk not attached or UUID changed | Check the domain's disk by serial, `blkid`, then `sudo mount /models`; llama-swap and backups depend on it. |
| Models load CPU-only or llama-swap fails | Driver raced the service | `sudo systemctl restart nvidia-persistenced llama-swap` |
| 502 on every host | Tunnel down | `sudo systemctl restart wg-quick@wg0` on Walter; check Covenant's ufw allows `udp/${WG_PORT}`. |
| Gateway 500s, Postgres restarting | Failed WAL recovery (rare) | Check `docker compose logs db`; if the data dir is damaged, restore from the last snapshot (`RESTORE.md`, section 2). |
| Pocket-ID will not start, "already one instance running" | Stale heartbeat (after a restore) | `DELETE FROM francis_hosts;` with Pocket-ID stopped (`RESTORE.md`, section 4). |
| No backup today | Timer not persistent, or `/models` missing at boot | `sudo systemctl start spark-backup`, then read the journal. |

A restore test after any unclean shutdown is cheap: `sudo /usr/local/sbin/spark-backup-restore-test.sh`.
