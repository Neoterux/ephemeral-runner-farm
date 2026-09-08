#!/usr/bin/env bash
# Guest-side half of a disk grow. Run AFTER the VM's virtual disk has been
# enlarged in vSphere (that part is manual and outside this project's scope).
#
#   sudo ./grow-home.sh [--apply]   # default: dry run
#
# Assumes the standard Rocky layout on this VM: /dev/sda, partition 3 is the
# LVM PV, VG "rl", LV "home" is XFS. Each step is idempotent.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

DISK="/dev/sda"
PART_NUM=3
PART="${DISK}${PART_NUM}"
VG="rl"
LV="/dev/${VG}/home"

command -v growpart >/dev/null || {
    echo "growpart missing — install with: dnf install -y cloud-utils-growpart" >&2
    [[ $APPLY -eq 1 ]] && exit 1
}

echo "== before =="
lsblk "$DISK"
df -h /home

run() { echo "+ $*"; [[ $APPLY -eq 1 ]] && "$@"; }

echo; echo "== steps =="
# 1. Make the kernel notice the larger disk.
for r in "/sys/class/block/$(basename "$DISK")/device/rescan" \
         /sys/class/scsi_disk/*/device/rescan; do
    [[ -w "$r" ]] && run bash -c "echo 1 > '$r'"
done
run udevadm settle

# 2. Grow partition, PV, LV, filesystem.
run growpart "$DISK" "$PART_NUM" || echo "  (growpart: nothing to do or already max)"
run partprobe "$DISK" || true
run pvresize "$PART"
run lvextend -l +100%FREE "$LV" || echo "  (lvextend: no free extents)"
run xfs_growfs /home

echo; echo "== after =="
[[ $APPLY -eq 1 ]] && { lsblk "$DISK"; df -h /home; } || echo "dry run — re-run with --apply"
