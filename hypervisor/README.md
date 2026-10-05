# Hypervisor layer

The host that runs Walter is managed outside this repo, by a separate (private) KVM/libvirt infrastructure-as-code project. This page lists what that layer must provide so the rest of the stack works.

| Requirement | Why |
|---|---|
| Ubuntu 24.04 with IOMMU enabled and both GPUs (plus their audio functions) bound to `vfio-pci` | GPUs are passed through to Walter. |
| Walter as a libvirt domain with **autostart**, 16 vCPU, ~80 GiB RAM | Recovers by itself after power loss. VFIO pins all guest RAM, so check host `MemAvailable` minus a reserve before raising it. |
| A whole NVMe passed raw as virtio `vdb` (`cache=none`, `io=native`, `discard=unmap`) with a fixed `<serial>` | Appears in the guest as `/dev/disk/by-id/virtio-<serial>`, which `walter/deploy.sh` formats/mounts as `${MODELS_DIR}` (by UUID, `nofail`). |
| Disks referenced by `/dev/disk/by-id/...`, never `/dev/nvmeXn1` | NVMe device names swap between boots. |
| `nvidia-cdi-refresh.{path,service}` masked on the host while every NVIDIA GPU is on `vfio-pci` | Otherwise they fail every boot and leave systemd "degraded". Unmask if a GPU is returned to the host. |
| BIOS set to power on after AC loss | Unattended recovery; see [power-loss runbook](../docs/runbooks/power-loss-recovery.md). |
| A libvirt NAT bridge (`${HYPERVISOR_BRIDGE_IP}`) | Walter's LAN path, and where the optional workstation `hermes-gateway` listens. |

Lessons from the current hardware: since the x8/x8 bifurcation riser (2026-10-05) both GPUs are PCIe Gen4 x8 on CPU root ports (before it, the second GPU sat in a chipset x1 slot). There is no NVLink and no P2P between GeForce cards under vfio, so llama.cpp's experimental tensor split goes through host shared memory; it pays off only for the dense split models. Models are placed one per GPU, layer-split or tensor-split. See [ARCHITECTURE](../ARCHITECTURE.md).
