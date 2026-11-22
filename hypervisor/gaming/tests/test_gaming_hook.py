"""The linux-gaming qemu hook's allow-list and gaming-setup's disks_of, run straight out of gaming-setup.sh.

The allow-list is the Python program the hook embeds. It also checks things outside the XML (the gaming-nat
network file, other VMs' cgroups) that don't exist on a CI runner; those add their own reasons, so the tests
look for the disk reasons by text instead of the exit status.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parents[1] / "gaming-setup.sh"
SRC = SETUP.read_text()

m = re.search(r"out=\$\(python3 -I -c '\n(.*?)\n' \"\$\{2%%/\*\}\"\)", SRC, re.S)
assert m, "allow-list program not found in gaming-setup.sh"
ALLOW = m.group(1)

VMS = "/data/vms"
DISK_FILE = f"""<disk type='file' device='disk'><driver name='qemu' type='raw'/>
  <source file='{VMS}/linux-gaming.raw'/><target dev='vda' bus='virtio'/></disk>"""


def block_disk(dev, raw=True):
    return f"""<disk type='block' device='disk'><driver name='qemu' type='{"raw" if raw else "qcow2"}'/>
  <source dev='{dev}'/><target dev='vdb' bus='virtio'/></disk>"""


def domain(*disks):
    return f"""<domain type='kvm'><name>linux-gaming</name>
  <os><type>hvm</type><loader>/usr/share/OVMF/OVMF_CODE_4M.fd</loader></os>
  <devices><emulator>/usr/bin/qemu-system-x86_64</emulator>{"".join(disks)}</devices></domain>"""


def reasons(xml, data_disk=""):
    env = dict(os.environ, VMS_DIR=VMS, GPU_PCI="0000:03:00.0 0000:03:00.1", HOST_CPUS="8-15,24-31",
               DATA_DISK=data_disk)
    r = subprocess.run(["python3", "-I", "-c", ALLOW, "prepare"], input=xml, env=env, capture_output=True,
                       text=True, timeout=30)
    return r.stderr


def mounted_bases():
    out = set()
    for f in ("/proc/self/mounts", "/proc/swaps"):
        for line in Path(f).read_text().splitlines():
            src = line.split()[0] if line.strip() else ""
            if src.startswith("/dev/"):
                out.add(os.path.basename(os.path.realpath(src)))
    return out


def free_whole_disk():
    """A /sys/block entry with a /dev node, nothing mounted from it or its partitions, and no holders."""
    used = mounted_bases()
    for base in sorted(os.listdir("/sys/block")):
        parts = {base} | {p for p in os.listdir(f"/sys/block/{base}") if p.startswith(base)}
        if parts & used or not os.path.exists(f"/dev/{base}"):
            continue
        if os.listdir(f"/sys/block/{base}/holders"):
            continue
        return base
    return None


def test_stray_block_device_is_refused():
    err = reasons(domain(DISK_FILE, block_disk("/dev/disk/by-id/some-other-disk")),
                  data_disk="/dev/disk/by-id/the-data-disk")
    assert "only GAMING_DATA_DISK may be a block device" in err


def test_no_block_device_allowed_without_data_disk():
    err = reasons(domain(DISK_FILE, block_disk("/dev/disk/by-id/the-data-disk")), data_disk="")
    assert "only GAMING_DATA_DISK may be a block device" in err


def test_file_disk_outside_vms_dir_still_refused():
    err = reasons(domain(DISK_FILE.replace(VMS, "/home")), data_disk="")
    assert "outside" in err


def test_missing_data_disk_is_refused():
    dev = "/dev/disk/by-id/does-not-exist-on-this-machine"
    err = reasons(domain(DISK_FILE, block_disk(dev)), data_disk=dev)
    assert "is not a whole disk" in err


def test_data_disk_must_be_raw(tmp_path):
    base = free_whole_disk()
    if base is None:
        pytest.skip("no unused whole disk on this machine")
    link = tmp_path / "by-id-data-disk"
    link.symlink_to(f"/dev/{base}")
    err = reasons(domain(DISK_FILE, block_disk(str(link), raw=False)), data_disk=str(link))
    assert "is not raw" in err


def test_unused_data_disk_is_accepted(tmp_path):
    base = free_whole_disk()
    if base is None:
        pytest.skip("no unused whole disk on this machine")
    link = tmp_path / "by-id-data-disk"
    link.symlink_to(f"/dev/{base}")
    err = reasons(domain(DISK_FILE, block_disk(str(link))), data_disk=str(link))
    assert "data disk" not in err and "block disk" not in err, err


def test_mounted_data_disk_is_refused(tmp_path):
    used = sorted(b for b in mounted_bases() if os.path.isdir(f"/sys/block/{b}"))
    # a partition is mounted: find its whole disk
    whole = None
    for base in sorted(os.listdir("/sys/block")):
        parts = {base} | {p for p in os.listdir(f"/sys/block/{base}") if p.startswith(base)}
        if parts & mounted_bases() and os.path.exists(f"/dev/{base}"):
            whole = base
            break
    if whole is None and not used:
        pytest.skip("no mounted block device on this machine")
    link = tmp_path / "by-id-system-disk"
    link.symlink_to(f"/dev/{whole or used[0]}")
    err = reasons(domain(DISK_FILE, block_disk(str(link))), data_disk=str(link))
    assert "is mounted or used as swap on the host" in err


def disks_of(xml):
    fn = re.search(r"^disks_of\(\) \{\n.*?^\}\n", SRC, re.S | re.M).group(0)
    r = subprocess.run(["bash", "-c", fn + "disks_of"], input=xml, capture_output=True, text=True, timeout=30,
                       check=True)
    return r.stdout.strip().splitlines()


def test_disks_of_lists_disks_and_skips_cdroms():
    cdrom = "<disk type='file' device='cdrom'><source file='/data/vms/x.iso'/></disk>"
    assert disks_of(domain(block_disk("/dev/disk/by-id/d"), DISK_FILE, cdrom)) == [
        "block /dev/disk/by-id/d", f"file {VMS}/linux-gaming.raw"]


def test_disks_of_changes_when_the_data_disk_is_added():
    assert disks_of(domain(DISK_FILE)) != disks_of(domain(DISK_FILE, block_disk("/dev/disk/by-id/d")))


def test_template_attaches_data_disk_to_setup_and_final_only():
    # the datadisk block is built in the non-install branch and placed after the system disk
    tmpl = SRC[SRC.index("write_vm_xml() {"):]
    assert "<source dev='$DATA_DISK'/>" in tmpl and "discard='unmap'" in tmpl
    install_branch = tmpl[tmpl.index("if [[ $mode == install ]]; then"):tmpl.index("    else\n        mem=24")]
    assert "datadisk=" not in install_branch
    assert re.search(r"<boot order='1'/>\n    </disk>\n\$datadisk\n", tmpl)


def test_site_value_validation():
    # gaming-setup refuses partitions and anything outside /dev/disk/by-id
    rx = re.search(r"\[\[ -z \$DATA_DISK \|\| \( \$DATA_DISK =~ (\S+) && ! \$DATA_DISK =~ (\S+) \) \]\]", SRC)
    assert rx
    ok, part = re.compile(rx.group(1)), re.compile(rx.group(2))
    good = "/dev/disk/by-id/nvme-VENDOR_MODEL_EXAMPLE"
    assert ok.search(good) and not part.search(good)
    assert part.search(good + "-part1")
    assert not ok.search("/dev/nvme9n1") and not ok.search("/dev/disk/by-id/../../nvme9n1 x")
