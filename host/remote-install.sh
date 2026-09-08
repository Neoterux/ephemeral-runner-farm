#!/usr/bin/env bash
# Run as root by deploy.sh (step 3). Placeholders @@STAGE@@ @@USER@@ @@TARGET@@
# are substituted by deploy.sh before upload. Not meant to be run by hand.
set -euo pipefail

STAGE="@@STAGE@@"
RUNNER_USER="@@USER@@"
TARGET="@@TARGET@@"

HOME_DIR="$(getent passwd "$RUNNER_USER" | cut -d: -f6)"
UID_N="$(id -u "$RUNNER_USER")"
RUNTIME="/run/user/$UID_N"

T0=$(date +%s)
say()  { printf '[install %3ds] %s\n' "$(( $(date +%s) - T0 ))" "$*"; }
step() { printf '\n[install %3ds] ==> %s\n' "$(( $(date +%s) - T0 ))" "$*"; }
warn() { printf '[install %3ds]  !! %s\n' "$(( $(date +%s) - T0 ))" "$*" >&2; }

inst() { install -o "$RUNNER_USER" -g "$RUNNER_USER" -m "$1" "$2" "$3"; }
# runuser keeps the caller's CWD; this script runs from root's shell in
# /home/neoterux (0700), which ghrunner cannot chdir into. Force a safe CWD.
asuser() { runuser -u "$RUNNER_USER" -- /usr/bin/env -C "$STAGE" XDG_RUNTIME_DIR="$RUNTIME" \
           DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME/bus" "$@"; }
sctl() { asuser systemctl --user "$@"; }
cd /tmp

say "host: $TARGET   user: $RUNNER_USER ($UID_N)   home: $HOME_DIR"

# --- agent files ---
step "installing agent files"
mkdir -p "$HOME_DIR/sp-runner-agent"
rsync -a --delete --exclude .venv "$STAGE/agent/" "$HOME_DIR/sp-runner-agent/"
chown -R "$RUNNER_USER:$RUNNER_USER" "$HOME_DIR/sp-runner-agent"
inst 600 "$STAGE/_agent.toml" "$HOME_DIR/.config/sp-runner/agent.toml"
for b in sp-runner-prestart sp-runner-run sp-runner-stop sp-runner-gc sp-runner-agent-run; do
    inst 700 "$STAGE/host/bin/$b" "$HOME_DIR/.local/bin/$b"
done
for u in sp-runner@.service sp-runner-agent.service sp-runner-gc.service sp-runner-gc.timer; do
    inst 644 "$STAGE/host/systemd/$u" "$HOME_DIR/.config/systemd/user/$u"
done
say "  agent.toml, 5 helper scripts, 4 unit files"

# prestart needs the agent URL + the host id (runner names are <host_id>-slot-<n>)
printf 'AGENT_URL=https://127.0.0.1:8181\nHOST_ID=%s\n' "$TARGET" \
    > "$HOME_DIR/.config/sp-runner/agent.env"
chown "$RUNNER_USER:$RUNNER_USER" "$HOME_DIR/.config/sp-runner/agent.env"
chmod 600 "$HOME_DIR/.config/sp-runner/agent.env"

# self-signed cert for the agent's HTTPS listener (pinned by the manager)
if [ ! -f "$HOME_DIR/.config/sp-runner/agent.crt" ]; then
    step "generating agent TLS cert (self-signed, 10y)"
    asuser openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$HOME_DIR/.config/sp-runner/agent.key" \
        -out    "$HOME_DIR/.config/sp-runner/agent.crt" \
        -subj "/CN=sp-runner-agent/O=$TARGET" 2>/dev/null
    chmod 600 "$HOME_DIR/.config/sp-runner/agent.key" "$HOME_DIR/.config/sp-runner/agent.crt"
else
    say "agent TLS cert already present — keeping it"
fi

# --- manager files (only where deploy staged _manager.toml) ---
HAVE_MANAGER=0
if [ -f "$STAGE/_manager.toml" ]; then
    HAVE_MANAGER=1
    step "installing manager files (this host is role=manager)"
    mkdir -p "$HOME_DIR/sp-runner-manager"
    rsync -a --delete --exclude .venv "$STAGE/manager/" "$HOME_DIR/sp-runner-manager/"
    chown -R "$RUNNER_USER:$RUNNER_USER" "$HOME_DIR/sp-runner-manager"
    inst 600 "$STAGE/_manager.toml" "$HOME_DIR/.config/sp-runner/manager.toml"
    if [ -f "$STAGE/_github-app.pem" ]; then
        inst 600 "$STAGE/_github-app.pem" "$HOME_DIR/.config/sp-runner/github-app.pem"
        say "  GitHub App private key installed"
    else
        warn "no github-app.pem staged — UI runs but runner registration is disabled"
    fi
    inst 644 "$STAGE/host/systemd/sp-runner-manager.service" "$HOME_DIR/.config/systemd/user/sp-runner-manager.service"
fi

# --- python venvs ---
PY_BIN="$(cat /etc/sp-runner-python 2>/dev/null || command -v python3.11 || command -v python3.12 || command -v python3)"
say "python interpreter: $PY_BIN ($("$PY_BIN" --version 2>&1))"
build_venv() {
    local d="$1" name vlog; name="$(basename "$d")"; vlog="/tmp/sp-venv-$name.log"
    if [ -x "$d/.venv/bin/python" ]; then
        say "  [$name] venv exists"
    else
        say "  [$name] creating venv…"
        asuser "$PY_BIN" -m venv "$d/.venv"
    fi
    say "  [$name] pip install -r requirements.txt (30–90s)…"
    if asuser "$d/.venv/bin/pip" install --upgrade pip -r "$d/requirements.txt" > "$vlog" 2>&1; then
        grep -E 'Successfully installed|Requirement already satisfied: (fastapi|uvicorn)' "$vlog" | tail -3 | sed 's/^/      /'
        say "  [$name] dependencies ready"
    else
        warn "[$name] pip install FAILED — $vlog:"; tail -15 "$vlog" | sed 's/^/      /'
        return 1
    fi
}
step "building python environments"
build_venv "$HOME_DIR/sp-runner-agent"
[ "$HAVE_MANAGER" = 1 ] && build_venv "$HOME_DIR/sp-runner-manager"

# --- runner image ---
step "building the ephemeral runner image (first run pulls ~2 GB base — be patient)"
IMG_LOG=/tmp/sp-image-build.log
set +e
asuser bash "$STAGE/containerfile/build.sh" 2>&1 | tee "$IMG_LOG" \
    | grep --line-buffered -E '^(STEP |COMMIT|--> |Copying blob|Copying config|Writing manifest|Storing|Successfully|\[build\])' \
    | sed 's/^/      /'
img_rc=${PIPESTATUS[0]}
set -e
if [ "$img_rc" = 0 ]; then
    say "runner image built:"
    asuser podman images localhost/sp-runner 2>/dev/null | sed 's/^/      /' || true
else
    warn "runner image build FAILED (exit $img_rc) — full log at $IMG_LOG on $TARGET:"
    tail -20 "$IMG_LOG" | sed 's/^/      /' || true
fi

# --- agent fingerprint ---
step "recording agent TLS fingerprint"
FPR="$(openssl x509 -in "$HOME_DIR/.config/sp-runner/agent.crt" -noout -fingerprint -sha256 \
       | sed 's/^.*=//' | tr -d ':' | tr 'A-F' 'a-f' | sed 's/^/sha256:/')"
printf '%s\n' "$FPR" > "$STAGE/_fingerprint"
chmod 644 "$STAGE/_fingerprint"
say "  $FPR"

# If the manager lives on THIS host, pin its own agent right now so it does not
# have to be restarted a second time from deploy.sh.
MGR_TOML="$HOME_DIR/.config/sp-runner/manager.toml"
if [ "$HAVE_MANAGER" = 1 ] && [ -f "$MGR_TOML" ]; then
    python3 - "$MGR_TOML" "$TARGET" "$FPR" <<'PY'
import re, sys
path, hid, fpr = sys.argv[1:4]
t = open(path).read()
parts = re.split(r'(?=^\[\[hosts\]\])', t, flags=re.M)
for i, b in enumerate(parts):
    if re.search(rf'^id = "{re.escape(hid)}"\s*$', b, flags=re.M):
        parts[i] = re.sub(r'agent_tls_fingerprint = "[^"]*"', f'agent_tls_fingerprint = "{fpr}"', b)
open(path, "w").write("".join(parts))
PY
    chown "$RUNNER_USER:$RUNNER_USER" "$MGR_TOML"
    say "  pinned into this host's manager.toml"
fi

# --- enable services ---
step "enabling services"
loginctl enable-linger "$RUNNER_USER"
sctl daemon-reload
say "  (re)starting sp-runner-agent…"
sctl enable sp-runner-agent.service
sctl restart sp-runner-agent.service        # enable --now won't restart a running unit → new code
sctl enable --now sp-runner-gc.timer
sleep 2
if sctl is-active --quiet sp-runner-agent.service; then
    say "  agent: active, listening on :8181"
else
    warn "agent NOT active:"; asuser systemctl --user status --no-pager sp-runner-agent.service | tail -15 | sed 's/^/      /' || true
fi

if [ "$HAVE_MANAGER" = 1 ]; then
    say "  (re)starting sp-runner-manager…"
    sctl enable sp-runner-manager.service
    sctl restart sp-runner-manager.service
    sleep 3
    if sctl is-active --quiet sp-runner-manager.service; then
        say "  manager: active, listening on :8080"
        step "SEED ADMIN (first run only)"
        cat "$HOME_DIR/.local/state/sp-runner-manager/seed-admin.txt" 2>/dev/null | sed 's/^/      /' \
            || echo "      (already initialised — reset: cd ~ghrunner/sp-runner-manager && \\
      FARM_MANAGER_CONFIG=~ghrunner/.config/sp-runner/manager.toml .venv/bin/python manage.py reset-password admin)"
    else
        warn "manager NOT active:"; asuser systemctl --user status --no-pager sp-runner-manager.service | tail -20 | sed 's/^/      /' || true
    fi
fi

step "done — $TARGET  ($(( $(date +%s) - T0 ))s total)"
say "agent fingerprint: $FPR"
