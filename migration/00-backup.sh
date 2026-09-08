#!/usr/bin/env bash
# Snapshot everything the later migration steps could break, so any of them can
# be rolled back. Run as root BEFORE the other migration scripts.
#
#   sudo ./00-backup.sh
#
# Produces /root/sp-runner-farm-backup-<timestamp>.tar.gz
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

stamp="$(date +%Y%m%d-%H%M%S)"
work="$(mktemp -d)"
out="/root/sp-runner-farm-backup-${stamp}.tar.gz"
trap 'rm -rf "$work"' EXIT

echo "[backup] systemd units"
mkdir -p "$work/systemd"
cp -a /etc/systemd/system/actions.runner.*.service "$work/systemd/" 2>/dev/null || true
cp -a /etc/systemd/system/actions.runner.*.service.d "$work/systemd/" 2>/dev/null || true
systemctl list-units --all 'actions.runner.*' --no-pager > "$work/systemd/unit-state.txt" 2>/dev/null || true
systemctl list-unit-files 'actions.runner.*' --no-pager >> "$work/systemd/unit-state.txt" 2>/dev/null || true

echo "[backup] runner identities (.runner / .credentials)"
mkdir -p "$work/runners"
for home in /home/*; do
    u="$(basename "$home")"
    [[ -d "$home/runner" ]] || continue
    mkdir -p "$work/runners/$u"
    for f in .runner .credentials .credentials_rsaparams .env .path; do
        [[ -e "$home/runner/$f" ]] && cp -a "$home/runner/$f" "$work/runners/$u/" || true
    done
    ls -la "$home/runner" > "$work/runners/$u/listing.txt" 2>/dev/null || true
done

echo "[backup] storage layout"
mkdir -p "$work/storage"
cp -a /etc/fstab "$work/storage/"
{ lsblk -f; echo; pvs; echo; vgs; echo; lvs; echo; df -hT; echo; free -h; swapon --show; } \
    > "$work/storage/layout.txt" 2>&1 || true
vgcfgbackup -f "$work/storage/vg-%s.cfg" >/dev/null 2>&1 || true

echo "[backup] passwd/group (for userdel rollback reference)"
getent passwd | grep -E 'runner|ghrunner' > "$work/accounts.txt" 2>/dev/null || true
cp -a /etc/subuid /etc/subgid "$work/" 2>/dev/null || true

tar czf "$out" -C "$work" .
chmod 600 "$out"
echo "[backup] wrote $out"
tar tzf "$out" | sed 's/^/  /'
