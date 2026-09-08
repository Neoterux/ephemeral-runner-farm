#!/usr/bin/env bash
# Retire one bare-metal runner: drain it, deregister it from GitHub, then
# remove its systemd unit and account. One runner per invocation — deliberately
# not a batch, so you watch each one land.
#
#   sudo GH_TOKEN=ghp_xxx ./03-retire-runner.sh <account> [--apply]
#
#     <account>  the local user account the runner runs as (see /home/*/runner)
#     GH_TOKEN   a token with admin:org (org runners) or repo admin (repo
#                runners). Only used to mint a removal token.
#
# Drains first: waits (up to --wait, default 2h) for the runner to finish its
# current job before doing anything destructive. Ctrl-C is safe during the wait.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

ACCT="${1:?usage: 03-retire-runner.sh <account> [--apply]}"
APPLY=0; WAIT_SECS=7200
shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --wait)  WAIT_SECS="${2:?}"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done
run() { echo "+ $*"; [[ $APPLY -eq 1 ]] && "$@" || true; }

home="/home/${ACCT}"
rd="${home}/runner"
[[ -d "$rd" ]] || { echo "no runner dir at $rd" >&2; exit 1; }

# --- work out org vs repo scope from .runner --------------------------------
gh_url="$(jq -r '.gitHubUrl' "$rd/.runner" 2>/dev/null || echo)"
agent_name="$(jq -r '.agentName' "$rd/.runner" 2>/dev/null || echo "$ACCT")"
case "$gh_url" in
    *"/"*"/"*) scope="repo";  api_base="repos/$(echo "$gh_url" | sed -E 's#https://github.com/##')" ;;
    *)         scope="org";   api_base="orgs/$(echo "$gh_url" | sed -E 's#https://github.com/##')" ;;
esac
unit="$(systemctl list-unit-files "actions.runner.*${ACCT}*" --no-pager | awk 'NR==2{print $1}')"

echo "account : $ACCT"
echo "runner  : $agent_name  (scope: $scope, $gh_url)"
echo "unit    : ${unit:-<none found>}"
echo

# --- drain: wait for the current job to finish ----------------------------
echo "[drain] waiting up to $((WAIT_SECS/60)) min for an idle window..."
deadline=$(( $(date +%s) + WAIT_SECS ))
while :; do
    if journalctl -u "$unit" -n 5 --no-pager 2>/dev/null | grep -q "Running job:"; then
        [[ $(date +%s) -lt $deadline ]] || { echo "[drain] timed out — aborting, runner still busy" >&2; exit 1; }
        sleep 30
    else
        echo "[drain] runner is idle."
        break
    fi
done

[[ -n "${GH_TOKEN:-}" ]] || { echo "GH_TOKEN not set — cannot mint removal token" >&2; exit 1; }
echo "[github] requesting removal token ($api_base)"
remove_token="$(curl -fsS -X POST \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/${api_base}/actions/runners/remove-token" | jq -r '.token')"
[[ -n "$remove_token" && "$remove_token" != "null" ]] || { echo "failed to get removal token" >&2; exit 1; }

echo "[retire] stop + deregister + remove"
[[ -n "$unit" ]] && run systemctl disable --now "$unit"
run runuser -u "$ACCT" -- bash -c "cd '$rd' && ./config.sh remove --token '$remove_token'"
[[ -n "$unit" ]] && run rm -f "/etc/systemd/system/${unit}" "/etc/systemd/system/${unit}.d" -r
run loginctl disable-linger "$ACCT" 2>/dev/null || true
run systemctl daemon-reload
run userdel -r "$ACCT"

if [[ $APPLY -eq 1 ]]; then
    echo "done — $ACCT retired."
else
    echo; echo "dry run — re-run with --apply"
fi
