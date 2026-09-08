#!/usr/bin/env bash
# One-time host bootstrap. Run as root (deploy.sh does this over sudo).
#
#   setup-host.sh --role manager [--lan-cidr 10.0.0.0/16]
#   setup-host.sh --role agent   [--allow-from 10.0.0.10]
#
# Idempotent: safe to re-run. Creates the unprivileged ghrunner account, gives
# it lingering + cgroup delegation so rootless resource limits and boot-start
# both work, installs the runtime deps, and opens exactly one firewall port.
set -euo pipefail

ROLE=""
LAN_CIDR=""
ALLOW_FROM=""
RUNNER_USER="ghrunner"

die() { echo "setup-host: $*" >&2; exit 1; }
log() { echo "[setup-host] $*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --role)       ROLE="${2:?}"; shift 2 ;;
        --lan-cidr)   LAN_CIDR="${2:?}"; shift 2 ;;
        --allow-from) ALLOW_FROM="${2:?}"; shift 2 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ $EUID -eq 0 ]] || die "must run as root"
[[ "$ROLE" == "manager" || "$ROLE" == "agent" ]] || die "--role must be manager or agent"

# --- packages -------------------------------------------------------------
# Rocky 9 ships python3.11 from AppStream; Rocky 10 ships 3.12 as python3.
# Pick whichever >=3.11 interpreter the distro provides.
PY_PKG=""
for cand in python3.11 python3.12 python3.13; do
    if dnf -q list --available "$cand" >/dev/null 2>&1 || rpm -q "$cand" >/dev/null 2>&1; then
        PY_PKG="$cand"; break
    fi
done
[[ -n "$PY_PKG" ]] || PY_PKG="python3"   # fall back to the platform python
log "installing packages (python: $PY_PKG)"
dnf install -y --setopt=install_weak_deps=False \
    podman crun fuse-overlayfs passt \
    "$PY_PKG" "${PY_PKG}-pip" cloud-utils-growpart \
    firewalld util-linux jq curl openssl >/dev/null 2>&1 || \
dnf install -y --setopt=install_weak_deps=False \
    podman crun fuse-overlayfs passt "$PY_PKG" cloud-utils-growpart \
    firewalld util-linux jq curl openssl >/dev/null
systemctl enable --now firewalld >/dev/null 2>&1 || true

# Record the interpreter path for deploy.sh (which builds the venvs).
PY_BIN="$(command -v "$PY_PKG" || command -v python3.11 || command -v python3.12 || command -v python3)"
echo "$PY_BIN" > /etc/sp-runner-python
log "python interpreter: $PY_BIN"

# --- ghrunner account ----------------------------------------------------
if ! id "$RUNNER_USER" >/dev/null 2>&1; then
    log "creating user $RUNNER_USER"
    useradd --system --create-home --shell /bin/bash \
        --comment "SP runner farm" "$RUNNER_USER"
fi
runner_home="$(getent passwd "$RUNNER_USER" | cut -d: -f6)"
runner_uid="$(id -u "$RUNNER_USER")"

# So the agent's `journalctl --user -u sp-runner@N` (the log viewer's journal
# pane) can actually read the journal.
if getent group systemd-journal >/dev/null && ! id -nG "$RUNNER_USER" | tr ' ' '\n' | grep -qx systemd-journal; then
    log "adding $RUNNER_USER to systemd-journal"
    usermod -aG systemd-journal "$RUNNER_USER"
fi

# subuid/subgid for rootless containers. shadow-utils auto-assigns a free range
# at useradd time on Rocky 9; only fall back to an explicit high range (well
# clear of the existing farm-user-*/sperp-* allocations) if it did not.
if ! grep -q "^${RUNNER_USER}:" /etc/subuid; then
    log "allocating subuid/subgid range explicitly"
    last_end=$(awk -F: '{ if ($2+$3 > max) max=$2+$3 } END { print (max ? max : 900000) }' /etc/subuid)
    start=$(( last_end > 900000 ? last_end : 900000 ))
    usermod --add-subuids "${start}-$((start+65535))" --add-subgids "${start}-$((start+65535))" "$RUNNER_USER"
fi
grep -q "^${RUNNER_USER}:" /etc/subuid || die "subuid allocation failed for $RUNNER_USER"

# Linger: user services (agent, manager, runner slots) start at boot with no
# login session. This is the single most important line in the file.
log "enabling linger for $RUNNER_USER"
loginctl enable-linger "$RUNNER_USER"

# cgroup v2 delegation so rootless podman can honour --memory / --cpus.
# Without the `memory` controller delegated to the user manager, crun fails with
# "the memory limit could be too low / cgroup.controllers: No such file".
deleg=/etc/systemd/system/user@.service.d/50-sp-delegate.conf
want=$'[Service]\nDelegate=cpu cpuset io memory pids\n'
if [[ "$(cat "$deleg" 2>/dev/null)" != "$want" ]]; then
    log "delegating cpu/memory/pids/io cgroup controllers to user managers"
    install -d "$(dirname "$deleg")"
    printf '%s' "$want" > "$deleg"
    systemctl daemon-reload
fi
# The drop-in only takes effect when user@<uid>.service (re)starts. Do it now so
# the very first slot works — the agent/manager (if already up) auto-restart.
runner_uid_now="$(id -u "$RUNNER_USER" 2>/dev/null || true)"
if [[ -n "$runner_uid_now" ]]; then
    ctrl="/sys/fs/cgroup/user.slice/user-${runner_uid_now}.slice/user@${runner_uid_now}.service/cgroup.subtree_control"
    if [[ ! -f "$ctrl" ]] || ! grep -q memory "$ctrl" 2>/dev/null; then
        log "restarting user@${runner_uid_now}.service to apply delegation"
        systemctl restart "user@${runner_uid_now}.service" || true
        sleep 2
    fi
fi

# --- config + app dirs -------------------------------------------------
log "preparing directories under $runner_home"
runuser -u "$RUNNER_USER" -- mkdir -p \
    "$runner_home/.config/sp-runner" \
    "$runner_home/.config/systemd/user" \
    "$runner_home/.local/bin" \
    "$runner_home/sp-runner-agent"
chmod 700 "$runner_home/.config/sp-runner"
[[ "$ROLE" == "manager" ]] && runuser -u "$RUNNER_USER" -- mkdir -p "$runner_home/sp-runner-manager"

# --- firewall ----------------------------------------------------------
zone="$(firewall-cmd --get-default-zone)"
add_port() {
    # $2 may be empty (open to all) or a comma/space-separated list of CIDRs.
    local port="$1" srcs="${2//,/ }"
    if [[ -n "$srcs" ]]; then
        local src
        for src in $srcs; do
            firewall-cmd --permanent --zone="$zone" \
                --add-rich-rule="rule family=ipv4 source address=${src} port port=${port} protocol=tcp accept" >/dev/null
            log "firewall: ${port}/tcp from ${src}"
        done
    else
        firewall-cmd --permanent --zone="$zone" --add-port="${port}/tcp" >/dev/null
        log "firewall: ${port}/tcp (any)"
    fi
}
if [[ "$ROLE" == "manager" ]]; then
    add_port 8080 "$LAN_CIDR"      # empty LAN_CIDR => open to all; pass --lan-cidr to scope
else
    [[ -n "$ALLOW_FROM" ]] || log "WARNING: agent port 8181 opening to ANY source; pass --allow-from <manager-ip>"
    add_port 8181 "$ALLOW_FROM"
fi
firewall-cmd --reload >/dev/null

# --- linger runtime dir now (so deploy can 'systemctl --user' immediately) ---
loginctl enable-linger "$RUNNER_USER"
systemctl start "user@${runner_uid}.service" 2>/dev/null || true

cat <<EOF

[setup-host] done. role=$ROLE user=$RUNNER_USER home=$runner_home uid=$runner_uid
Next: deploy.sh pushes the app files and unit definitions, then enables the
      $( [[ "$ROLE" == manager ]] && echo "manager + agent" || echo "agent" ) user services.
EOF
