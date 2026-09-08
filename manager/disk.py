"""Disk sampling + watermark enforcement.

The runner-data filesystem is the FIRST mount an agent reports (its
disk_mounts[0], normally /home). Watermarks act on that mount's used-percent:

  warn   -> UI banner only
  prune  -> ask the agent to `podman system prune` + clear idle-slot tool caches
  freeze -> stop enabling new slots on that host (enforced in app.py) + audit
"""
from __future__ import annotations

import time
from typing import Any

import agentclient
import db
from config import CFG

# host_id -> {"mount", "used_pct", "level", "since", "total", "used"}
DISK: dict[str, dict[str, Any]] = {}


def level_for(used_pct: float) -> str:
    wm = CFG.watermarks
    if used_pct >= wm.freeze:
        return "freeze"
    if used_pct >= wm.prune:
        return "prune"
    if used_pct >= wm.warn:
        return "warn"
    return "ok"


def is_frozen(host_id: str) -> bool:
    return DISK.get(host_id, {}).get("level") == "freeze"


async def sample_and_enforce() -> None:
    for host in CFG.hosts:
        if not host.enabled:
            continue
        try:
            data = await agentclient.get_disk(host)
        except agentclient.AgentError:
            continue

        mounts = data.get("mounts", [])
        for m in mounts:
            db.add_disk_sample(host.id, m["mount"], m["total_bytes"], m["used_bytes"])
        if not mounts:
            continue

        primary = mounts[0]
        used_pct = primary["used_pct"]
        new_level = level_for(used_pct)
        prev = DISK.get(host.id, {})
        prev_level = prev.get("level")

        DISK[host.id] = {
            "mount": primary["mount"],
            "used_pct": used_pct,
            "total": primary["total_bytes"],
            "used": primary["used_bytes"],
            "level": new_level,
            "since": prev.get("since", time.time()) if new_level == prev_level else time.time(),
            "all_mounts": mounts,
            "podman_df": data.get("podman_df"),
        }

        if new_level != prev_level:
            db.audit("system", "disk-watermark", target=host.id,
                     detail=f"{primary['mount']} at {used_pct}% -> {new_level}")

        if new_level in ("prune", "freeze") and prev_level not in ("prune", "freeze"):
            try:
                res = await agentclient.prune(host)
                db.audit("system", "auto-prune", target=host.id,
                         detail=f"triggered at {used_pct}%: {str(res)[:200]}")
            except agentclient.AgentError as exc:
                db.audit("system", "auto-prune", target=host.id, detail=str(exc), result="error")


def snapshot() -> dict[str, Any]:
    return {
        "watermarks": {"warn": CFG.watermarks.warn, "prune": CFG.watermarks.prune, "freeze": CFG.watermarks.freeze},
        "hosts": {hid: d for hid, d in DISK.items()},
    }
