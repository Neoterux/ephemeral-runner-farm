"""Manager configuration — parsed once from the TOML pointed at by
$SP_MANAGER_CONFIG (default ~/.config/sp-runner/manager.toml)."""
from __future__ import annotations

import os
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 (dev only; hosts run 3.11)
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("SP_MANAGER_CONFIG", str(Path.home() / ".config/sp-runner/manager.toml")))
STATE_DIR = Path(os.environ.get("STATE_DIRECTORY", str(Path.home() / ".local/state/sp-runner-manager")).split(":")[0])


@dataclass(frozen=True)
class Host:
    id: str
    address: str
    agent_url: str
    agent_token: str
    manager_token: str
    enabled: bool = True
    agent_tls_fingerprint: str | None = None


@dataclass(frozen=True)
class Watermarks:
    warn: int = 75
    prune: int = 85
    freeze: int = 92


@dataclass(frozen=True)
class Config:
    secret_key: str
    session_hours: int
    secure_cookies: bool
    gh_org: str
    gh_app_id: int
    gh_installation_id: int
    gh_private_key_file: str
    watermarks: Watermarks
    reap_grace_minutes: int
    reconcile_seconds: int
    disk_sample_seconds: int
    disk_history_days: int
    hosts: list[Host] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return STATE_DIR / "manager.db"

    def host(self, host_id: str) -> Host | None:
        return next((h for h in self.hosts if h.id == host_id), None)


def load() -> Config:
    try:
        with CONFIG_PATH.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        sys.exit(f"manager config not found: {CONFIG_PATH}")

    srv = raw.get("server", {})
    gh = raw.get("github", {})
    wm = raw.get("watermarks", {})
    fl = raw.get("fleet", {})

    if not srv.get("secret_key") or "REPLACE" in srv.get("secret_key", ""):
        sys.exit("server.secret_key is unset — run: openssl rand -base64 48")
    if not gh.get("app_id") or not gh.get("installation_id"):
        # Non-fatal: the UI + disk monitoring run without GitHub; only runner
        # registration needs the App. deploy.sh can install before it exists.
        print("WARNING: github.app_id / github.installation_id unset — "
              "runner registration disabled until they are set", file=sys.stderr)

    hosts = [
        Host(
            id=h["id"], address=h["address"], agent_url=h["agent_url"],
            agent_token=h["agent_token"], manager_token=h["manager_token"],
            enabled=h.get("enabled", True),
            agent_tls_fingerprint=h.get("agent_tls_fingerprint") or None,
        )
        for h in raw.get("hosts", [])
    ]
    if not hosts:
        sys.exit("no [[hosts]] configured")

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    return Config(
        secret_key=srv["secret_key"],
        session_hours=int(srv.get("session_hours", 12)),
        secure_cookies=bool(srv.get("secure_cookies", False)),
        gh_org=gh["org"],
        gh_app_id=int(gh["app_id"]),
        gh_installation_id=int(gh["installation_id"]),
        gh_private_key_file=gh["private_key_file"],
        watermarks=Watermarks(int(wm.get("warn", 75)), int(wm.get("prune", 85)), int(wm.get("freeze", 92))),
        reap_grace_minutes=int(fl.get("reap_grace_minutes", 15)),
        reconcile_seconds=int(fl.get("reconcile_seconds", 30)),
        disk_sample_seconds=int(fl.get("disk_sample_seconds", 60)),
        disk_history_days=int(fl.get("disk_history_days", 30)),
        hosts=hosts,
    )


CFG = load()
