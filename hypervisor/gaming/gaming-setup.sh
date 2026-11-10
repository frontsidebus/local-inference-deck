#!/usr/bin/env bash
# Gaming VM host prep: the root side of hypervisor/gaming (see README.md next to this file).
#
#   sudo ./gaming-setup.sh                         # plan: show current state, change nothing
#   sudo ./gaming-setup.sh --apply                 # apply the default steps in order, asking before each
#   sudo ./gaming-setup.sh --apply guard network   # apply only the named steps
#   --yes skips the per-step questions. --site-env FILE: site values (default: the invoking user's
#   ~/.config/deck/site.env; keys GAMING_*, WALTER_DOMAIN; read, never sourced).
#
# Steps (default, in this order: guard network storage packages guests hook vm; vm-setup and vm-final only when named):
#   guard     nft table inet gaming_guard (guest -> internet only), loaded at boot by gaming-guard.service,
#             and added to the 5-minute nft-guard-check
#   network   libvirt NAT network gaming-nat (virbr-gaming, ${GAMING_SUBNET}.0/24, no IPv6); refuses to run
#             unless the guard table is loaded
#   storage   ${GAMING_VMS_DIR} (root:kvm 0710), a preallocated raw disk linux-gaming.raw, and the Bazzite ISO
#             (copied, then sha256 and cosign signature checked on the copy)
#   packages  apt install vainfo (to check the iGPU's HEVC decode for Moonlight)
#   guests    libvirt-guests SHUTDOWN_TIMEOUT 120 -> 300 s, so a host reboot never cuts Walter's ACPI shutdown short
#   hook      libvirt qemu hook for linux-gaming: allow-list (GPU1 only, disks under ${GAMING_VMS_DIR},
#             gaming-nat), guard check, host CPUs off the game's cores while GPU1 is in use (libvirtd reload)
#   vm        define linux-gaming, install variant: no GPU, VNC console on a unix socket, the Bazzite ISO
#   vm-setup  (only when named, after the install) GPU1 + VNC console, for the first boot with the card
#   vm-final  (only when named, after the setup) GPU1 only, no console: played through Moonlight
#
# It never touches Walter, its networks, the GPUs or GRUB, and never starts a VM.
# Files it overwrites are copied to /root/gaming-setup-<time>/ first, with a log of the run; each step
# prints its undo.

set -euo pipefail

NET=gaming-nat
BR=virbr-gaming
GUARD_UNIT=gaming-guard.service
GUARD_NFT=/etc/gaming-guard.nft
NET_HOOK=/etc/libvirt/hooks/network.d/10-gaming-guard
QEMU_HOOK=/etc/libvirt/hooks/qemu.d/10-linux-gaming
CHECK=/usr/local/sbin/nft-guard-check
IMG_GIB=160          # preallocated
MARGIN_GIB=20        # kept free on the disk's filesystem on top of Walter's possible qcow2 growth
ISO_NAME=bazzite-nvidia-open-stable-live-amd64.iso
# Bazzite's cosign key (github.com/ublue-os/bazzite/blob/main/cosign.pub), pinned by sha256.
COSIGN_PUB_SHA256=af78ecfda6eb21c35195af3739341715e9cfc3f2f5911dd9c10b0670547bf6e8
VIRSH=(virsh -q -c qemu:///system)

DEFAULT_STEPS=(guard network storage packages guests hook vm)
ALL_STEPS=("${DEFAULT_STEPS[@]}" vm-setup vm-final)

APPLY=0 YES=0 STEPS=() SITE=
while (($#)); do
    case $1 in
        --apply) APPLY=1 ;;
        --yes) YES=1 ;;
        --site-env) SITE=${2:?--site-env needs a file}; shift ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
        *) [[ " ${ALL_STEPS[*]} " == *" $1 "* ]] || { echo "unknown step: $1" >&2; exit 2; }
           STEPS+=("$1") ;;
    esac
    shift
done
((${#STEPS[@]})) || STEPS=("${DEFAULT_STEPS[@]}")

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 2; }
U=${SUDO_USER:-}
[[ -n $U && $U != root ]] || { echo "run via sudo from your own account (SUDO_USER locates the ISO download)" >&2; exit 2; }
UHOME=$(getent passwd "$U" | cut -d: -f6)
DL=$UHOME/Downloads/gaming

# Site values (site.env.example, "hypervisor: GPU modes"). gpu-mode reads the same keys: keep them in step.
SITE=${SITE:-$UHOME/.config/deck/site.env}
[[ -r $SITE ]] || { echo "cannot read $SITE (pass --site-env FILE)" >&2; exit 2; }
site() { local v; v=$(sed -n "s/^$1=//p" "$SITE" | tail -1 | sed -E 's/[[:space:]]+#.*$//' | tr -d '"'"'"); printf '%s' "${v:-${2:-}}"; }
WALTER_DOM=$(site WALTER_DOMAIN walter)
read -ra GPU_PCI <<<"$(site GAMING_GPU_PCI '0000:03:00.0 0000:03:00.1')"
HOST_CPUS=$(site GAMING_HOST_CPUS 8-15,24-31)     # Walter and the host while the game runs
GUEST_CPUS=$(site GAMING_GUEST_CPUS 0-7,16-23)    # the game's vCPUs, pinned one to one
SUBNET=$(site GAMING_SUBNET 192.168.150)
VMS=$(site GAMING_VMS_DIR /data/vms)
IMG=$VMS/linux-gaming.raw
DATAFS=$(dirname "$VMS")
for v in "${GPU_PCI[@]}"; do [[ $v =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$ ]] || { echo "GAMING_GPU_PCI: bad address '$v'" >&2; exit 2; }; done
[[ $SUBNET =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ && $VMS == /?* && $WALTER_DOM =~ ^[A-Za-z0-9._-]+$ ]] \
    || { echo "GAMING_SUBNET, GAMING_VMS_DIR or WALTER_DOMAIN malformed in $SITE" >&2; exit 2; }

# smt_pairs LIST: the CPUs in LIST as "cpu sibling" lines (one per physical core, from sysfs)
smt_pairs() {
    python3 -I - "$1" <<'EOF'
import sys
def rng(s):
    out = []
    for p in s.strip().split(","):
        if p:
            a, _, b = p.partition("-"); out += range(int(a), int(b or a) + 1)
    return out
want, seen = rng(sys.argv[1]), set()
for c in want:
    if c in seen: continue
    sib = [x for x in rng(open("/sys/devices/system/cpu/cpu%d/topology/thread_siblings_list" % c).read()) if x in want]
    seen.update(sib)
    print(" ".join(map(str, sorted(sib))))
EOF
}

BK=
if ((APPLY)); then
    BK=/root/gaming-setup-$(date +%Y%m%d-%H%M%S)
    install -d -m 700 "$BK"
    # tee ignores Ctrl-C/hangup so the last lines (and the failure message) still reach the log.
    exec > >(trap '' INT HUP; exec tee -a "$BK/run.log") 2>&1
    TEEPID=$!
fi

on_exit() {
    local rc=$?
    if ((rc != 0)); then
        echo "!! stopped (rc=$rc) during step: ${CUR:-none}; later steps were not run" >&2
    fi
    if [[ -n ${TEEPID:-} ]]; then exec >&- 2>&-; wait "$TEEPID" 2>/dev/null || true; fi
}
trap on_exit EXIT

hdr()  { printf '\n=== %s\n' "$*"; }
ok()   { printf '  ok:   %s\n' "$*"; }
todo() { printf '  todo: %s\n' "$*"; }
note() { printf '  note: %s\n' "$*"; }
die()  { printf '  !! %s\n' "$*" >&2; exit 1; }
modeof() { stat -c '%a %U:%G' "$1"; }

# Runs "$@" when applying; otherwise just shows it.
run() {
    if ((APPLY)); then printf '  run: '; printf ' %q' "$@"; echo; "$@"
    else printf '  would run:'; printf ' %q' "$@"; echo; fi
}

# Copies a file into the backup dir before it is overwritten (apply mode only).
backup() {
    ((APPLY)) || return 0
    [[ -e $1 ]] || return 0
    install -d -m 700 "$BK/files$(dirname "$1")"
    cp -a "$1" "$BK/files$1"
    printf '  backup: %s -> %s\n' "$1" "$BK/files$1"
}

# Asks once per step when applying. Returns 1 (skip) on anything but y/yes.
confirm() {
    ((APPLY)) || return 0
    ((YES)) && return 0
    local r=
    if ! read -r -p "  apply step '$CUR'? [y/N] " r </dev/tty; then
        echo "  !! no terminal to ask on; re-run from a terminal, or add --yes" >&2; exit 2
    fi
    [[ $r == [yY]* ]] || { note "skipped"; return 1; }
}

# libvirt network hook (libvirt.org/hooks.html; hooks/network.d/ since 6.5): a non-zero exit on
# "<name> start begin" aborts that network's start. Every other network and operation exits 0.
write_net_hook() {
    cat >"$1" <<EOF
#!/bin/sh
# Installed by gaming-setup.sh (hypervisor/gaming): refuse to start $NET unless nft table inet gaming_guard is loaded.
[ "\$1" = "$NET" ] && [ "\$2" = start ] && [ "\$3" = begin ] || exit 0
/usr/sbin/nft list table inet gaming_guard 2>/dev/null | grep -q -- "$BR" && exit 0
echo "gaming-guard hook: nft table inet gaming_guard is not loaded; refusing to start $NET" >&2
exit 1
EOF
}

# yes when every vCPU/emulator thread of every running VM is already on HOST_CPUS (only in game mode)
state_of_walter_pinned() {
    local f
    for f in /sys/fs/cgroup/machine.slice/machine-qemu*.scope/libvirt/*/cpuset.cpus.effective; do
        [[ -e $f ]] || continue
        [[ $(cat "$f") == "$HOST_CPUS" ]] || { echo no; return; }
    done
    echo yes
}

guard_loaded() { nft list table inet gaming_guard 2>/dev/null | grep -q -- "$BR"; }

# ---------------------------------------------------------------------------------------------
write_guard_nft() {
    cat >"$1" <<EOF
# Gaming VM guard (installed by gaming-setup.sh, hypervisor/gaming). The guest on $BR may reach
# the public internet (through libvirt's NAT) and the host's DHCP/DNS, and nothing else: no host
# address (hermes-gateway, the host's LAN address, ...), no LAN, no Walter, no docker, no IPv6.
# The first two lines make a reload replace the table atomically instead of adding duplicate rules.
table inet gaming_guard
delete table inet gaming_guard
table inet gaming_guard {
  set nonpublic4 {
    type ipv4_addr; flags interval
    elements = { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16,
                 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 }
  }
  chain input {   # guest -> any host address
    type filter hook input priority -10; policy accept;
    iifname "$BR" ct state established,related accept   # replies to host-initiated Moonlight
    iifname "$BR" ip daddr $SUBNET.1 udp dport 53 accept   # this network's dnsmasq only: other
    iifname "$BR" ip daddr $SUBNET.1 tcp dport 53 accept   # dnsmasqs (other libvirt networks) stay blocked
    iifname "$BR" ip daddr { $SUBNET.1, 255.255.255.255 } udp sport 68 udp dport 67 accept   # DHCP
    iifname "$BR" meta nfproto ipv6 drop
    iifname "$BR" counter drop
  }
  chain forward {
    type filter hook forward priority -10; policy accept;
    iifname "$BR" ip daddr @nonpublic4 counter drop     # LAN, Walter, other VM networks, docker
    iifname "$BR" meta nfproto ipv6 drop
    oifname "$BR" ct state new counter drop             # nothing new into the guest via forward
  }
}
EOF
}

step_guard() {
    hdr "guard: nft table inet gaming_guard"
    local need=0 tmp
    tmp=$(mktemp)
    write_guard_nft "$tmp"
    nft -c -f "$tmp" || { rm -f "$tmp"; die "nft rejects the guard ruleset (syntax check); nothing changed"; }
    ok "guard ruleset passes 'nft -c' (syntax check)"
    if [[ -e $GUARD_NFT ]] && cmp -s "$tmp" "$GUARD_NFT"; then ok "$GUARD_NFT is current"
    else todo "write $GUARD_NFT"; need=1; fi
    if systemctl is-enabled -q "$GUARD_UNIT" 2>/dev/null && systemctl is-active -q "$GUARD_UNIT"; then ok "$GUARD_UNIT enabled and active"
    else todo "install and enable $GUARD_UNIT (oneshot, loads the table at boot, before libvirtd)"; need=1; fi
    if guard_loaded; then ok "table inet gaming_guard is loaded"; else todo "load table inet gaming_guard"; need=1; fi
    if [[ ! -e $CHECK ]]; then
        note "$CHECK not found (the security script's guard step installs it); skipping the health-check hook"
    elif grep -q 'check gaming_guard' "$CHECK"; then ok "nft-guard-check already covers gaming_guard"
    else todo "add gaming_guard to $CHECK (5-minute timer reloads it if it goes missing)"; need=1; fi
    if [[ -e $NET_HOOK ]] && cmp -s <(write_net_hook /dev/stdout) "$NET_HOOK"; then ok "$NET_HOOK is current"
    else todo "install $NET_HOOK (libvirt refuses to start $NET while the guard table is missing)"; need=1; fi
    note "the unit has no tie to libvirtd on purpose: binding them would let a guard failure take Walter's"
    note "networks down. The libvirt hook is what stops $NET (only) from starting unguarded."
    ((need)) || { rm -f "$tmp"; return 0; }
    confirm || { rm -f "$tmp"; return 0; }
    if ((APPLY)); then
        backup "$GUARD_NFT"; backup "/etc/systemd/system/$GUARD_UNIT"; backup "$NET_HOOK"
        install -o root -g root -m 644 "$tmp" "$GUARD_NFT"
        install -d -o root -g root -m 755 "$(dirname "$NET_HOOK")"
        write_net_hook "$NET_HOOK"; chown root:root "$NET_HOOK"; chmod 755 "$NET_HOOK"
        sh -n "$NET_HOOK" || die "$NET_HOOK does not parse"
        # It must pass every other network untouched (Walter's above all).
        for n in $("${VIRSH[@]}" net-list --all --name | grep -vx "$NET") default; do
            "$NET_HOOK" "$n" start begin - </dev/null || { rm -f "$NET_HOOK"; die "$NET_HOOK would block network $n; removed it"; }
        done
        echo "  wrote $NET_HOOK (picked up the next time libvirtd starts; it idles out after 120 s)"
        cat >"/etc/systemd/system/$GUARD_UNIT" <<EOF
[Unit]
Description=Gaming VM guard: guest on $BR reaches the internet only (nft table inet gaming_guard)
After=network-pre.target
Before=network-online.target libvirtd.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $GUARD_NFT
# The file replaces the table atomically, so a reload has no unguarded gap (restart has a short one).
ExecReload=/usr/sbin/nft -f $GUARD_NFT
ExecStop=/usr/sbin/nft delete table inet gaming_guard

[Install]
WantedBy=multi-user.target
EOF
        echo "  wrote $GUARD_NFT and /etc/systemd/system/$GUARD_UNIT"
    else
        echo "  would write $GUARD_NFT, /etc/systemd/system/$GUARD_UNIT and $NET_HOOK"
    fi
    rm -f "$tmp"
    run systemctl daemon-reload
    run systemctl enable "$GUARD_UNIT"
    run systemctl reload-or-restart "$GUARD_UNIT"
    if [[ -e $CHECK ]] && ! grep -q 'check gaming_guard' "$CHECK"; then
        if ((APPLY)); then
            backup "$CHECK"
            grep -qx 'exit $rc' "$CHECK" || die "$CHECK has no 'exit \$rc' line to insert before; add the check by hand"
            sed -i 's/^exit \$rc$/# Gaming VM guard, once gaming-setup.sh has installed it.\nif [ -e \/etc\/systemd\/system\/gaming-guard.service ]; then\n    check gaming_guard virbr-gaming gaming-guard.service\nfi\nexit $rc/' "$CHECK"
            grep -q 'check gaming_guard' "$CHECK" || die "could not add the gaming_guard check to $CHECK"
            sh -n "$CHECK" || die "$CHECK no longer parses; restore it from $BK/files$CHECK"
            echo "  added the gaming_guard check to $CHECK"
        else
            echo "  would add to $CHECK: check gaming_guard virbr-gaming gaming-guard.service (if the unit exists)"
        fi
    fi
    if ((APPLY)); then
        guard_loaded || die "table inet gaming_guard is not loaded after the restart: see journalctl -u $GUARD_UNIT"
        ok "table inet gaming_guard is loaded"
        if [[ -e $CHECK ]]; then
            systemctl start nft-guard-check.service || die "nft-guard-check FAILED: see journalctl -u nft-guard-check"
            ok "nft-guard-check passes"
        fi
    fi
    note "undo: systemctl disable --now $GUARD_UNIT; rm $GUARD_NFT /etc/systemd/system/$GUARD_UNIT $NET_HOOK;"
    note "      systemctl daemon-reload (the check in $CHECK is skipped once the unit file is gone)"
}

# ---------------------------------------------------------------------------------------------
step_network() {
    hdr "network: libvirt NAT network $NET"
    local need=0 x
    if "${VIRSH[@]}" net-info "$NET" >/dev/null 2>&1; then
        ok "$NET is defined"
        [[ $("${VIRSH[@]}" net-info "$NET" | awk '/^Active:/{print $2}') == yes ]] && ok "$NET is active" || { todo "start $NET"; need=1; }
        [[ $("${VIRSH[@]}" net-info "$NET" | awk '/^Autostart:/{print $2}') == no ]] && ok "$NET does not autostart" || { todo "turn off autostart for $NET"; need=1; }
    else
        todo "define $NET ($BR, $SUBNET.0/24, DHCP .100-.199, linux-gaming fixed at $SUBNET.10, no IPv6)"; need=1
        x=$(ip -4 -o addr show | awk -v s="$SUBNET." 'index($4, s) == 1 {print $2}')
        [[ -z $x ]] || die "$SUBNET.0/24 is already in use on $x; pick another subnet"
        ip link show "$BR" >/dev/null 2>&1 && die "a link named $BR already exists"
        ok "$SUBNET.0/24 and $BR are free"
    fi
    if guard_loaded; then ok "table inet gaming_guard is loaded"
    else
        todo "table inet gaming_guard is NOT loaded: run the guard step first"
        ((APPLY)) && die "refusing to create $NET without the guard"
    fi
    ((need)) || return 0
    confirm || return 0
    if ! "${VIRSH[@]}" net-info "$NET" >/dev/null 2>&1; then
        local xml
        xml=$(mktemp)
        cat >"$xml" <<EOF
<network>
  <name>$NET</name>
  <forward mode='nat'><nat><port start='1024' end='65535'/></nat></forward>
  <bridge name='$BR' stp='on' delay='0'/>
  <ip address='$SUBNET.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='$SUBNET.100' end='$SUBNET.199'/>
      <host mac='52:54:00:77:00:10' name='linux-gaming' ip='$SUBNET.10'/>
    </dhcp>
  </ip>
</network>
EOF
        run "${VIRSH[@]}" net-define "$xml"
        rm -f "$xml"
    fi
    # No autostart: the game-mode script starts it on demand after checking the guard .
    run "${VIRSH[@]}" net-autostart --disable "$NET"
    if [[ $("${VIRSH[@]}" net-info "$NET" 2>/dev/null | awk '/^Active:/{print $2}') != yes ]]; then
        run "${VIRSH[@]}" net-start "$NET"
    fi
    if ((APPLY)); then
        ip -4 -o addr show dev "$BR" | grep -q " $SUBNET.1/24 " || die "$BR does not have $SUBNET.1/24"
        ok "$BR is up with $SUBNET.1/24"
        "${VIRSH[@]}" domstate "$WALTER_DOM" | grep -qx running && ok "Walter still running" || note "Walter is not running (this step did not touch it)"
    fi
    note "nothing is attached to $NET until the gaming VM is defined (a later, separate step)"
    note "it does not autostart: after a reboot it stays down until game mode starts it"
    note "undo: virsh net-destroy $NET; virsh net-undefine $NET"
}

# ---------------------------------------------------------------------------------------------
step_storage() {
    hdr "storage: $VMS, preallocated disk image, Bazzite ISO "
    local need=0 avail_k walter_grow_k need_k img_k=0 iso_k=0 vol_bytes sz
    local iso_src=$DL/$ISO_NAME iso_dst=$VMS/$ISO_NAME want_sum want_name do_iso=0
    if [[ -d $VMS ]]; then
        [[ $(modeof "$VMS") == "710 root:kvm" ]] && ok "$(modeof "$VMS") $VMS" || { todo "$(modeof "$VMS") $VMS -> 710 root:kvm"; need=1; }
    else todo "create $VMS (710 root:kvm: QEMU runs as libvirt-qemu, which is in group kvm, not libvirt)"; need=1; fi
    if [[ -e $IMG ]]; then ok "$(modeof "$IMG") $IMG ($(du -h "$IMG" | cut -f1) allocated of $(du -h --apparent-size "$IMG" | cut -f1))"
    else
        todo "create a preallocated ${IMG_GIB} GiB raw image $IMG (600 libvirt-qemu:kvm)"; need=1
        img_k=$((IMG_GIB * 1048576))
    fi
    note "preallocated (fallocate), not sparse: a game disk filling up later can never fill $DATAFS under Walter's"
    note "qcow2. The VM XML uses discard='ignore' so the guest's TRIM does not hand the space back."

    # ISO: the download must be complete, then it is copied, hashed once and signature-checked as root.
    want_sum='' want_name=''
    [[ -r $iso_src-CHECKSUM ]] && read -r want_sum want_name <"$iso_src-CHECKSUM" || true
    if [[ -e $iso_dst && -e $iso_dst.verified ]] && [[ $(cat "$iso_dst.verified") == "$(stat -c %s "$iso_dst") $want_sum" || -z $want_sum ]]; then
        ok "$iso_dst present and verified on copy ($(modeof "$iso_dst"))"
    elif [[ -e $iso_dst ]]; then
        todo "$iso_dst exists but was not verified by this script: it will be replaced"; need=1; do_iso=1
    elif [[ -L $iso_src ]]; then
        die "$iso_src is a symlink; refusing"
    elif [[ -f $iso_src && -n $want_sum && -e $iso_src.sig && -e $DL/bazzite-cosign.pub ]]; then
        [[ $want_sum =~ ^[0-9a-f]{64}$ && $want_name == "$ISO_NAME" ]] || die "$iso_src-CHECKSUM is not '<sha256>  $ISO_NAME'"
        sz=$(stat -c %s "$iso_src")
        vol_bytes=$(( $(od -An -t u4 -j 32848 -N4 "$iso_src") * 2048 ))   # ISO9660 volume size
        if [[ $sz != "$vol_bytes" ]]; then
            todo "the ISO download looks incomplete ($sz of $vol_bytes bytes); re-run this step when it finishes"
        else
            ok "ISO download complete ($((sz / 1048576)) MiB); sha256 and signature are checked on the copy"
            todo "copy the ISO to $iso_dst (644 root:root; not secret, read-only for QEMU)"; need=1; do_iso=1
            iso_k=$((sz / 1024))
        fi
    else
        note "ISO not downloaded yet ($iso_src with -CHECKSUM, .sig and bazzite-cosign.pub); re-run this step later"
    fi

    # Space: what this run still adds, plus a margin, must fit next to Walter's possible growth.
    avail_k=$(df -k --output=avail "$DATAFS" | tail -1)
    walter_grow_k=$("${VIRSH[@]}" domblkinfo "$WALTER_DOM" vda 2>/dev/null \
        | awk '/^Capacity:/{c=$2} /^Allocation:/{a=$2} END{if (c == "") exit 1; g=int((c-a)/1024); print (g > 0 ? g : 0)}') \
        || die "cannot read Walter's disk size (virsh domblkinfo $WALTER_DOM vda)"
    need_k=$((img_k + iso_k + MARGIN_GIB * 1048576))
    note "$DATAFS: $((avail_k / 1048576)) GiB free; Walter's qcow2 can still grow $((walter_grow_k / 1048576)) GiB;"
    note "this run adds $(( (img_k + iso_k) / 1048576 )) GiB and keeps a $MARGIN_GIB GiB margin"
    ((avail_k - walter_grow_k >= need_k)) || die "not enough room on $DATAFS; lower IMG_GIB"
    ((need)) || return 0
    confirm || return 0

    run install -d -o root -g kvm -m 710 "$VMS"
    if [[ ! -e $IMG ]]; then
        run fallocate -l "${IMG_GIB}GiB" "$IMG"
        run chown libvirt-qemu:kvm "$IMG"
        run chmod 600 "$IMG"
    fi
    ((do_iso)) || { note "undo: rm -r $VMS (only while no VM uses it)"; return 0; }
    if ((!APPLY)); then
        echo "  would copy $iso_src to $iso_dst.part (600 root), check the copy's sha256 and cosign"
        echo "  signature against the pinned key, then make it 644 and rename it to $iso_dst"
        note "undo: rm -r $VMS (only while no VM uses it)"; return 0
    fi
    local t sum
    t=$(mktemp -d)
    # Copy the small files first and check the key pin on root's copy.
    install -m 600 "$DL/bazzite-cosign.pub" "$t/pub"; install -m 600 "$iso_src.sig" "$t/sig"
    [[ $(sha256sum "$t/pub" | cut -d' ' -f1) == "$COSIGN_PUB_SHA256" ]] || { rm -r "$t"; die "bazzite-cosign.pub does not match the pinned key"; }
    rm -f "$iso_dst" "$iso_dst.verified"
    echo "  copying and hashing the ISO (9 GB, a minute or two)..."
    install -o root -g root -m 600 "$iso_src" "$iso_dst.part"
    sum=$(nice -n 19 ionice -c 3 sha256sum "$iso_dst.part" | cut -d' ' -f1)
    if [[ $sum != "$want_sum" ]]; then rm -f "$iso_dst.part"; rm -r "$t"; die "ISO sha256 mismatch on the copy; deleted it"; fi
    ok "copy's sha256 matches $ISO_NAME-CHECKSUM"
    printf %s "$sum" | xxd -r -p >"$t/digest"
    base64 -d "$t/sig" >"$t/sig.der"
    if ! openssl pkeyutl -verify -pubin -inkey "$t/pub" -sigfile "$t/sig.der" -in "$t/digest" -pkeyopt digest:sha256 >/dev/null; then
        rm -f "$iso_dst.part"; rm -r "$t"; die "ISO cosign signature does NOT verify against Bazzite's pinned key; deleted the copy"
    fi
    rm -r "$t"
    ok "copy's cosign signature verifies against Bazzite's pinned key"
    chmod 644 "$iso_dst.part"; mv "$iso_dst.part" "$iso_dst"
    echo "$(stat -c %s "$iso_dst") $sum" >"$iso_dst.verified"
    ok "$(modeof "$VMS") $VMS"; ls -ls "$VMS" | sed 's/^/  /'
    note "the ISO can be deleted after the install: rm $iso_dst $iso_dst.verified"
    note "undo: rm -r $VMS (only while no VM uses it)"
}

# ---------------------------------------------------------------------------------------------
step_packages() {
    hdr "packages: vainfo (iGPU HEVC decode check for Moonlight)"
    if command -v vainfo >/dev/null; then ok "vainfo installed"; return 0; fi
    todo "apt install vainfo (mesa-va-drivers and libva2 are already installed)"
    confirm || return 0
    run apt-get install -y --no-install-recommends vainfo
    note "then, as $U: vainfo --display drm --device /dev/dri/renderD128 | grep -i hevc  (want VAEntrypointVLD)"
    note "undo: apt remove vainfo"
}

# ---------------------------------------------------------------------------------------------
# libvirt qemu hook for linux-gaming. libvirt reads hooks when libvirtd starts or reloads.
# This is a guard against mistakes, not a security boundary: anyone in group libvirt is root-equivalent,
# and the hook keys on the domain name and never sees live hotplug.
write_qemu_hook() {
    {
    printf '#!/bin/sh\n# Installed by gaming-setup.sh (hypervisor/gaming). Site values, fixed at install time:\n'
    printf "HOST_CPUS='%s'\nGPU_PCI='%s'\nVMS_DIR='%s'\n" "$HOST_CPUS" "${GPU_PCI[*]}" "$VMS"
    cat <<'EOF'
export HOST_CPUS GPU_PCI VMS_DIR
# Acts only on the domain linux-gaming; every other domain (Walter above all) and every other
# operation exits 0 at once. Must not call the libvirt API.
#   prepare/begin: refuse to start unless the domain XML is inside a strict allow-list (GPU1 only,
#     raw disks under VMS_DIR, gaming-nat with clean-traffic, VNC on a unix socket only, no qemu
#     passthrough, no host files or devices), GPU1 is on vfio-pci, gaming-nat is still the guarded NAT
#     bridge and nft table inet gaming_guard is loaded. With GPU1: every other VM's threads must already
#     be on HOST_CPUS, then host processes are moved onto HOST_CPUS too (the game gets the rest).
#   started/begin: the guest's tap device must be on virbr-gaming (a failure here stops the guest).
#   release/end: give the host all cores back.
[ "$1" = linux-gaming ] || exit 0
UNITS="system.slice user.slice init.scope"
log() { echo "linux-gaming hook: $*" >&2; }
cpus() { for u in $UNITS; do systemctl set-property --runtime "$u" AllowedCPUs="$1" || return 1; done; }
# systemd 255 leaves cpuset.cpus as last written when AllowedCPUs is cleared: write every online
# CPU first, then clear the property.
allcpus() { cpus "$(cat /sys/devices/system/cpu/online)" && cpus ""; }
case "$2/$3" in
  release/end) allcpus || log "could not reset AllowedCPUs"; exit 0 ;;
  prepare/begin|started/begin) ;;
  *) exit 0 ;;
esac
out=$(python3 -I -c '
import os, sys, glob, xml.etree.ElementTree as ET
op = sys.argv[1]
r = ET.parse(sys.stdin).getroot()
bad = []
def no(m): bad.append(m)
QEMU_NS = "{http://libvirt.org/schemas/domain/qemu/"
if any(e.tag.startswith(QEMU_NS) for e in r.iter()): no("qemu namespace (commandline/override)")
if op == "started":
    for i in r.findall("devices/interface"):
        t = i.find("target"); dev = t.get("dev") if t is not None else ""
        m = os.path.realpath("/sys/class/net/%s/master" % dev) if dev else ""
        if os.path.basename(m) != "virbr-gaming": no("interface %s is not on virbr-gaming" % dev)
    if bad: sys.stderr.write("; ".join(bad) + "\n"); sys.exit(1)
    sys.exit(0)
TOP = {"name", "uuid", "title", "description", "metadata", "memory", "currentMemory", "memoryBacking", "vcpu",
       "iothreads", "cputune", "resource", "os", "features", "cpu", "clock", "on_poweroff", "on_reboot",
       "on_crash", "pm", "devices", "seclabel"}
for c in r:
    if c.tag not in TOP: no("element <%s>" % c.tag)
for s in r.findall("seclabel"):
    if s.get("type") != "dynamic": no("seclabel type=%s" % s.get("type"))
for c in r.findall("memoryBacking/*"):
    if c.tag not in ("nosharepages", "locked"): no("memoryBacking <%s>" % c.tag)
for c in r.find("os"):
    if c.tag not in ("type", "loader", "nvram", "boot", "bootmenu", "firmware"): no("os <%s>" % c.tag)
lo = r.find("os/loader")
if lo is None or (lo.text or "").strip() != "/usr/share/OVMF/OVMF_CODE_4M.fd": no("loader is not OVMF_CODE_4M.fd")
nv = r.find("os/nvram")
if nv is not None and (nv.text or "").strip() not in ("", "/var/lib/libvirt/qemu/nvram/linux-gaming_VARS.fd"): no("nvram path")
D = r.find("devices")
ALLOWED = {"emulator", "disk", "controller", "interface", "hostdev", "input", "graphics", "video", "audio",
           "rng", "memballoon", "watchdog"}
gpu = []
for d in D:
    t = d.tag
    if t not in ALLOWED: no("device <%s>" % t); continue
    if t == "emulator" and (d.text or "").strip() != "/usr/bin/qemu-system-x86_64": no("emulator path")
    elif t == "disk":
        s = d.find("source"); f = s.get("file") if s is not None else None
        drv = d.find("driver")
        if d.get("type") != "file" or not f: no("disk type=%s" % d.get("type")); continue
        if not os.path.realpath(f).startswith(os.path.realpath(os.environ["VMS_DIR"]) + "/"): no("disk %s outside %s" % (f, os.environ["VMS_DIR"]))
        if drv is None or drv.get("type") != "raw": no("disk %s is not raw" % f)
        for b in d.findall("backingStore"):
            if len(b) or b.attrib: no("disk %s has a backing store" % f)
        if d.find("dataStore") is not None: no("disk %s has a data store" % f)
    elif t == "interface":
        s = d.find("source"); fr = d.find("filterref")
        if d.get("type") != "network" or s is None or s.get("network") != "gaming-nat": no("interface not on gaming-nat")
        if fr is None or fr.get("filter") != "clean-traffic": no("interface without clean-traffic")
    elif t == "hostdev":
        a = d.find("source/address")
        if d.get("type") != "pci" or d.get("mode") != "subsystem" or a is None: no("non-PCI hostdev"); continue
        bdf = "%04x:%02x:%02x.%x" % tuple(int(a.get(k), 16) for k in ("domain", "bus", "slot", "function"))
        if bdf not in os.environ["GPU_PCI"].split(): no("hostdev " + bdf)
        elif any(x.get("file") for x in d.findall("rom")): no("hostdev %s with a rom file" % bdf)
        else: gpu.append(bdf)
    elif t == "input":
        if d.get("type") not in ("tablet", "keyboard", "mouse") or d.get("bus") not in ("virtio", "ps2", "usb"):
            no("input type=%s bus=%s" % (d.get("type"), d.get("bus")))
    elif t == "graphics":
        ls = d.findall("listen")
        if d.get("type") != "vnc" or d.get("listen") or not ls or any(l.get("type") != "socket" for l in ls):
            no("graphics other than VNC on a unix socket")
    elif t == "audio" and d.get("type") != "none": no("audio type=%s" % d.get("type"))
    elif t == "rng":
        b = d.find("backend")
        if b is None or b.get("model") != "random" or (b.text or "").strip() != "/dev/urandom": no("rng backend")
    elif t == "memballoon" and d.get("model") != "none": no("memballoon model=%s" % d.get("model"))
    elif t == "watchdog" and d.get("model") != "itco": no("watchdog model=%s" % d.get("model"))
# gaming-nat must still be the guarded NAT bridge
try:
    n = ET.parse("/etc/libvirt/qemu/networks/gaming-nat.xml").getroot()
    if n.find("bridge").get("name") != "virbr-gaming" or n.find("forward").get("mode") != "nat":
        no("gaming-nat is no longer NAT on virbr-gaming")
except Exception as e:
    no("cannot read gaming-nat config (%s)" % e)
# With GPU1 the guest gets its cores to itself: every other VM thread must already be on HOST_CPUS.
if gpu:
    def rng(s):
        out = set()
        for p in s.strip().split(","):
            if p: a, _, b = p.partition("-"); out.update(range(int(a), int(b or a) + 1))
        return out
    host = rng(os.environ["HOST_CPUS"])
    for scope in glob.glob("/sys/fs/cgroup/machine.slice/machine-qemu*.scope"):
        if "linux\\x2dgaming" in scope: continue
        for f in glob.glob(scope + "/libvirt/*/cpuset.cpus.effective"):
            if not rng(open(f).read()) <= host:
                no("%s may run on the gaming cores (%s)" % (f.split("/")[-2] + " of " + scope.split("/")[-1], open(f).read().strip()))
                break
if bad:
    sys.stderr.write("; ".join(bad) + "\n"); sys.exit(1)
print(" ".join(gpu))
' "${2%%/*}") || { log "refusing to start (reasons above)"; exit 1; }
[ "$2" = started ] && exit 0
for bdf in $out; do
    drv=$(basename "$(readlink "/sys/bus/pci/devices/$bdf/driver" 2>/dev/null)" 2>/dev/null)
    [ "$drv" = vfio-pci ] || { log "refusing to start: $bdf is on '${drv:-no driver}', not vfio-pci"; exit 1; }
done
/usr/sbin/nft list table inet gaming_guard 2>/dev/null | grep -q -- virbr-gaming \
    || { log "refusing to start: nft table inet gaming_guard is not loaded"; exit 1; }
if [ -n "$out" ]; then
    cpus "$HOST_CPUS" || { allcpus; log "refusing to start: could not set host AllowedCPUs"; exit 1; }
fi
exit 0
EOF
    } >"$1"
}

step_guests() {
    hdr "guests: libvirt-guests shutdown timeout (Walter's ACPI shutdown can take minutes)"
    local f=/etc/default/libvirt-guests cur
    cur=$(sed -n 's/^SHUTDOWN_TIMEOUT=//p' "$f" | tail -1)
    if [[ ${cur:-300} -ge 300 ]]; then ok "SHUTDOWN_TIMEOUT=${cur:-300 (default)}"; return 0; fi
    todo "$f: SHUTDOWN_TIMEOUT=$cur -> 300 (on a host shutdown, guests still running after this are killed)"
    note "ON_SHUTDOWN stays 'shutdown' and ON_BOOT 'ignore': linux-gaming never comes back on its own after a reboot"
    confirm || return 0
    backup "$f"
    run sed -i 's/^SHUTDOWN_TIMEOUT=.*/SHUTDOWN_TIMEOUT=300/' "$f"
    if ((APPLY)); then [[ $(sed -n 's/^SHUTDOWN_TIMEOUT=//p' "$f" | tail -1) == 300 ]] || die "edit did not take"; ok "SHUTDOWN_TIMEOUT=300"; fi
    note "read at host shutdown; no restart needed. undo: restore $f from the backup"
}

step_hook() {
    hdr "hook: libvirt qemu hook for linux-gaming"
    local tmp
    tmp=$(mktemp)
    write_qemu_hook "$tmp"
    sh -n "$tmp" || { rm -f "$tmp"; die "the qemu hook does not parse"; }
    # Every other domain must pass untouched, whatever its XML.
    for d in "$WALTER_DOM" $("${VIRSH[@]}" list --all --name | grep -vx linux-gaming) x; do
        sh "$tmp" "$d" prepare begin - </dev/null || { rm -f "$tmp"; die "the hook would block domain $d"; }
    done
    ok "hook parses and passes every other domain"
    # The three linux-gaming variants against the allow-list. Outside game mode Walter is not pinned to
    # HOST_CPUS, so setup and final must be refused for exactly that reason and nothing else.
    local v x err
    x=$(mktemp)
    ((EUID == 0)) || { note "allow-list self-test needs root (it reads the network config); skipped"; set -- skip; }
    [[ ${1:-} == skip ]] || for v in install setup final; do
        write_vm_xml "$v" "$x"
        err=$(sh "$tmp" linux-gaming prepare begin - <"$x" 2>&1 >/dev/null) && rc=0 || rc=$?
        if [[ $v == install ]]; then
            ((rc == 0)) || [[ $err == *"gaming_guard is not loaded"* && $EUID -ne 0 ]] || { rm -f "$tmp" "$x"; die "hook refuses the install variant: $err"; }
        else
            [[ $err == *"may run on the gaming cores"* && $err != *";"* ]] || [[ $(state_of_walter_pinned) == yes && $rc == 0 ]] \
                || { rm -f "$tmp" "$x"; die "hook result for the $v variant is unexpected: ${err:-accepted}"; }
        fi
        ok "hook on the $v variant: ${err:-accepted}"
    done
    # A real start must leave the host CPUs alone after each test (the tests above exit before setting them).
    rm -f "$x"
    if [[ -e $QEMU_HOOK ]] && cmp -s "$tmp" "$QEMU_HOOK"; then ok "$QEMU_HOOK is current"; rm -f "$tmp"; return 0; fi
    todo "install $QEMU_HOOK"
    note "libvirt notices hook files when libvirtd starts or reloads: afterwards run 'sudo systemctl reload libvirtd'"
    note "(running VMs are not affected)"
    confirm || { rm -f "$tmp"; return 0; }
    backup "$QEMU_HOOK"
    run install -d -o root -g root -m 755 "$(dirname "$QEMU_HOOK")"
    run install -o root -g root -m 755 "$tmp" "$QEMU_HOOK"
    rm -f "$tmp"
    note "undo: rm $QEMU_HOOK; systemctl reload libvirtd"
}

# ---------------------------------------------------------------------------------------------
# linux-gaming domain XML. $1 =
#   install  no GPU, VNC console, the Bazzite ISO; 8 unpinned vCPUs, 16 GiB. Runs while Walter runs normally.
#   setup    GPU1 + VNC console, no ISO: first boot with the card (nvidia-smi, Sunshine, virtual display).
#            Started only through game-mode, like final.
#   final    GPU1 only, no console: played through Moonlight.
write_vm_xml() {
    local mode=$1 mem vcpus hint pins='' hostdevs='' console='' cdrom='' video
    local gpu='' a dom bus slot fn
    for a in "${GPU_PCI[@]}"; do
        IFS=':.' read -r dom bus slot fn <<<"$a"
        gpu+="${gpu:+
}    <hostdev mode='subsystem' type='pci' managed='yes'>
      <source><address domain='0x$dom' bus='0x$bus' slot='0x$slot' function='0x$fn'/></source>
    </hostdev>"
    done
    local vnc="    <graphics type='vnc'><listen type='socket'/></graphics>
    <input type='tablet' bus='virtio'/>
    <input type='keyboard' bus='virtio'/>"
    if [[ $mode == install ]]; then
        # Walter keeps all its cores and RAM during the install: smaller, unpinned, no dedicated-core hint.
        mem=16 vcpus=8 hint='' console=$vnc
        video="    <video><model type='virtio' heads='1' primary='yes'/></video>"
        # The empty disk is not bootable, so OVMF falls through to the ISO; after the install it boots the disk.
        cdrom="    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='$VMS/$ISO_NAME'><seclabel model='dac' relabel='no'/></source>
      <target dev='sda' bus='sata'/>
      <readonly/>
      <boot order='2'/>
    </disk>"
    else
        mem=24 hint="<hint-dedicated state='on'/>" hostdevs=$gpu
        # One vCPU per guest CPU, SMT siblings kept together; emulator and I/O thread on the first two
        # host cores.
        local c0 c1 n=0 hp
        while read -r c0 c1; do
            pins+="    <vcpupin vcpu='$n' cpuset='$c0'/><vcpupin vcpu='$((n + 1))' cpuset='${c1:?GAMING_GUEST_CPUS needs whole SMT cores}'/>
"
            n=$((n + 2))
        done < <(smt_pairs "$GUEST_CPUS")
        vcpus=$n
        mapfile -t hp < <(smt_pairs "$HOST_CPUS" | head -2 | tr ' ' ,)
        ((vcpus >= 2 && ${#hp[@]} == 2)) || die "GAMING_GUEST_CPUS / GAMING_HOST_CPUS: need guest cores and two host cores"
        pins="  <cputune>
$pins    <emulatorpin cpuset='${hp[0]}'/>
    <iothreadpin iothread='1' cpuset='${hp[1]}'/>
  </cputune>"
        if [[ $mode == setup ]]; then
            console=$vnc video="    <video><model type='virtio' heads='1' primary='yes'/></video>"
        else
            video="    <video><model type='none'/></video>"
        fi
    fi
    local gtitle=${GPU_PCI[0]#*:}; gtitle=${gtitle%.*}
    local topo="<topology sockets='1' dies='1' clusters='1' cores='$((vcpus / 2))' threads='2'/>"
    cat >"$2" <<EOF
<domain type='kvm'>
  <name>linux-gaming</name>${UUID:+
  <uuid>$UUID</uuid>}
  <title>Bazzite gaming VM on GPU1 ($gtitle.x) [$mode]</title>
  <memory unit='GiB'>$mem</memory>
  <currentMemory unit='GiB'>$mem</currentMemory>
  <memoryBacking><nosharepages/></memoryBacking>
  <vcpu placement='static'>$vcpus</vcpu>
  <iothreads>1</iothreads>
$pins
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <loader readonly='yes' type='pflash'>/usr/share/OVMF/OVMF_CODE_4M.fd</loader>
    <nvram template='/usr/share/OVMF/OVMF_VARS_4M.fd'>/var/lib/libvirt/qemu/nvram/linux-gaming_VARS.fd</nvram>
  </os>
  <features>
    <acpi/><apic/><vmport state='off'/>
    <kvm>$hint<poll-control state='on'/><pv-ipi state='on'/></kvm>
  </features>
  <cpu mode='host-passthrough' check='none' migratable='off'>
    $topo
    <cache mode='passthrough'/>
    <maxphysaddr mode='passthrough'/>
    <feature policy='require' name='topoext'/>
    <feature policy='require' name='invtsc'/>
  </cpu>
  <clock offset='utc'>
    <timer name='kvmclock' present='yes'/><timer name='rtc' tickpolicy='catchup'/>
    <timer name='pit' tickpolicy='delay'/><timer name='hpet' present='no'/>
  </clock>
  <on_poweroff>destroy</on_poweroff><on_reboot>restart</on_reboot><on_crash>destroy</on_crash>
  <pm><suspend-to-mem enabled='no'/><suspend-to-disk enabled='no'/></pm>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='raw' cache='none' io='io_uring' discard='ignore' iothread='1' queues='4'/>
      <source file='$IMG'/>
      <target dev='vda' bus='virtio'/>
      <boot order='1'/>
    </disk>
$cdrom
$console
$video
    <interface type='network'>
      <mac address='52:54:00:77:00:10'/>
      <source network='$NET'/>
      <model type='virtio'/><driver name='vhost' queues='4'/>
      <filterref filter='clean-traffic'><parameter name='IP' value='$SUBNET.10'/></filterref>
    </interface>
$hostdevs
    <rng model='virtio'><backend model='random'>/dev/urandom</backend></rng>
    <memballoon model='none'/>
  </devices>
</domain>
EOF
}

define_vm() {   # $1 = install | setup | final
    local mode=$1 xml state
    [[ $mode == install ]] && CUR=vm || CUR=vm-$mode
    hdr "$CUR: define linux-gaming ($mode variant)"
    [[ -e $IMG ]] || die "$IMG not found: run the storage step first"
    [[ $mode != install || -e $VMS/$ISO_NAME ]] || die "$VMS/$ISO_NAME not found: run the storage step first"
    xml=$(mktemp)
    # A redefine must keep the domain's uuid (libvirt refuses a new uuid for an existing name); the same
    # uuid and NVRAM file also keep the UEFI boot entry the installer wrote.
    local UUID=
    UUID=$("${VIRSH[@]}" domuuid linux-gaming 2>/dev/null || true)
    write_vm_xml "$mode" "$xml"
    "${VIRSH[@]}" domxml-to-native qemu-argv --xml "$xml" >/dev/null || { rm -f "$xml"; die "libvirt rejects the $mode XML"; }
    ok "libvirt parses the $mode XML"
    if "${VIRSH[@]}" dominfo linux-gaming >/dev/null 2>&1; then
        state=$("${VIRSH[@]}" domstate linux-gaming)
        [[ $state == "shut off" ]] || { rm -f "$xml"; die "linux-gaming is '$state': shut it down first"; }
        if "${VIRSH[@]}" dumpxml --inactive linux-gaming | grep -q "\[$mode\]</title>"; then
            ok "linux-gaming is defined as the $mode variant"; rm -f "$xml"; return 0
        fi
        todo "redefine linux-gaming as the $mode variant"
    else
        todo "define linux-gaming ($mode variant)"
    fi
    case $mode in
        install) note "install variant: no GPU, 16 GiB, 8 vCPUs, VNC console on a unix socket, boots the Bazzite ISO;"
                 note "Walter is not touched. Console: virt-manager (qemu:///system), or"
                 note "virt-viewer --attach -c qemu:///system linux-gaming" ;;
        setup)   note "setup variant: GPU1 + VNC console, no ISO; start it only through game-mode" ;;
        final)   note "final variant: GPU1, 24 GiB, no console; start it only through game-mode" ;;
    esac
    note "autostart stays off; the hook refuses to start it outside its allow-list or without the guard"
    confirm || { rm -f "$xml"; return 0; }
    if ((APPLY)) && "${VIRSH[@]}" dominfo linux-gaming >/dev/null 2>&1; then
        install -d -m 700 "$BK/files"; "${VIRSH[@]}" dumpxml --inactive linux-gaming >"$BK/files/linux-gaming.xml"
        echo "  backup: current linux-gaming XML -> $BK/files/linux-gaming.xml"
    fi
    run "${VIRSH[@]}" define "$xml"
    rm -f "$xml"
    if ((APPLY)); then
        [[ $("${VIRSH[@]}" dominfo linux-gaming | awk '/^Autostart:/{print $2}') == disable ]] || run "${VIRSH[@]}" autostart --disable linux-gaming
        ok "linux-gaming defined ($mode), autostart off"
    fi
    note "undo: virsh undefine --nvram linux-gaming"
}
step_vm() { define_vm install; }
step_vm-setup() { define_vm setup; }
step_vm-final() { define_vm final; }

# ---------------------------------------------------------------------------------------------
for s in "${STEPS[@]}"; do CUR=$s; "step_$s"; done
CUR=
hdr "done"
if ((APPLY)); then echo "  log and backups: $BK"; else echo "  plan only; nothing was changed. Add --apply to make the changes."; fi
