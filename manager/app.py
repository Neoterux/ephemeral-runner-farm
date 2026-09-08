"""
SP runner farm — web manager.

Runs on build-farm as a rootless `systemd --user` service. Owns the GitHub App
credential, the SQLite DB, the web UI, and the background reconcile/disk loops.
Talks to each host only through its agent HTTP API — never SSH, never root.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import agentclient
import auth
import db
import disk
import fleet
from background import BG
from config import CFG
from github import APP

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _datetimeformat(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


templates.env.filters["datetimeformat"] = _datetimeformat

app = FastAPI(title="sp-runner-manager")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# --------------------------------------------------------------------------- lifecycle


@app.on_event("startup")
async def _startup() -> None:
    db.init()
    seed = auth.ensure_seed_admin()
    if seed:
        # Printed to the journal so deploy.sh (which tails it) can show the operator.
        print(f"\n=== SEED ADMIN CREATED ===\nusername: {seed[0]}\npassword: {seed[1]}\n"
              f"(change it on first login)\n==========================\n", flush=True)
    await BG.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await BG.stop()


# --------------------------------------------------------------------------- helpers


def _tmpl(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    ctx["request"] = request
    ctx.setdefault("cfg", CFG)
    ctx.setdefault("now", time.time())
    return templates.TemplateResponse(name, ctx)


def _require_host(host_id: str):
    host = CFG.host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"unknown host {host_id}")
    return host


async def _user(request: Request) -> auth.CurrentUser:
    if os.environ.get("FARM_DEMO"):
        return auth.CurrentUser(username="demo", must_change=False)
    try:
        return await auth.current_user(request)
    except HTTPException as exc:
        # Turn the 307 signal from auth into an actual redirect for browser routes.
        if exc.status_code == 307:
            raise HTTPException(status_code=307, detail="login", headers={"Location": "/login"})
        raise


# --------------------------------------------------------------------------- auth routes


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return _tmpl(request, "login.html", error=None)


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)) -> Any:
    row = db.get_user(username)
    if row is None or not auth.verify_password(row["pw_hash"], password):
        db.audit(username, "login", result="fail")
        return _tmpl(request, "login.html", error="Invalid credentials")
    if auth.needs_rehash(row["pw_hash"]):
        db.set_password(username, auth.hash_password(password))
    db.audit(username, "login", result="ok")
    resp = RedirectResponse("/fleet", status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, auth.make_session(username), max_age=auth.MAX_AGE,
                    httponly=True, samesite="lax", secure=CFG.secure_cookies)
    return resp


@app.post("/logout")
async def logout(request: Request, user: auth.CurrentUser = Depends(_user)) -> Any:
    db.audit(user.username, "logout")
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.post("/password")
async def change_password(
    request: Request,
    current: str = Form(...), new: str = Form(...), confirm: str = Form(...),
    user: auth.CurrentUser = Depends(_user),
) -> Any:
    row = db.get_user(user.username)
    if not auth.verify_password(row["pw_hash"], current):
        db.audit(user.username, "password-change", result="fail")
        ctx = await _settings_ctx(request, user) | {"pw_error": "Current password is wrong"}
        return _tmpl(request, "settings.html", **ctx)
    if new != confirm or len(new) < 12:
        ctx = await _settings_ctx(request, user) | {"pw_error": "New passwords must match and be at least 12 characters"}
        return _tmpl(request, "settings.html", **ctx)
    db.set_password(user.username, auth.hash_password(new))
    db.audit(user.username, "password-change", result="ok")
    return RedirectResponse("/settings", status_code=303)


# --------------------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
async def index() -> Any:
    return RedirectResponse("/fleet", status_code=307)


@app.get("/fleet", response_class=HTMLResponse)
async def fleet_page(request: Request, user: auth.CurrentUser = Depends(_user)) -> HTMLResponse:
    return _tmpl(request, "fleet.html", user=user, snap=fleet.snapshot(), disk=disk.snapshot())


@app.get("/fleet/rows", response_class=HTMLResponse)
async def fleet_rows(request: Request, user: auth.CurrentUser = Depends(_user)) -> HTMLResponse:
    return _tmpl(request, "_fleet_rows.html", user=user, snap=fleet.snapshot(), disk=disk.snapshot())


@app.get("/disks", response_class=HTMLResponse)
async def disks_page(request: Request, user: auth.CurrentUser = Depends(_user)) -> HTMLResponse:
    return _tmpl(request, "disks.html", user=user, disk=disk.snapshot())


@app.get("/disks/series")
async def disks_series(host: str, mount: str, user: auth.CurrentUser = Depends(_user)) -> JSONResponse:
    return JSONResponse({"host": host, "mount": mount,
                         "series": db.disk_series(host, mount, since_hours=CFG.disk_history_days * 24)})


@app.get("/health", response_class=HTMLResponse)
async def health_page(request: Request, user: auth.CurrentUser = Depends(_user)) -> HTMLResponse:
    return _tmpl(request, "health.html", user=user, snap=fleet.snapshot(),
                 audit=db.recent_audit(150))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: auth.CurrentUser = Depends(_user)) -> HTMLResponse:
    return _tmpl(request, "settings.html", **await _settings_ctx(request, user))


@app.get("/logs/{host_id}/{slot}", response_class=HTMLResponse)
async def slot_logs_page(request: Request, host_id: str, slot: int, lines: int = 200,
                         user: auth.CurrentUser = Depends(_user)) -> HTMLResponse:
    host = _require_host(host_id)
    lines = max(20, min(lines, 2000))
    if os.environ.get("FARM_DEMO"):
        import demo
        logs = demo.slot_logs(host_id, slot, lines)
    else:
        try:
            logs = await agentclient.slot_logs(host, slot, lines)
        except agentclient.AgentError as exc:
            logs = {"slot": slot, "container_running": False, "error": str(exc),
                    "container": [], "journal": []}
    partial = request.headers.get("hx-request") == "true"
    return _tmpl(request, "_logs.html" if partial else "logs.html",
                 user=user, host_id=host_id, slot=slot, lines=lines, logs=logs)


async def _settings_ctx(request: Request, user: auth.CurrentUser) -> dict[str, Any]:
    try:
        groups = await APP.list_runner_groups()
        gh_ok, gh_err = True, None
    except Exception as exc:  # noqa: BLE001
        groups, gh_ok, gh_err = [], False, str(exc)
    return {"user": user, "runner_groups": groups, "gh_ok": gh_ok, "gh_err": gh_err, "pw_error": None}


# --------------------------------------------------------------------------- slot actions


@app.post("/slots/{host_id}/{slot}/{action}")
async def slot_action(host_id: str, slot: int, action: str,
                      user: auth.CurrentUser = Depends(_user)) -> JSONResponse:
    host = _require_host(host_id)
    if action == "start" and disk.is_frozen(host_id):
        raise HTTPException(status_code=409, detail="host is disk-frozen — free space before starting slots")
    try:
        res = await agentclient.slot_action(host, slot, action)
        db.audit(user.username, f"slot-{action}", target=f"{host_id}/slot-{slot}")
        return JSONResponse(res)
    except agentclient.AgentError as exc:
        db.audit(user.username, f"slot-{action}", target=f"{host_id}/slot-{slot}", detail=str(exc), result="error")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/slots/{host_id}/create")
async def slot_create(
    host_id: str,
    count: int = Form(1),
    labels: str = Form("self-hosted,linux,x64,podman"),
    group: str = Form(""),
    image_tag: str = Form("latest"),
    memory: str = Form("12g"),
    cpus: str = Form("2"),
    user: auth.CurrentUser = Depends(_user),
) -> Any:
    host = _require_host(host_id)
    if disk.is_frozen(host_id):
        raise HTTPException(status_code=409, detail="host is disk-frozen")
    state = await agentclient.get_state(host)
    used = {s["slot"] for s in state.get("slots", [])}
    created = []
    n = 1
    for _ in range(max(1, min(count, 16))):
        while n in used:
            n += 1
        body = {"enabled": True, "labels": labels, "group": group,
                "image_tag": image_tag, "memory": memory, "cpus": cpus,
                "github_url": f"https://github.com/{CFG.gh_org}"}
        await agentclient.slot_upsert(host, n, body)
        created.append(n)
        used.add(n)
    db.audit(user.username, "slot-create", target=host_id, detail=f"slots {created}, labels={labels}, group={group}")
    return RedirectResponse("/fleet", status_code=303)


@app.post("/slots/{host_id}/{slot}/edit")
async def slot_edit(host_id: str, slot: int,
                    labels: str = Form(...), group: str = Form(""),
                    image_tag: str = Form("latest"), memory: str = Form("12g"),
                    cpus: str = Form("2"), user: auth.CurrentUser = Depends(_user)) -> Any:
    host = _require_host(host_id)
    await agentclient.slot_upsert(host, slot, {
        "labels": labels, "group": group, "image_tag": image_tag,
        "memory": memory, "cpus": cpus,
    })
    db.audit(user.username, "slot-edit", target=f"{host_id}/slot-{slot}",
             detail=f"labels={labels} group={group} tag={image_tag} mem={memory} cpus={cpus}")
    return RedirectResponse("/fleet", status_code=303)


@app.post("/slots/{host_id}/{slot}/delete")
async def slot_delete(host_id: str, slot: int, user: auth.CurrentUser = Depends(_user)) -> Any:
    host = _require_host(host_id)
    await agentclient.slot_delete(host, slot)
    db.audit(user.username, "slot-delete", target=f"{host_id}/slot-{slot}")
    return RedirectResponse("/fleet", status_code=303)


@app.post("/hosts/{host_id}/prune")
async def host_prune(host_id: str, user: auth.CurrentUser = Depends(_user)) -> Any:
    host = _require_host(host_id)
    res = await agentclient.prune(host)
    db.audit(user.username, "host-prune", target=host_id, detail=str(res)[:200])
    return RedirectResponse("/disks", status_code=303)


@app.post("/health/reap")
async def health_reap(runner_id: int = Form(...), name: str = Form(...),
                      user: auth.CurrentUser = Depends(_user)) -> Any:
    await fleet.reap_orphan(user.username, runner_id, name)
    return RedirectResponse("/health", status_code=303)


# --------------------------------------------------------------------------- agent-facing


@app.post("/api/agent/registration-token")
async def agent_registration_token(request: Request, body: dict[str, Any]) -> JSONResponse:
    """Called by an agent's loopback token proxy. Authenticated with that
    host's manager_token (distinct from user sessions)."""
    host_id = body.get("host_id", "")
    host = CFG.host(host_id)
    header = request.headers.get("authorization", "")
    if host is None or header != f"Bearer {host.manager_token}":
        raise HTTPException(status_code=401, detail="bad host or token")
    try:
        tok = await APP.registration_token()
    except Exception as exc:  # noqa: BLE001 — App not configured yet, or GitHub down
        db.audit("system", "mint-token", target=host_id, detail=str(exc)[:200], result="error")
        raise HTTPException(status_code=503, detail=f"cannot mint registration token: {exc}") from exc
    db.audit("system", "mint-token", target=f"{host_id}/{body.get('runner_name', '?')}")
    return JSONResponse({"token": tok["token"], "expires_at": tok["expires_at"],
                         "github_url": f"https://github.com/{CFG.gh_org}"})
