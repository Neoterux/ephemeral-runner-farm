#!/usr/bin/env bash
# Reclaim the never-activated 16 GB rl-swap LV: shrink it to a 4 GB working
# cushion and hand the freed ~12 GB to /home. No downtime, no hypervisor.
#
#   sudo ./01-reclaim-swap.sh            # dry run: prints the plan
#   sudo ./01-reclaim-swap.sh --apply
#
# Safe to re-run: if swap is already 4 GB and the VG has no free extents it
# reports "nothing to do".
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

VG="rl"
SWAP_LV="/dev/${VG}/swap"
HOME_LV="/dev/${VG}/home"
KEEP_SWAP="4G"

command -v lvs >/dev/null || { echo "LVM tools missing" >&2; exit 1; }
[[ -e "$SWAP_LV" && -e "$HOME_LV" ]] || { echo "expected $SWAP_LV and $HOME_LV" >&2; exit 1; }

swap_size_g="$(lvs --noheadings --units g -o lv_size "$SWAP_LV" | tr -dc '0-9.')"
vg_free_g="$(vgs --noheadings --units g -o vg_free "$VG" | tr -dc '0-9.')"
home_before="$(df -h --output=size,used,avail /home | tail -1)"

echo "== current =="
echo "  swap LV : ${swap_size_g} GiB   (active: $(swapon --show=NAME --noheadings | grep -q "$SWAP_LV" && echo yes || echo no))"
echo "  VG free : ${vg_free_g} GiB"
echo "  /home   : ${home_before}   (size/used/avail)"
echo
echo "== plan =="
echo "  swapoff ${SWAP_LV}                 (if active)"
echo "  lvreduce --yes -L ${KEEP_SWAP} ${SWAP_LV}"
echo "  mkswap ${SWAP_LV}  &&  swapon ${SWAP_LV}"
echo "  lvextend -l +100%FREE ${HOME_LV}"
echo "  xfs_growfs /home"

if [[ "${swap_size_g%.*}" -le 4 && "${vg_free_g%.*}" -eq 0 ]]; then
    echo; echo "nothing to do — swap already trimmed and VG fully allocated."
    exit 0
fi

if [[ $APPLY -eq 0 ]]; then
    echo; echo "dry run. re-run with --apply to execute."
    exit 0
fi

echo; echo "== applying =="
if swapon --show=NAME --noheadings | grep -q "$SWAP_LV"; then
    swapoff "$SWAP_LV"
fi
# Also drop the fstab-referenced device if named differently (rl-swap mapper).
swapoff /dev/mapper/${VG}-swap 2>/dev/null || true

lvreduce --yes -L "$KEEP_SWAP" "$SWAP_LV"
mkswap "$SWAP_LV"
swapon "$SWAP_LV"

lvextend -l +100%FREE "$HOME_LV"
xfs_growfs /home

echo
echo "== result =="
swapon --show
df -h /home
free -h
echo "done. fstab already references ${VG}-swap; swap will re-activate on boot."
