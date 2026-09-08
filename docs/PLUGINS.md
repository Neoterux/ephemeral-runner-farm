# Plugins

A plugin is a Python module that runs inside the manager and hooks into three
things: **events**, **HTTP routes**, and **Prometheus metrics**. Two ship in the
box (`webhook`, `prometheus`); write your own for anything else.

## Enabling

```toml
[plugins]
enabled = ["webhook", "prometheus"]

[plugins.webhook]
# ...this table is passed to webhook's setup() as `config`
```

Names resolve to `manager/plugins/<name>.py`, or any importable module path. A
plugin that raises during load is logged and skipped — it never stops the
manager.

## Writing one

```python
# manager/plugins/mything.py

def setup(hub, config: dict) -> None:
    # 1. react to events
    async def on_evt(ev):
        # ev.kind, ev.message, ev.host, ev.slot, ev.data, ev.ts
        ...
    hub.on("host.*", on_evt)          # fnmatch glob; "*" for everything

    # 2. add an HTTP route (mounted before the server binds)
    async def handler():
        return {"hello": config.get("who", "world")}
    hub.route("/api/v1/mything", handler, methods=["GET"])

    # 3. add Prometheus metrics (rendered by the prometheus plugin)
    hub.metric(
        "farm_mything_total", "things counted",
        lambda: [({"kind": "a"}, 3), ({"kind": "b"}, 5)],   # [(labels, value), ...]
    )
```

`hub` is `events.HUB`. Route handlers are plain FastAPI endpoints — add your own
auth dependency if the route is sensitive (the built-in API key check is
`api.require_key`).

## Events

Emitted by core on a state **transition** (not every poll):

| kind | when |
|---|---|
| `host.degraded` / `host.recovered` | agent stopped / started responding |
| `host.reaped` | host down past the grace period, its GitHub runners deleted |
| `slot.offline` / `slot.online` | a slot's state changed to/from offline |
| `slot.crashloop` | restart count crossed a multiple of 20 |
| `disk.ok` / `disk.warn` / `disk.prune` / `disk.freeze` | watermark level changed |
| `job.started` / `job.finished` | only if `[events] track_jobs = true` |

All events are also stored (queryable at `/api/v1/events`) and shown on the
**Health** page.

## Bundled: `webhook`

POST events to any URL — ntfy.sh, Pushover, Gotify, Slack, your own endpoint.

```toml
[plugins.webhook]
[[plugins.webhook.targets]]
url    = "https://ntfy.sh/my-runner-farm"
events = ["host.degraded", "host.recovered", "disk.freeze", "slot.crashloop"]
format = "ntfy"        # ntfy | slack | json
# headers = { Authorization = "Bearer …" }   # optional

[[plugins.webhook.targets]]                   # a second target, different filter
url    = "https://hooks.slack.com/services/…"
events = ["*"]
format = "slack"
```

- `ntfy` — sends `ev.message` as the body with `Title`/`Priority`/`Tags` headers.
  Point your phone's ntfy app at the topic and you get a push when a host drops.
- `slack` — `{"text": "..."}`
- `json` — the full event object

A failing target is swallowed so a down notifier can't stall the reconcile loop.

## Bundled: `prometheus`

Adds `GET /metrics`. Renders the core series plus every `hub.metric(...)` other
plugins registered. Gate it with an API key or set `[api] metrics_public = true`.
See [API.md](API.md) for the scrape config and series list.
