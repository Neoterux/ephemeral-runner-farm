"""
SP runner farm — per-host agent.

The only component that touches this host. Runs as the unprivileged ghrunner
user inside its own `systemd --user` manager, so it drives runner slots with
plain `systemctl --user` and inspects containers with plain `podman` — no root,
no sudo. The manager talks to exactly this API and never SSHes in.

Endpoints (all except /health and /internal/* require Bearer <listen_token>):

  GET  /health                     liveness, unauthenticated
  GET  /state                      slot units + container status + versions
  GET  /disk                       filesystem usage + `podman system df`
  POST /slots/{n}/{action}         start|stop|restart|drain|clear-cache
  PUT  /slots/{n}                  create/update slot config (labels, group, limits, tag)
  DELETE /slots/{n}                remove slot entirely (config + cache volume)
  POST /prune                      `podman system prune`
  GET  /journal/{unit}?lines=      recent journal for a unit
  POST /internal/slot-token/{n}    loopback-only: mint a registration token via the manager
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 (dev only; hosts run 3.11)
    import tomli as tomllib
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

VERSION = "1.0.0"
CONFIG_PATH = Path(os.environ.get("FARM_AGENT_CONFIG", str(Path.home() / ".config/sp-runner/agent.toml")))
CFG_DIR = Path.home() / ".config/sp-runner"
USER_UNIT_DIR = Path.home() / ".config/systemd/user"

# --------------------------------------------------------------------------- config


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("rb") as fh:
        cfg = tomllib.load(fh)
    for key in ("host_id", "github_url", "manager_url", "manager_token", "listen_token"):
        if not cfg.get(key):
            sys.exit(f"agent config missing required key: {key}")
    cfg.setdefault("verify_tls", False)
    cfg.setdefault("disk_mounts", ["/home", "/"])
    cfg.setdefault("toolcache_idle_days", 14)
    return cfg


CFG = load_config()
app = FastAPI(title="sp-runner-agent", version=VERSION)


# --------------------------------------------------------------------------- auth


async def require_token(request: Request) -> None:
    header = request.headers.get("authorization", "")
    expected = f"Bearer {CFG['listen_token']}"
    # constant-time-ish compare
    if len(header) != len(expected) or not _consteq(header, expected):
        raise HTTPException(status_code=401, detail="bad or missing bearer token")


def _consteq(a: str, b: str) -> bool:
    result = 0
    for x, y in zip(a.encode(), b.encode()):
        result |= x ^ y
    return result == 0


def require_loopback(request: Request) -> None:
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="loopback only")


# --------------------------------------------------------------------------- shell


async def run(*argv: str, timeout: float = 30.0, check: bool = False) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
    except FileNotFoundError:
        if check:
            raise HTTPException(status_code=500, detail=f"{argv[0]}: not found")
        return 127, "", f"{argv[0]}: not found"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail=f"timeout: {' '.join(argv)}")
    rc = proc.returncode or 0
    if check and rc != 0:
        raise HTTPException(status_code=500, detail=f"{' '.join(argv)} exited {rc}: {err.decode().strip()}")
    return rc, out.decode(), err.decode()


async def systemctl(*args: str, **kw: Any) -> tuple[int, str, str]:
    return await run("systemctl", "--user", *args, **kw)


async def podman_json(*args: str, timeout: float = 30.0) -> Any:
    rc, out, err = await run("podman", *args, "--format", "json", timeout=timeout)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"podman {' '.join(args)}: {err.strip()}")
    return json.loads(out or "null")


# --------------------------------------------------------------------------- slots


SLOT_RE = re.compile(r"^slot-(\d+)\.env$")


def known_slots() -> list[int]:
    return sorted(
        int(m.group(1)) for f in CFG_DIR.glob("slot-*.env") if (m := SLOT_RE.match(f.name))
    )


def read_slot_env(n: int) -> dict[str, str]:
    path = CFG_DIR / f"slot-{n}.env"
    data: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip()
    return data


def write_slot_env(n: int, values: dict[str, str]) -> None:
    path = CFG_DIR / f"slot-{n}.env"
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text(body)
    tmp.chmod(0o600)
    tmp.rename(path)


async def container_status(n: int) -> dict[str, Any]:
    """Local view of what slot n is doing right now, from podman + container logs."""
    name = f"sp-runner-{n}"
    rc, out, _ = await run("podman", "inspect", name, "--format",
                           "{{.State.Status}}|{{.State.StartedAt}}|{{.Image}}", timeout=10)
    if rc != 0:
        return {"container": None, "phase": "between-jobs", "job": None}
    status, started, image = (out.strip().split("|", 2) + ["", "", ""])[:3]
    rc, logs, _ = await run("podman", "logs", "--tail", "40", name, timeout=10)
    phase, job = "starting", None
    if "Listening for Jobs" in logs and "Running job:" not in logs.split("Listening for Jobs")[-1]:
        phase = "idle"
    m = None
    for line in reversed(logs.splitlines()):
        if line.startswith("Running job:"):
            m = line.split(":", 1)[1].strip()
            break
        if "Job " in line and "completed with result" in line:
            break
    if m:
        phase, job = "running-job", m
    return {"container": status, "started_at": started, "image": image, "phase": phase, "job": job}


async def slot_view(n: int) -> dict[str, Any]:
    env = read_slot_env(n)
    unit = f"sp-runner@{n}.service"
    rc, out, _ = await systemctl("show", unit, "--property=ActiveState,SubState,UnitFileState,NRestarts,ExecMainStatus")
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    cs = await container_status(n)
    active = props.get("ActiveState", "unknown")
    if active == "active" and cs["phase"] == "running-job":
        state = "running"
    elif active == "active":
        state = "idle"
    elif active == "activating":
        state = "restarting"
    elif props.get("UnitFileState") in ("disabled", "") and env.get("ENABLED") == "false":
        state = "drained" if (CFG_DIR / f"slot-{n}.drain").exists() else "stopped"
    else:
        state = "offline"
    return {
        "slot": n,
        "state": state,
        "enabled": env.get("ENABLED", "true") == "true",
        "labels": env.get("RUNNER_LABELS", ""),
        "group": env.get("RUNNER_GROUP", ""),
        "image_tag": env.get("IMAGE_TAG", "latest"),
        "memory": env.get("SLOT_MEMORY", ""),
        "cpus": env.get("SLOT_CPUS", ""),
        "restarts": int(props.get("NRestarts", "0") or 0),
        "active_state": active,
        "sub_state": props.get("SubState", ""),
        **cs,
    }


# --------------------------------------------------------------------------- routes


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"host_id": CFG["host_id"], "status": "ok", "version": VERSION, "time": time.time()}


@app.get("/state", dependencies=[Depends(require_token)])
async def state() -> dict[str, Any]:
    slots = [await slot_view(n) for n in known_slots()]
    _, pv, _ = await run("podman", "--version", timeout=10)
    _, rv, _ = await run("uname", "-r", timeout=10)
    return {
        "host_id": CFG["host_id"],
        "version": VERSION,
        "podman": pv.strip(),
        "kernel": rv.strip(),
        "slots": slots,
        "time": time.time(),
    }


@app.get("/disk", dependencies=[Depends(require_token)])
async def disk() -> dict[str, Any]:
    mounts = []
    for mp in CFG["disk_mounts"]:
        try:
            st = os.statvfs(mp)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - (st.f_bfree * st.f_frsize)
        mounts.append({
            "mount": mp, "total_bytes": total, "used_bytes": used, "free_bytes": free,
            "used_pct": round(used / total * 100, 1) if total else 0.0,
        })
    # `podman system df -v` can't combine with --format json, so gather the
    # summary and the per-volume detail separately.
    df: dict[str, Any] | None = None
    try:
        summary = await podman_json("system", "df", timeout=45)  # list of {Type,Total,Size,Reclaimable}
        vols = await podman_json("volume", "ls", timeout=20)     # list of {Name,Driver,Mountpoint,...}
        vsizes = {}
        for v in vols or []:
            rc, out, _ = await run("podman", "volume", "inspect", v["Name"],
                                   "--format", "{{.Mountpoint}}", timeout=10)
            mp2 = out.strip()
            if rc == 0 and mp2 and Path(mp2).is_dir():
                vsizes[v["Name"]] = sum(f.stat().st_size for f in Path(mp2).rglob("*") if f.is_file())
        df = {
            "summary": summary,
            "volumes": [{"VolumeName": v["Name"], "Size": vsizes.get(v["Name"], 0),
                         "Links": 1 if v.get("Name", "").startswith(("sp-tool-", "sp-pnpm")) else 0}
                        for v in (vols or [])],
        }
    except (HTTPException, KeyError, OSError):
        df = None
    return {"host_id": CFG["host_id"], "mounts": mounts, "podman_df": df, "time": time.time()}


VALID_ACTIONS = {"start", "stop", "restart", "drain", "clear-cache"}


@app.post("/slots/{n}/{action}", dependencies=[Depends(require_token)])
async def slot_action(n: int, action: str) -> dict[str, Any]:
    if action not in VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action must be one of {sorted(VALID_ACTIONS)}")
    unit = f"sp-runner@{n}.service"
    if not (CFG_DIR / f"slot-{n}.env").exists():
        raise HTTPException(status_code=404, detail=f"slot {n} not configured")

    if action == "start":
        env = read_slot_env(n)
        env["ENABLED"] = "true"
        write_slot_env(n, env)
        (CFG_DIR / f"slot-{n}.drain").unlink(missing_ok=True)
        await systemctl("enable", "--now", unit, check=True)
    elif action == "stop":
        env = read_slot_env(n)
        env["ENABLED"] = "false"
        write_slot_env(n, env)
        await systemctl("disable", "--now", unit)
    elif action == "restart":
        await systemctl("restart", unit, check=True)
    elif action == "drain":
        # Marker consumed by sp-runner-prestart at the next job boundary.
        (CFG_DIR / f"slot-{n}.drain").write_text(f"{time.time()}\n")
    elif action == "clear-cache":
        rc, mp, _ = await run("podman", "volume", "inspect", f"sp-tool-{n}",
                              "--format", "{{.Mountpoint}}", timeout=10)
        if rc == 0 and (path := mp.strip()) and Path(path).is_dir():
            for child in Path(path).iterdir():
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
    return {"slot": n, "action": action, "result": "ok", "view": await slot_view(n)}


def _norm_memory(v: str) -> str:
    """podman --memory needs a unit. A bare number means gigabytes."""
    v = str(v).strip().lower()
    if re.fullmatch(r"\d+", v):
        return f"{v}g"
    if re.fullmatch(r"\d+(\.\d+)?[bkmg]", v):
        return v
    raise HTTPException(status_code=400, detail=f"bad memory value {v!r} — use e.g. '12g', '512m'")


def _norm_cpus(v: str) -> str:
    v = str(v).strip()
    if not re.fullmatch(r"\d+(\.\d+)?", v) or float(v) <= 0:
        raise HTTPException(status_code=400, detail=f"bad cpus value {v!r} — use e.g. '2' or '1.5'")
    return v


@app.put("/slots/{n}", dependencies=[Depends(require_token)])
async def slot_upsert(n: int, body: dict[str, Any]) -> dict[str, Any]:
    existing = read_slot_env(n)
    env = {
        "ENABLED": str(body.get("enabled", existing.get("ENABLED", "true") == "true")).lower(),
        "GITHUB_URL": body.get("github_url", existing.get("GITHUB_URL", CFG["github_url"])),
        "RUNNER_LABELS": body.get("labels", existing.get("RUNNER_LABELS", "self-hosted,linux,x64,podman")),
        "RUNNER_GROUP": body.get("group", existing.get("RUNNER_GROUP", "")),
        "IMAGE_TAG": body.get("image_tag", existing.get("IMAGE_TAG", "latest")),
        "SLOT_MEMORY": _norm_memory(body.get("memory", existing.get("SLOT_MEMORY", "12g"))),
        "SLOT_CPUS": _norm_cpus(body.get("cpus", existing.get("SLOT_CPUS", "2"))),
    }
    write_slot_env(n, env)
    await run("systemctl", "--user", "daemon-reload", timeout=15)
    if env["ENABLED"] == "true":
        (CFG_DIR / f"slot-{n}.drain").unlink(missing_ok=True)
        await systemctl("enable", "--now", f"sp-runner@{n}.service", check=True)
    return {"slot": n, "result": "ok", "view": await slot_view(n)}


@app.delete("/slots/{n}", dependencies=[Depends(require_token)])
async def slot_delete(n: int) -> dict[str, Any]:
    unit = f"sp-runner@{n}.service"
    await systemctl("disable", "--now", unit)
    await run("podman", "rm", "-f", f"sp-runner-{n}", timeout=20)
    await run("podman", "volume", "rm", f"sp-tool-{n}", timeout=20)
    (CFG_DIR / f"slot-{n}.env").unlink(missing_ok=True)
    (CFG_DIR / f"slot-{n}.drain").unlink(missing_ok=True)
    return {"slot": n, "result": "deleted"}


@app.post("/prune", dependencies=[Depends(require_token)])
async def prune() -> dict[str, Any]:
    rc, out, err = await run("podman", "system", "prune", "--force", "--filter", "until=24h", timeout=120)
    return {"result": "ok" if rc == 0 else "error", "output": out or err}


@app.get("/journal/{unit}", dependencies=[Depends(require_token)])
async def journal(unit: str, lines: int = 100) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9@._-]+", unit):
        raise HTTPException(status_code=400, detail="bad unit name")
    rc, out, err = await run("journalctl", "--user", "-u", unit, "-n", str(min(lines, 1000)),
                             "--no-pager", "-o", "short-iso", timeout=20)
    return {"unit": unit, "lines": (out or err).splitlines()}


@app.get("/slots/{n}/logs", dependencies=[Depends(require_token)])
async def slot_logs(n: int, lines: int = 200) -> dict[str, Any]:
    """Last <lines> of the current job container plus the slot's systemd journal
    (which spans restarts and shows the prestart/token phase)."""
    lines = max(10, min(lines, 2000))
    name = f"sp-runner-{n}"
    _, clog, cerr = await run("podman", "logs", "--tail", str(lines), "--timestamps", name, timeout=15)
    _, jout, jerr = await run("journalctl", "--user", "-u", f"sp-runner@{n}.service",
                              "-n", str(lines), "--no-pager", "-o", "short-iso", timeout=20)
    rc, _, _ = await run("podman", "container", "exists", name, timeout=5)
    return {
        "slot": n,
        "container_running": rc == 0,
        "container": (clog or cerr or "(no container — between jobs)").splitlines(),
        "journal": (jout or jerr or "").splitlines(),
        "time": time.time(),
    }


@app.post("/internal/slot-token/{n}", dependencies=[Depends(require_loopback)])
async def slot_token(n: int, body: dict[str, Any]) -> JSONResponse:
    """Called by sp-runner-prestart (loopback). Proxies to the manager, which
    holds the GitHub App key and mints a short-lived registration token."""
    runner_name = body.get("runner_name") or f"{CFG['host_id']}-slot-{n}"
    verify = CFG["verify_tls"] if CFG["verify_tls"] else False
    async with httpx.AsyncClient(verify=verify, timeout=30) as client:
        resp = await client.post(
            f"{CFG['manager_url'].rstrip('/')}/api/agent/registration-token",
            headers={"authorization": f"Bearer {CFG['manager_token']}"},
            json={"host_id": CFG["host_id"], "runner_name": runner_name},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"manager token mint failed: {resp.status_code} {resp.text}")
    return JSONResponse(resp.json())
