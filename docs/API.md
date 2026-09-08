# HTTP API

Stable JSON surface under `/api/v1` plus a Prometheus `/metrics` endpoint.
Build state watchers, notifiers, and exporters against this instead of touching
the core.

## Auth

Set one or more keys in `manager.toml`:

```toml
[api]
keys = ["k-3f9a…"]        # deploy.sh init generates one
metrics_public = false    # true = /metrics needs no key
```

Send the key as `X-API-Key: <key>` or `Authorization: Bearer <key>`. With no
keys configured every `/api/v1/*` call returns `503`.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/api/v1/summary` | compact health rollup — one poll has everything a notifier needs |
| GET | `/api/v1/fleet` | full fleet snapshot: hosts, slots, GitHub status, meta |
| GET | `/api/v1/hosts` | array of hosts |
| GET | `/api/v1/hosts/{id}` | one host + its disk snapshot |
| GET | `/api/v1/disks` | per-host mounts, levels, `podman system df` |
| GET | `/api/v1/events?since_id=&limit=&kind=` | event log (newest first); `kind` accepts `host.*` |
| GET | `/api/v1/logs/{host}/{slot}?lines=` | container + journal for a slot |
| POST | `/api/v1/hosts/{host}/slots/{slot}/{action}` | `start` `stop` `restart` `drain` `clear-cache` |
| GET | `/metrics` | Prometheus text exposition |

### `/api/v1/summary`

```json
{
  "ts": 1788888888.0,
  "ok": false,
  "hosts_total": 2, "hosts_up": 1, "hosts_degraded": ["build-farm-2"],
  "slots_total": 7, "slots_running": 3, "slots_offline": 1, "slots_crashloop": [],
  "github_runners": 7, "github_error": null,
  "disk_worst_pct": 78.0, "disk_frozen_hosts": [],
  "orphans": 1, "reconciled_ago_s": 4.2
}
```

`ok` is `false` when any host is degraded, any slot offline, or any host disk-frozen.

## Example: a phone-notification watcher

You do **not** need this — the `webhook` plugin already does it in-process (see
[PLUGINS.md](PLUGINS.md)). But if you want an external process:

```bash
#!/usr/bin/env bash
API=https://build-farm:8080 ; KEY=k-3f9a…
prev_ok=true
while :; do
  ok=$(curl -sf -H "X-API-Key: $KEY" "$API/api/v1/summary" | jq -r .ok)
  if [ "$ok" = false ] && [ "$prev_ok" = true ]; then
    msg=$(curl -sf -H "X-API-Key: $KEY" "$API/api/v1/summary" \
          | jq -r '"degraded=\(.hosts_degraded) offline=\(.slots_offline) disk=\(.disk_worst_pct)%"')
    curl -s -d "runner farm not OK: $msg" https://ntfy.sh/YOUR-TOPIC
  fi
  prev_ok=$ok
  sleep 30
done
```

## Example: Prometheus scrape config

```yaml
scrape_configs:
  - job_name: runner-farm
    metrics_path: /metrics
    static_configs: [{ targets: ["build-farm:8080"] }]
    authorization: { credentials: "k-3f9a…" }   # omit if metrics_public = true
```

Key series: `farm_host_reachable{host}`, `farm_slots{host,state}`,
`farm_slot_restarts_total{host,slot}`, `farm_disk_used_ratio{host,mount}`,
`farm_disk_used_bytes`, `farm_github_runners`, `farm_reconcile_timestamp_seconds`,
`farm_up`.
