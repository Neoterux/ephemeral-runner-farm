"""prometheus plugin — exposes /metrics in the text exposition format for
Grafana / Prometheus to scrape.

    [plugins]
    enabled = ["prometheus"]

    [api]
    metrics_public = true     # or false to require an X-API-Key on /metrics

Also renders any gauges other plugins register via hub.metric(name, help, fn)
where fn() -> [({label: value, ...}, number), ...].
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import Header, HTTPException
from fastapi.responses import PlainTextResponse

import db
import disk
import fleet
from config import CFG

_STATES = ("idle", "running", "restarting", "drained", "stopped", "offline")


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def _line(name: str, labels: dict[str, Any], value: float) -> str:
    if labels:
        lbl = ",".join(f'{k}="{_esc(str(v))}"' for k, v in labels.items())
        return f"{name}{{{lbl}}} {value}"
    return f"{name} {value}"


def _core_metrics() -> list[str]:
    out: list[str] = []
    snap = fleet.snapshot()
    dsnap = disk.snapshot()

    out += ["# HELP farm_host_reachable 1 if the host agent responded on the last reconcile",
            "# TYPE farm_host_reachable gauge"]
    for h in snap["hosts"]:
        out.append(_line("farm_host_reachable", {"host": h["id"]}, int(h["reachable"])))
        if h["unreachable_since"]:
            out.append(_line("farm_host_unreachable_seconds", {"host": h["id"]},
                             round(time.time() - h["unreachable_since"], 1)))

    out += ["# HELP farm_slots number of slots in a given state",
            "# TYPE farm_slots gauge"]
    for h in snap["hosts"]:
        counts = {s: 0 for s in _STATES}
        for slot in (h.get("state") or {}).get("slots", []):
            counts[slot["state"]] = counts.get(slot["state"], 0) + 1
            out.append(_line("farm_slot_restarts_total",
                             {"host": h["id"], "slot": slot["slot"]}, slot["restarts"]))
        for st, n in counts.items():
            out.append(_line("farm_slots", {"host": h["id"], "state": st}, n))

    out += ["# HELP farm_disk_used_ratio used fraction of a mount (0-1)",
            "# TYPE farm_disk_used_ratio gauge"]
    for hid, d in dsnap["hosts"].items():
        for m in d.get("all_mounts", []):
            lbl = {"host": hid, "mount": m["mount"]}
            out.append(_line("farm_disk_used_ratio", lbl, round(m["used_pct"] / 100, 4)))
            out.append(_line("farm_disk_used_bytes", lbl, m["used_bytes"]))
            out.append(_line("farm_disk_total_bytes", lbl, m["total_bytes"]))

    meta = snap["meta"]
    out += [_line("farm_github_runners", {}, meta.get("github_runner_count", 0)),
            _line("farm_orphan_runners", {}, len(meta.get("orphans", []))),
            _line("farm_reconcile_timestamp_seconds", {}, meta.get("reconciled_at", 0) or 0),
            _line("farm_github_up", {}, int(not meta.get("github_error"))),
            _line("farm_up", {}, 1)]

    for row in db.recent_events(limit=1):
        out.append(_line("farm_last_event_timestamp_seconds", {"kind": row["kind"]}, row["ts"]))
    return out


def setup(hub, config: dict[str, Any]) -> None:
    async def metrics(x_api_key: str | None = Header(default=None),
                      authorization: str | None = Header(default=None)) -> PlainTextResponse:
        if not CFG.metrics_public:
            key = x_api_key or (authorization or "").removeprefix("Bearer ").strip()
            if not CFG.api_keys or key not in CFG.api_keys:
                raise HTTPException(status_code=401, detail="metrics require an API key (or set api.metrics_public)")
        lines = _core_metrics()
        for name, mtype, help_, fn in hub.metrics:
            lines += [f"# HELP {name} {help_}", f"# TYPE {name} {mtype}"]
            try:
                for labels, value in fn():
                    lines.append(_line(name, labels, value))
            except Exception:  # noqa: BLE001
                lines.append(_line(name + "_scrape_error", {}, 1))
        return PlainTextResponse("\n".join(lines) + "\n",
                                 media_type="text/plain; version=0.0.4; charset=utf-8")

    hub.route("/metrics", metrics, methods=["GET"], name="prometheus_metrics")
