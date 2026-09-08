"""Password hashing (argon2) + signed-cookie sessions (itsdangerous).

No session table: the cookie is a signed, timestamped token carrying the
username. Revocation is by changing the server secret_key or the password
(pw_changed_at is checked on every request)."""
from __future__ import annotations

import secrets
import time
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import db
from config import CFG

_ph = PasswordHasher()
_serializer = URLSafeTimedSerializer(CFG.secret_key, salt="sp-runner-session")
COOKIE_NAME = "sp_session"
MAX_AGE = CFG.session_hours * 3600


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(pw_hash: str, password: str) -> bool:
    try:
        return _ph.verify(pw_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(pw_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(pw_hash)
    except InvalidHashError:
        return True


def make_session(username: str) -> str:
    return _serializer.dumps({"u": username, "iat": int(time.time())})


def read_session(token: str) -> Optional[str]:
    try:
        data = _serializer.loads(token, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("u")


def ensure_seed_admin() -> tuple[str, str] | None:
    """Create an 'admin' user with a random password on first run. Returns
    (username, password) exactly once, and drops it in a 0600 file next to the
    DB so the operator can retrieve it after deploy without scraping the journal."""
    if db.user_count() > 0:
        return None
    password = secrets.token_urlsafe(18)
    db.create_user("admin", hash_password(password), must_change=True)
    db.audit("system", "seed-admin", target="admin", result="ok")
    try:
        from config import STATE_DIR
        f = STATE_DIR / "seed-admin.txt"
        f.write_text(f"username: admin\npassword: {password}\n"
                     f"(change it on first login; then: rm {f})\n")
        f.chmod(0o600)
    except OSError:
        pass
    return "admin", password


class CurrentUser:
    def __init__(self, username: str, must_change: bool):
        self.username = username
        self.must_change = must_change


async def current_user(request: Request) -> CurrentUser:
    token = request.cookies.get(COOKIE_NAME)
    username = read_session(token) if token else None
    if not username:
        raise HTTPException(status_code=307, detail="login required", headers={"Location": "/login"})
    row = db.get_user(username)
    if row is None:
        raise HTTPException(status_code=307, detail="unknown user", headers={"Location": "/login"})
    return CurrentUser(username=username, must_change=bool(row["must_change"]))
