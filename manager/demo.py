"""Demo mode. `FARM_DEMO=1` fills the fleet + disk views with synthetic data so
the UI can be explored (or screenshotted) without any hosts. Never touches
GitHub or a real agent. Enabled from background.py."""
from __future__ import annotations

import math
import time

import db
import disk
import fleet
from config import CFG


def _slot(n, state, job=None, restarts=0, labels="self-hosted,linux,x64,podman", group="builder", tag="2026-09-08"):
    return {
        "slot": n, "state": state, "enabled": state != "stopped",
        "labels": labels, "group": group, "image_tag": tag,
        "memory": "12g", "cpus": "2", "restarts": restarts,
        "active_state": "active", "sub_state": "running",
        "container": "running" if state in ("running", "idle") else None,
        "phase": "running-job" if state == "running" else "idle", "job": job,
        "github": {"id": 400 + n, "status": "online" if state in ("running", "idle") else "offline",
                   "busy": state == "running", "labels": labels.split(",")},
    }


def _host_state(hid, slots):
    return {"host_id": hid, "version": "1.0.0", "podman": "podman version 5.8.2",
            "kernel": "5.14.0-503.el9", "slots": slots, "time": time.time()}


def slot_logs(host_id, slot, lines):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return {
        "slot": slot, "container_running": True,
        "container": [
            f"{ts}  √ Connected to GitHub",
            f"{ts}  Current runner version: '2.337.0'",
            f"{ts}  {time.strftime('%Y-%m-%d %H:%M:%SZ')}: Listening for Jobs",
            f"{ts}  {time.strftime('%Y-%m-%d %H:%M:%SZ')}: Running job: build-backend",
            f"{ts}  ##[group]Run actions/checkout@v4",
            f"{ts}  Syncing repository: {host_id.replace('-', '/')}/service",
            f"{ts}  ##[endgroup]",
            f"{ts}  ##[group]Run actions/setup-node@v4",
            f"{ts}  Found in cache @ /home/runner/_work/_tool/node/20.18.0/x64",
            f"{ts}  ##[endgroup]",
            f"{ts}  $ pnpm install --frozen-lockfile",
            f"{ts}  Lockfile is up to date, resolution step is skipped",
            f"{ts}  Packages: +812 reused, 0 downloaded",
            f"{ts}  $ pnpm run build",
            f"{ts}  vite v5.4.8 building for production...",
            f"{ts}  ✓ 1284 modules transformed.",
        ][-lines:],
        "journal": [
            f"{ts} {host_id} systemd[993]: Starting sp-runner@{slot}.service...",
            f"{ts} {host_id} sp-runner-prestart[41]: [prestart] slot {slot}: token minted for {host_id}-slot-{slot}",
            f"{ts} {host_id} systemd[993]: Started sp-runner@{slot}.service.",
            f"{ts} {host_id} sp-runner-run[52]: [entrypoint] configuring {host_id}-slot-{slot}",
        ][-lines:],
        "time": time.time(),
    }


def load() -> None:
    now = time.time()
    hosts = [h.id for h in CFG.hosts][:2] or ["build-farm", "build-farm-2"]
    h0, h1 = (hosts + ["build-farm-2"])[:2]

    fleet.FLEET[h0] = {
        "host_id": h0, "reachable": True, "last_seen": now, "unreachable_since": None,
        "reaped": False, "error": None,
        "state": _host_state(h0, [
            _slot(1, "running", job="deploy / build-backend (push)", restarts=2),
            _slot(2, "running", job="ci / test (pull_request)", restarts=0),
            _slot(3, "idle", restarts=1),
            _slot(4, "restarting", restarts=7),
        ]),
    }
    fleet.FLEET[h1] = {
        "host_id": h1, "reachable": True, "last_seen": now, "unreachable_since": None,
        "reaped": False, "error": None,
        "state": _host_state(h1, [
            _slot(1, "running", job="ci / lint (pull_request)", restarts=0, group=""),
            _slot(2, "idle", restarts=0, group=""),
            _slot(3, "idle", restarts=3, group=""),
        ]),
    }
    fleet.FLEET["_meta"] = {
        "reconciled_at": now - 3, "github_error": None, "github_runner_count": 7,
        "orphans": [{"id": 388, "name": f"{h0}-slot-9", "host": h0, "slot": 9}],
    }

    def mounts(pct_home, pct_root, home_total=207, root_total=42):
        return [
            {"mount": "/home", "total_bytes": home_total * 2**30,
             "used_bytes": int(home_total * pct_home / 100 * 2**30),
             "free_bytes": int(home_total * (1 - pct_home / 100) * 2**30), "used_pct": pct_home},
            {"mount": "/", "total_bytes": root_total * 2**30,
             "used_bytes": int(root_total * pct_root / 100 * 2**30),
             "free_bytes": int(root_total * (1 - pct_root / 100) * 2**30), "used_pct": pct_root},
        ]

    df = {"Images": [{}, {}, {}], "Volumes": [
        {"VolumeName": "sp-tool-1", "Size": 3.1 * 2**30, "Links": 1},
        {"VolumeName": "sp-tool-2", "Size": 2.7 * 2**30, "Links": 1},
        {"VolumeName": "sp-pnpm", "Size": 4.4 * 2**30, "Links": 3},
    ]}
    disk.DISK[h0] = {"mount": "/home", "used_pct": 41.0, "total": 207 * 2**30,
                     "used": int(0.41 * 207 * 2**30), "level": "ok", "since": now - 9000,
                     "all_mounts": mounts(41.0, 8.0), "podman_df": df}
    disk.DISK[h1] = {"mount": "/home", "used_pct": 78.0, "total": 120 * 2**30,
                     "used": int(0.78 * 120 * 2**30), "level": "warn", "since": now - 1800,
                     "all_mounts": mounts(78.0, 22.0, home_total=120, root_total=60), "podman_df": df}

    # 30 days of samples so the sparkline is populated
    with db.conn() as c:
        c.execute("DELETE FROM disk_samples")
        rows = []
        for hid, base, amp, total in ((h0, 34, 9, 207 * 2**30), (h1, 62, 16, 120 * 2**30)):
            for i in range(30 * 24):
                ts = now - (30 * 24 - i) * 3600
                pct = base + amp * (0.5 + 0.5 * math.sin(i / 22)) + (i / (30 * 24)) * 6
                rows.append((ts, hid, "/home", total, int(total * pct / 100)))
        c.executemany("INSERT INTO disk_samples(ts, host_id, mount, total_bytes, used_bytes) VALUES (?,?,?,?,?)", rows)

    for kind, msg, hh, ss, ago in [
        ("disk.warn", f"{h1} /home at 78.0% (ok -> warn)", h1, None, 1750),
        ("slot.crashloop", f"{h0}/slot-4 has 20 restarts", h0, 4, 2400),
        ("host.recovered", f"{h1} agent is back", h1, None, 8000),
        ("slot.offline", f"{h0}/slot-9 went offline", h0, 9, 3600),
        ("host.degraded", f"{h1} agent unreachable: timeout", h1, None, 8200),
    ]:
        db.add_event(now - ago, kind, msg, hh, ss, {})

    for act, tgt, det, res, ago in [
        ("slot-create", h0, "slots [1, 2, 3, 4], labels=self-hosted,linux,x64,podman", "ok", 5400),
        ("mint-token", f"{h0}/{h0}-slot-1", None, "ok", 240),
        ("slot-drain", f"{h1}/slot-2", None, "ok", 900),
        ("disk-watermark", h1, "/home at 78.0% -> warn", "ok", 1750),
        ("reap-orphan", f"{h0}-slot-8", "runner id 371", "ok", 3600),
        ("auto-prune", h1, "triggered at 78.0%: reclaimed 1.9GB", "ok", 1740),
        ("login", "admin", None, "ok", 60),
    ]:
        db.audit("admin" if act in ("slot-create", "slot-drain", "reap-orphan", "login") else "system",
                 act, target=tgt, detail=det, result=res)
        with db.conn() as c:
            c.execute("UPDATE audit SET ts = ? WHERE id = (SELECT MAX(id) FROM audit)", (now - ago,))
