"""Fleet reconciliation.

Merges three sources into one view:
  - each agent's /state (authoritative for "what is this host's systemd doing")
  - GitHub's org runner list (authoritative for "is the runner connected / busy")
  - our own reachability history (drives the degraded -> reap state machine)

Reap policy: a host unreachable longer than reap_grace_minutes has its GitHub
runner registrations deleted so queued jobs stop targeting a dead node. The
host rejoins cleanly when its agent comes back (slots re-register on next start).
"""
from __future__ import annotations

import re
import time
from typing import Any

import agentclient
import db
from config import CFG
from events import HUB
from github import APP

# host_id -> live status
FLEET: dict[str, dict[str, Any]] = {}

_RUNNER_NAME_RE = re.compile(r"^(?P<host>.+)-slot-(?P<slot>\d+)$")


def _host_entry(host_id: str) -> dict[str, Any]:
    return FLEET.setdefault(host_id, {
        "host_id": host_id,
        "reachable": False,
        "last_seen": None,
        "unreachable_since": None,
        "reaped": False,
        "error": None,
        "state": None,
    })


async def reconcile() -> None:
    now = time.time()

    # 1. GitHub side (once for the whole org).
    try:
        gh_runners = await APP.list_runners()
        gh_by_name = {r["name"]: r for r in gh_runners}
        gh_error = None
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the loop
        gh_runners, gh_by_name, gh_error = [], {}, str(exc)

    # 2. Each host's agent.
    for host in CFG.hosts:
        entry = _host_entry(host.id)
        was_reachable = entry["reachable"]
        if not host.enabled:
            entry.update(reachable=False, error="disabled in config", state=None)
            continue
        try:
            state = await agentclient.get_state(host)
            entry.update(reachable=True, last_seen=now, unreachable_since=None,
                         reaped=False, error=None, state=state)
            if not was_reachable and entry.get("_ever_seen"):
                await HUB.emit("host.recovered", f"{host.id} agent is back", host=host.id)
            entry["_ever_seen"] = True
        except agentclient.AgentError as exc:
            if entry["unreachable_since"] is None:
                entry["unreachable_since"] = now
            entry.update(reachable=False, error=str(exc))
            if was_reachable:
                await HUB.emit("host.degraded", f"{host.id} agent unreachable: {exc}", host=host.id)

    # 3. Attach GitHub status to each slot; detect orphans; emit slot transitions.
    grace = CFG.reap_grace_minutes * 60
    for host in CFG.hosts:
        entry = _host_entry(host.id)
        state = entry.get("state")
        prev = entry.setdefault("_slot_states", {})
        if state:
            for slot in state.get("slots", []):
                n = slot["slot"]
                gh = gh_by_name.get(f"{host.id}-slot-{n}")
                slot["github"] = _slim_gh(gh) if gh else None
                st, was = slot["state"], prev.get(n)
                if was and was != st:
                    if st == "offline":
                        await HUB.emit("slot.offline", f"{host.id}/slot-{n} went offline",
                                       host=host.id, slot=n, restarts=slot["restarts"])
                    elif was == "offline" and st in ("idle", "running"):
                        await HUB.emit("slot.online", f"{host.id}/slot-{n} back online", host=host.id, slot=n)
                if slot["restarts"] >= 20 and prev.get(f"cl{n}") != (slot["restarts"] // 20):
                    prev[f"cl{n}"] = slot["restarts"] // 20
                    await HUB.emit("slot.crashloop",
                                   f"{host.id}/slot-{n} has {slot['restarts']} restarts",
                                   host=host.id, slot=n, restarts=slot["restarts"])
                if CFG.track_jobs and slot.get("job") != prev.get(f"job{n}"):
                    if slot.get("job"):
                        await HUB.emit("job.started", f"{host.id}/slot-{n}: {slot['job']}",
                                       host=host.id, slot=n, job=slot["job"])
                    elif prev.get(f"job{n}"):
                        await HUB.emit("job.finished", f"{host.id}/slot-{n} finished {prev.get(f'job{n}')}",
                                       host=host.id, slot=n)
                    prev[f"job{n}"] = slot.get("job")
                prev[n] = st

        # Degraded -> reap.
        us = entry["unreachable_since"]
        if us and not entry["reaped"] and (now - us) > grace and not gh_error:
            n = await _reap_host_runners(host.id, gh_runners, reason="host unreachable > grace")
            entry["reaped"] = True
            await HUB.emit("host.reaped", f"{host.id} unreachable > {CFG.reap_grace_minutes}m — reaped {n} runner(s)",
                           host=host.id, count=n)

    FLEET["_meta"] = {
        "reconciled_at": now,
        "github_error": gh_error,
        "github_runner_count": len(gh_runners),
        "orphans": _find_orphans(gh_runners),
    }


def _slim_gh(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "status": r.get("status"),          # "online" / "offline"
        "busy": r.get("busy", False),
        "labels": [l["name"] for l in r.get("labels", [])],
    }


def _find_orphans(gh_runners: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """GitHub runners that look like ours (<host>-slot-<n>) but have no live
    local slot backing them — offline registrations left after a crash/reboot."""
    live: set[str] = set()
    for host_id, entry in FLEET.items():
        if host_id == "_meta" or not isinstance(entry, dict):
            continue
        for slot in (entry.get("state") or {}).get("slots", []):
            live.add(f"{host_id}-slot-{slot['slot']}")
    orphans = []
    for r in gh_runners:
        m = _RUNNER_NAME_RE.match(r["name"])
        if not m:
            continue
        if r["name"] not in live and r.get("status") == "offline":
            orphans.append({"id": r["id"], "name": r["name"],
                            "host": m.group("host"), "slot": int(m.group("slot"))})
    return orphans


async def _reap_host_runners(host_id: str, gh_runners: list[dict[str, Any]], reason: str) -> int:
    n = 0
    for r in gh_runners:
        m = _RUNNER_NAME_RE.match(r["name"])
        if m and m.group("host") == host_id:
            try:
                await APP.delete_runner(r["id"])
                n += 1
            except Exception as exc:  # noqa: BLE001
                db.audit("system", "reap-runner", target=r["name"], detail=str(exc), result="error")
    if n:
        db.audit("system", "reap-host", target=host_id, detail=f"{reason}: removed {n} runner(s)")
    return n


async def reap_orphan(username: str, runner_id: int, name: str) -> None:
    await APP.delete_runner(runner_id)
    db.audit(username, "reap-orphan", target=name, detail=f"runner id {runner_id}")


def snapshot() -> dict[str, Any]:
    """Plain dict for templates / JSON."""
    hosts = []
    for host in CFG.hosts:
        e = _host_entry(host.id)
        degraded = (not e["reachable"] and e["unreachable_since"] is not None)
        hosts.append({
            "id": host.id,
            "address": host.address,
            "enabled": host.enabled,
            "reachable": e["reachable"],
            "degraded": degraded,
            "reaped": e["reaped"],
            "error": e["error"],
            "last_seen": e["last_seen"],
            "unreachable_since": e["unreachable_since"],
            "state": e["state"],
        })
    return {"hosts": hosts, "meta": FLEET.get("_meta", {})}
