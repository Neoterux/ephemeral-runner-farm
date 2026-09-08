"""Event hub — the extension point.

Core (fleet reconcile, disk watermarks) calls `HUB.emit(...)` on a state
transition. Plugins register against the hub at startup:

    def setup(hub, config):
        hub.on("host.*", my_async_or_sync_handler)
        hub.route("/api/v1/thing", handler, methods=["GET"])
        hub.metric("farm_thing_total", "help text", lambda: [({}, 42)])

Event kinds currently emitted:
    host.degraded  host.recovered  host.reaped
    slot.offline   slot.online     slot.crashloop
    disk.ok        disk.warn       disk.prune     disk.freeze
    job.started    job.finished    (only if [events] track_jobs = true)
"""
from __future__ import annotations

import asyncio
import fnmatch
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

import db


@dataclass(frozen=True)
class Event:
    ts: float
    kind: str
    message: str
    host: str | None = None
    slot: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


Handler = Callable[[Event], Awaitable[None] | None]
MetricFn = Callable[[], "list[tuple[dict[str, str], float]]"]


class Hub:
    def __init__(self) -> None:
        self._subs: list[tuple[str, Handler]] = []
        self.routes: list[dict[str, Any]] = []
        self.metrics: list[tuple[str, str, str, MetricFn]] = []  # name, mtype, help, fn

    # -- plugin-facing API ------------------------------------------------
    def on(self, pattern: str, handler: Handler) -> None:
        """pattern is an fnmatch glob over the kind, e.g. 'host.*' or '*'."""
        self._subs.append((pattern, handler))

    def route(self, path: str, handler: Callable[..., Any], methods: list[str] | None = None,
              name: str | None = None) -> None:
        self.routes.append({"path": path, "endpoint": handler,
                            "methods": methods or ["GET"], "name": name})

    def metric(self, name: str, help_: str, fn: MetricFn, mtype: str = "gauge") -> None:
        self.metrics.append((name, mtype, help_, fn))

    # -- core-facing -----------------------------------------------------
    async def emit(self, kind: str, message: str, *, host: str | None = None,
                   slot: int | None = None, **data: Any) -> Event:
        ev = Event(time.time(), kind, message, host, slot, data)
        try:
            db.add_event(ev.ts, kind, message, host, slot, data)
        except Exception:  # noqa: BLE001 — never let logging break the loop
            pass
        for pattern, handler in self._subs:
            if fnmatch.fnmatchcase(kind, pattern):
                try:
                    res = handler(ev)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:  # noqa: BLE001
                    db.audit("plugin", "event-handler-error", target=kind, detail=str(exc)[:300], result="error")
        return ev


HUB = Hub()
