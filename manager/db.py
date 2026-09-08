"""SQLite persistence: users, sessions-are-cookies (no table), audit log,
disk samples, and a small key/value store for watermark state."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from config import CFG

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    pw_hash       TEXT NOT NULL,
    created_at    REAL NOT NULL,
    pw_changed_at REAL NOT NULL,
    must_change   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    username  TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT,
    detail    TEXT,
    result    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts DESC);
CREATE TABLE IF NOT EXISTS disk_samples (
    ts          REAL NOT NULL,
    host_id     TEXT NOT NULL,
    mount       TEXT NOT NULL,
    total_bytes INTEGER NOT NULL,
    used_bytes  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS disk_samples_lookup ON disk_samples(host_id, mount, ts DESC);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL,
    host_id TEXT,
    slot    INTEGER,
    data    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ts ON events(id DESC);
"""


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(CFG.db_path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


# --- users ---------------------------------------------------------------


def get_user(username: str) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def user_count() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username: str, pw_hash: str, must_change: bool = False) -> None:
    now = time.time()
    with conn() as c:
        c.execute(
            "INSERT INTO users(username, pw_hash, created_at, pw_changed_at, must_change) VALUES (?,?,?,?,?)",
            (username, pw_hash, now, now, int(must_change)),
        )


def set_password(username: str, pw_hash: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE users SET pw_hash = ?, pw_changed_at = ?, must_change = 0 WHERE username = ?",
            (pw_hash, time.time(), username),
        )


# --- audit --------------------------------------------------------------


def audit(username: str, action: str, target: str | None = None,
          detail: str | None = None, result: str = "ok") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO audit(ts, username, action, target, detail, result) VALUES (?,?,?,?,?,?)",
            (time.time(), username, action, target, detail, result),
        )


def recent_audit(limit: int = 100) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()


# --- disk samples ------------------------------------------------------


def add_disk_sample(host_id: str, mount: str, total: int, used: int, ts: float | None = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO disk_samples(ts, host_id, mount, total_bytes, used_bytes) VALUES (?,?,?,?,?)",
            (ts or time.time(), host_id, mount, total, used),
        )


def disk_series(host_id: str, mount: str, since_hours: int = 720) -> list[dict[str, Any]]:
    cutoff = time.time() - since_hours * 3600
    with conn() as c:
        rows = c.execute(
            "SELECT ts, total_bytes, used_bytes FROM disk_samples "
            "WHERE host_id = ? AND mount = ? AND ts >= ? ORDER BY ts",
            (host_id, mount, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def prune_disk_samples(keep_days: int) -> int:
    cutoff = time.time() - keep_days * 86400
    with conn() as c:
        cur = c.execute("DELETE FROM disk_samples WHERE ts < ?", (cutoff,))
        return cur.rowcount


# --- events ----------------------------------------------------------


def add_event(ts: float, kind: str, message: str, host_id: str | None,
              slot: int | None, data: dict[str, Any]) -> None:
    import json
    with conn() as c:
        c.execute(
            "INSERT INTO events(ts, kind, message, host_id, slot, data) VALUES (?,?,?,?,?,?)",
            (ts, kind, message, host_id, slot, json.dumps(data)),
        )


def recent_events(limit: int = 100, since_id: int = 0, kind_like: str | None = None) -> list[dict[str, Any]]:
    import json
    q = "SELECT id, ts, kind, message, host_id, slot, data FROM events WHERE id > ?"
    args: list[Any] = [since_id]
    if kind_like:
        q += " AND kind LIKE ?"
        args.append(kind_like.replace("*", "%"))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(min(limit, 1000))
    with conn() as c:
        rows = c.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d.pop("data") or "{}")
        out.append(d)
    return out


def prune_events(keep_days: int) -> int:
    cutoff = time.time() - keep_days * 86400
    with conn() as c:
        return c.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount


# --- kv ---------------------------------------------------------------


def kv_get(key: str, default: str | None = None) -> str | None:
    with conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (key, value))
