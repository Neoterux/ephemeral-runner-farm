"""webhook plugin — POST events to one or more URLs. Powers phone notifications
via ntfy.sh / Pushover / Gotify / Slack / any endpoint that takes a POST.

    [plugins.webhook]
    [[plugins.webhook.targets]]
    url    = "https://ntfy.sh/my-runner-farm"
    events = ["host.degraded", "host.recovered", "disk.freeze", "slot.crashloop"]
    format = "ntfy"            # ntfy | slack | json   (default: json)
    # optional: headers = { Authorization = "Bearer tok" }
"""
from __future__ import annotations

import fnmatch
from typing import Any

import httpx

_PRIORITY = {  # ntfy priority by event severity
    "host.degraded": "high", "host.reaped": "high", "disk.freeze": "urgent",
    "disk.prune": "high", "slot.crashloop": "high", "slot.offline": "default",
}
_EMOJI = {
    "host.degraded": "warning", "host.recovered": "white_check_mark",
    "host.reaped": "wastebasket", "disk.warn": "warning", "disk.prune": "broom",
    "disk.freeze": "no_entry", "disk.ok": "white_check_mark",
    "slot.offline": "warning", "slot.online": "white_check_mark",
    "slot.crashloop": "recycle",
}


def _match(kind: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(kind, p) for p in patterns) if patterns else True


def setup(hub, config: dict[str, Any]) -> None:
    targets = config.get("targets", [])
    if not targets:
        return

    async def deliver(ev) -> None:
        for t in targets:
            if not _match(ev.kind, t.get("events", [])):
                continue
            url = t["url"]
            fmt = t.get("format", "json")
            headers = dict(t.get("headers", {}))
            body: Any
            if fmt == "ntfy":
                headers.setdefault("Title", f"runner-farm: {ev.kind}")
                headers.setdefault("Priority", _PRIORITY.get(ev.kind, "default"))
                headers.setdefault("Tags", _EMOJI.get(ev.kind, "gear"))
                body = ev.message
            elif fmt == "slack":
                loc = " ".join(x for x in (ev.host, f"slot-{ev.slot}" if ev.slot is not None else None) if x)
                body = None
                headers.setdefault("content-type", "application/json")
                data = {"text": f"*{ev.kind}* {loc}\n{ev.message}"}
            else:
                headers.setdefault("content-type", "application/json")
                body = None
                data = ev.as_dict()

            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    if fmt == "ntfy":
                        await c.post(url, content=body.encode(), headers=headers)
                    else:
                        await c.post(url, json=data, headers=headers)
            except httpx.HTTPError:
                pass  # a down notifier must not block the reconcile loop

    hub.on("*", deliver)
