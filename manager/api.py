"""Read/control JSON API under /api/v1. Auth: an X-API-Key header (or
Authorization: Bearer) matching one of [api].keys in manager.toml. With no keys
configured the API is disabled.

This is the stable surface external tools build on — a state watcher, a
notifier, a Prometheus exporter — without touching the core.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

import agentclient
import db
import disk
import fleet
from config import CFG

router = APIRouter(prefix="/api/v1", tags=["api"])


async def require_key(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    if not CFG.api_keys:
        raise HTTPException(status_code=503, detail="API disabled — set [api].keys in manager.toml")
    presented = x_api_key or (authorization or "").removeprefix("Bearer ").strip()
    if presented not in CFG.api_keys:
        raise HTTPException(status_code=401, detail="bad or missing API key")


API = Depends(require_key)


@router.get("/fleet", dependencies=[API])
async def api_fleet() -> dict[str, Any]:
    return fleet.snapshot()


@router.get("/hosts", dependencies=[API])
async def api_hosts() -> list[dict[str, Any]]:
    return fleet.snapshot()["hosts"]


@router.get("/hosts/{host_id}", dependencies=[API])
async def api_host(host_id: str) -> dict[str, Any]:
    h = next((h for h in fleet.snapshot()["hosts"] if h["id"] == host_id), None)
    if h is None:
        raise HTTPException(status_code=404, detail=f"unknown host {host_id}")
    h["disk"] = disk.snapshot()["hosts"].get(host_id)
    return h


@router.get("/disks", dependencies=[API])
async def api_disks() -> dict[str, Any]:
    return disk.snapshot()


@router.get("/events", dependencies=[API])
async def api_events(since_id: int = 0, limit: int = 100, kind: str | None = None) -> dict[str, Any]:
    rows = db.recent_events(limit=limit, since_id=since_id, kind_like=kind)
    return {"events": rows, "latest_id": rows[0]["id"] if rows else since_id}


@router.get("/summary", dependencies=[API])
async def api_summary() -> dict[str, Any]:
    """Compact rollup — everything a phone-notification watcher needs in one poll."""
    snap = fleet.snapshot()
    dsnap = disk.snapshot()
    hosts_up = sum(1 for h in snap["hosts"] if h["reachable"])
    degraded = [h["id"] for h in snap["hosts"] if h["degraded"]]
    slots, running, offline, crashloopers = 0, 0, 0, []
    for h in snap["hosts"]:
        for s in (h.get("state") or {}).get("slots", []):
            slots += 1
            if s["state"] == "running":
                running += 1
            elif s["state"] == "offline":
                offline += 1
            if s["restarts"] > 20:
                crashloopers.append(f"{h['id']}/slot-{s['slot']}")
    worst = max((d["used_pct"] for d in dsnap["hosts"].values()), default=0.0)
    frozen = [hid for hid, d in dsnap["hosts"].items() if d.get("level") == "freeze"]
    return {
        "ts": time.time(),
        "ok": not degraded and not offline and not frozen,
        "hosts_total": len(snap["hosts"]), "hosts_up": hosts_up,
        "hosts_degraded": degraded,
        "slots_total": slots, "slots_running": running, "slots_offline": offline,
        "slots_crashloop": crashloopers,
        "github_runners": snap["meta"].get("github_runner_count", 0),
        "github_error": snap["meta"].get("github_error"),
        "disk_worst_pct": round(worst, 1), "disk_frozen_hosts": frozen,
        "orphans": len(snap["meta"].get("orphans", [])),
        "reconciled_ago_s": round(time.time() - (snap["meta"].get("reconciled_at") or time.time()), 1),
    }


_ACTIONS = {"start", "stop", "restart", "drain", "clear-cache"}


@router.post("/hosts/{host_id}/slots/{slot}/{action}", dependencies=[API])
async def api_slot_action(host_id: str, slot: int, action: str) -> dict[str, Any]:
    if action not in _ACTIONS:
        raise HTTPException(status_code=400, detail=f"action must be one of {sorted(_ACTIONS)}")
    host = CFG.host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"unknown host {host_id}")
    try:
        res = await agentclient.slot_action(host, slot, action)
    except agentclient.AgentError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.audit("api", f"slot-{action}", target=f"{host_id}/slot-{slot}")
    return res


@router.get("/logs/{host_id}/{slot}", dependencies=[API])
async def api_slot_logs(host_id: str, slot: int, lines: int = 200) -> dict[str, Any]:
    host = CFG.host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"unknown host {host_id}")
    try:
        return await agentclient.slot_logs(host, slot, lines)
    except agentclient.AgentError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
