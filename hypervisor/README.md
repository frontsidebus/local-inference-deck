# Hypervisor layer

The host that runs Walter is managed outside this repo, by a separate (private) KVM/libvirt infrastructure-as-code project. This page lists what that layer must provide so the rest of the stack works, and the procedures that touch it here (hardware changes, recovery).

## Requirements

| Requirement | Why |
|---|---|
| Ubuntu 24.04 with IOMMU enabled and both GPUs (plus their audio functions) bound to `vfio-pci` | GPUs are passed through to Walter. Live, `vfio-pci` claims them by vendor:device ID (`options vfio-pci ids=...` plus `softdep <driver> pre: vfio-pci`), so the binding follows the cards to any slot. |
| Walter as a libvirt domain with **autostart**, 16 vCPU, ~80 GiB RAM | Recovers by itself after power loss. VFIO pins all guest RAM, so check host `MemAvailable` minus a reserve before raising it. Turn autostart **off** around hardware changes (below). |
| One `<hostdev>` per GPU function (VGA `.0` and audio `.1`), `managed='yes'`, by host PCI address | The domain names the GPUs **by address**, not by ID. See [Hardware changes](#hardware-changes-gpus-risers-slots). |
| A whole NVMe passed raw as virtio `vdb` (`cache=none`, `io=native`, `discard=unmap`) with a fixed `<serial>` | Appears in the guest as `/dev/disk/by-id/virtio-<serial>`, which `walter/deploy.sh` formats/mounts as `${MODELS_DIR}` (by UUID, `nofail`). |
| Disks referenced by `/dev/disk/by-id/...`, never `/dev/nvmeXn1` | NVMe device names swap between boots. |
| `nvidia-cdi-refresh.{path,service}` masked on the host while every NVIDIA GPU is on `vfio-pci` | Otherwise they fail every boot and leave systemd "degraded". Unmask if a GPU is returned to the host. |
| BIOS set to power on after AC loss | Unattended recovery; see [power-loss runbook](../docs/runbooks/power-loss-recovery.md). |
| BIOS bifurcation x8/x8 on the riser slot | Both GPUs share one x16 CPU slot through the riser. |
| A libvirt NAT bridge (`${HYPERVISOR_BRIDGE_IP}`) | Walter's LAN path, and where the optional workstation `hermes-gateway` listens. |

## Current hardware

- **Two RTX 3090s, both PCIe Gen4 x8 on CPU root ports**, through an x16 → x8/x8 bifurcation riser (installed 2026-10-04). Before the riser the second GPU sat in a chipset slot at x1.
- In Walter, `nvidia-smi --query-gpu=index,pcie.link.gen.max,pcie.link.width.current --format=csv` shows `4, 8` for both GPUs. The link drops to Gen1/2 at idle (power saving) and returns to Gen4 under load.
- Weight uploads run at 10–13 GB/s per GPU, so cold model loads are limited by the models disk (about 2.8 GB/s through virtio), not by PCIe. The second GPU's cold load of `coder-fast` went from 18–22 s on x1 to 12 s.
- There is no NVLink and no P2P between GeForce cards under vfio. llama.cpp's experimental tensor split therefore goes through host shared memory (NCCL SHM transport). It pays off for the dense split models (`hermes`, `vision`), not for the MoE `big`. Row split does not load on the pinned llama.cpp build. Numbers: [walter/llama-swap/BENCHMARKS.md](../walter/llama-swap/BENCHMARKS.md).
- Models are placed one per GPU, layer-split or tensor-split. See [ARCHITECTURE](../ARCHITECTURE.md).

## Hardware changes (GPUs, risers, slots)

Moving a GPU, adding a riser or changing the slot layout changes the GPUs' **host PCI addresses**. Walter's `<hostdev>` entries name addresses and are `managed='yes'`: when the domain starts, libvirt detaches **whatever device is at that address** from its host driver and hands it to vfio. With a stale address that can be a different device. On the riser change, the second GPU moved to a new bus, and its old address then belonged to the chipset PCIe switch in front of the host's root NVMe. Starting Walter with the old XML would have tried to take that switch away from the host.

**Lesson: disable Walter's autostart before any hardware change, and re-enable it only after the hostdev addresses are checked.**

```bash
# 1. Before powering off
virsh -c qemu:///system autostart --disable <walter-domain>
virsh -c qemu:///system dumpxml <walter-domain> > <walter-domain>.pre-change.xml   # backup, keep it outside this repo
virsh -c qemu:///system shutdown <walter-domain>                                    # clean guest shutdown

# 2. Change the hardware and boot the host (Walter stays off)

# 3. Find the new addresses and check the binding
lspci -nnk | grep -A3 -i nvidia          # every GPU function: "Kernel driver in use: vfio-pci"
lspci -tv                                 # which root port or switch each GPU sits behind
# each GPU should share its IOMMU group only with its own audio function:
for d in /sys/kernel/iommu_groups/*/devices/*; do echo "$(basename "$(dirname "$(dirname "$d")")") $(basename "$d")"; done | sort -n

# 4. Point the hostdev entries at the new addresses (the .0 and .1 function of each GPU)
virsh -c qemu:///system edit <walter-domain>
#   <hostdev mode='subsystem' type='pci' managed='yes'>
#     <source><address domain='0x0000' bus='0x<NN>' slot='0x00' function='0x0'/></source>
virsh -c qemu:///system dumpxml <walter-domain> > <walter-domain>.post-change.xml

# 5. Start, verify in the guest, then re-enable autostart
virsh -c qemu:///system start <walter-domain>
ssh ${BACKEND_SSH_USER}@${BACKEND_LAN_IP} \
  'nvidia-smi --query-gpu=index,name,pcie.link.gen.max,pcie.link.width.current --format=csv; sudo dmesg | grep -iE "xid|aer" | tail'
virsh -c qemu:///system autostart <walter-domain>
```

Then run the checks in [power-loss-recovery §2](../docs/runbooks/power-loss-recovery.md#2-walter) and load each split model once (`big`, `vision`, `hermes`) to exercise both GPUs.

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| Walter does not start after a hardware change, with a hostdev / PCI device error | The hostdev addresses are stale. Fix them as in step 4. Keep autostart off until Walter starts cleanly. |
| A GPU function shows a host driver (`nvidia`, `nouveau`, `snd_hda_intel`) instead of `vfio-pci` | The `vfio-pci ids=` / softdep config is missing or not applied at boot. Fix it in the host IaC and reboot. |
| `nvidia-smi` in Walter shows `x1`, or Gen1 under load | Check the riser seating and the BIOS bifurcation setting for that slot. Gen1/2 **at idle** is normal. |
| The telemetry dashboard labels the GPUs "chipset slot" | Cosmetic known issue: it labels any link narrower than the card's maximum (x8 of x16) that way. See [walter/telemetry](../walter/telemetry/README.md.tmpl#known-issues). |
| Host `systemctl --failed` lists `nvidia-cdi-refresh` | It was unmasked; mask it again while both GPUs are on vfio-pci. |

## Rollback

- **Domain XML:** `virsh -c qemu:///system define <walter-domain>.pre-change.xml` restores the backup from step 1. Only do this with the hardware back in its old layout, or the old addresses are stale in the same way.
- **Hardware:** with the old layout back, check the addresses with step 3 before starting Walter, then re-enable autostart.
