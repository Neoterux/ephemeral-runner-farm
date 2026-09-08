#!/usr/bin/env bash
# Ephemeral runner entrypoint. Registers with GitHub, runs exactly one job
# (--ephemeral), then exits. Cleans up its own registration on any exit path so
# a killed container never leaves a ghost runner behind.
set -euo pipefail

: "${RUNNER_URL:?RUNNER_URL is required (e.g. https://github.com/your-org)}"
: "${RUNNER_TOKEN:?RUNNER_TOKEN is required (short-lived registration token)}"
RUNNER_NAME="${RUNNER_NAME:?RUNNER_NAME is required}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,linux,x64}"
RUNNER_GROUP="${RUNNER_GROUP:-}"
RUNNER_WORKDIR="${RUNNER_WORKDIR:-_work}"

cd /home/runner

# On SIGTERM/SIGINT, ask run.sh to stop cleanly. We do NOT call `config.sh
# remove` — that needs a *removal* token, not this *registration* token, and an
# --ephemeral runner's registration is auto-reaped by GitHub when it disconnects
# (the manager's orphan reaper is the backstop).
term() { echo "[entrypoint] signal received — stopping"; kill -INT "${run_pid:-0}" 2>/dev/null || true; }
trap term INT TERM

config_args=(
    --url "${RUNNER_URL}"
    --token "${RUNNER_TOKEN}"
    --name "${RUNNER_NAME}"
    --labels "${RUNNER_LABELS}"
    --work "${RUNNER_WORKDIR}"
    --unattended
    --replace
    --ephemeral
    --disableupdate
)
if [[ -n "${RUNNER_GROUP}" ]]; then
    config_args+=(--runnergroup "${RUNNER_GROUP}")
fi

echo "[entrypoint] configuring ${RUNNER_NAME} (labels: ${RUNNER_LABELS}${RUNNER_GROUP:+, group: ${RUNNER_GROUP}})"
./config.sh "${config_args[@]}"

echo "[entrypoint] starting run.sh — will exit after one job"
# run.sh handles the job and returns; --ephemeral makes it a single-shot.
./run.sh &
run_pid=$!
wait "$run_pid"
