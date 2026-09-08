#!/usr/bin/env bash
# Retire a legacy bare-metal GitHub Actions runner setup so the host is a clean
# slate for the ephemeral podman farm. Fully self-discovering — nothing is
# hardcoded. Run as root.
#
#   sudo ./02-retire-bare-metal.sh                 # dry run: shows what it would do
#   sudo ./02-retire-bare-metal.sh --apply
#   sudo ./02-retire-bare-metal.sh --apply --keep alice --keep bob
#
# Handles:
#   * `actions.runner.*.service` units      -> stop, disable, remove unit file
#   * leftover cgroup slices                -> stop
#   * orphan drop-in dirs referencing units that no longer exist
#   * the runner's local account            -> userdel -r  (unless --keep'd)
#   * stale self-update leftovers (bin.* / externals.* beside the live copy)
#   * subordinate uid/gid ranges left behind by userdel
#
# GitHub-side registrations are NOT touched here (they need a token). Delete
# offline runners from the org/repo Runners page, or use 03-retire-runner.sh
# which deregisters properly.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

APPLY=0
KEEP=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --keep)  KEEP+=("${2:?}"); shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done
run() { echo "+ $*"; [[ $APPLY -eq 1 ]] && "$@" || true; }
kept() { local u; for u in "${KEEP[@]:-}"; do [[ "$u" == "$1" ]] && return 0; done; return 1; }

# --- discover runner units + their accounts --------------------------------
declare -A RUNNER_USERS=()
UNIT_FILES=()
while IFS= read -r f; do
    [[ -e "$f" ]] || continue
    UNIT_FILES+=("$f")
    u="$(sed -nE 's/^User=//p' "$f" | head -1)"
    [[ -n "$u" ]] && RUNNER_USERS["$u"]=1
done < <(ls /etc/systemd/system/actions.runner.*.service 2>/dev/null || true)

echo "== runner units =="
for f in "${UNIT_FILES[@]:-}"; do
    [[ -e "$f" ]] || continue
    unit="$(basename "$f")"
    run systemctl stop "$unit"
    run systemctl disable "$unit"
    run rm -f "$f"
done
[[ ${#UNIT_FILES[@]:-0} -gt 0 ]] && run systemctl reset-failed || echo "  (none)"

echo "== leftover cgroup slices =="
while read -r slice _; do
    [[ "$slice" == *actions.runner* ]] || continue
    run systemctl stop "$slice"
done < <(systemctl list-units --no-pager --plain 'system-actions.runner.*' 2>/dev/null || true)

echo "== orphan drop-in dirs =="
for d in /etc/systemd/system/actions.runner.*.service.d; do
    [[ -d "$d" ]] || continue
    base="$(basename "$d" .d)"
    if systemctl cat "$base" >/dev/null 2>&1; then
        echo "  KEEP $d — $base still exists"
    else
        run rm -rf "$d"
    fi
done

echo "== runner accounts =="
for u in "${!RUNNER_USERS[@]}"; do
    kept "$u" && { echo "  KEEP $u (--keep)"; continue; }
    id "$u" >/dev/null 2>&1 || { echo "  $u: absent"; continue; }
    pgrep -u "$u" >/dev/null 2>&1 && { echo "  SKIP $u — has live processes"; continue; }
    run loginctl disable-linger "$u" 2>/dev/null || true
    run userdel -r "$u"
    run sed -i "/^${u}:/d" /etc/subuid
    run sed -i "/^${u}:/d" /etc/subgid
done

echo "== stale self-update leftovers =="
for home in /home/*; do
    rd="$home/runner"
    [[ -d "$rd" ]] || continue
    kept "$(basename "$home")" && continue
    live_bin="$(basename "$(readlink -f "$rd/bin" 2>/dev/null)" 2>/dev/null || true)"
    live_ext="$(basename "$(readlink -f "$rd/externals" 2>/dev/null)" 2>/dev/null || true)"
    for stale in "$rd"/bin.* "$rd"/externals.*; do
        [[ -e "$stale" ]] || continue
        b="$(basename "$stale")"
        if [[ "$b" == "$live_bin" || "$b" == "$live_ext" ]]; then
            echo "  KEEP $stale (current)"
        else
            run rm -rf "$stale"
        fi
    done
done

if [[ $APPLY -eq 1 ]]; then
    systemctl daemon-reload
    echo; echo "done. disk:"; df -h /home / 2>/dev/null
else
    echo; echo "dry run — re-run with --apply"
fi
