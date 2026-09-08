"""Small admin CLI for the manager. Run as the ghrunner user on the manager host:

    cd ~/sp-runner-manager
    FARM_MANAGER_CONFIG=~/.config/sp-runner/manager.toml .venv/bin/python manage.py <cmd>

Commands:
    reset-password <user> [newpass]   set a password (random + printed if omitted)
    list-users                        show accounts
    show-seed                         print the seed-admin file if it still exists
"""
from __future__ import annotations

import secrets
import sys

import auth
import db
from config import STATE_DIR


def reset_password(user: str, newpass: str | None = None) -> None:
    if db.get_user(user) is None:
        sys.exit(f"no such user: {user}")
    pw = newpass or secrets.token_urlsafe(18)
    db.set_password(user, auth.hash_password(pw))
    db.audit("cli", "password-reset", target=user)
    print(f"password for {user} set to:\n  {pw}")


def list_users() -> None:
    with db.conn() as c:
        for row in c.execute("SELECT username, created_at, pw_changed_at, must_change FROM users"):
            print(dict(row))


def show_seed() -> None:
    f = STATE_DIR / "seed-admin.txt"
    print(f.read_text() if f.exists() else "(no seed file — admin password has been changed)")


def main() -> None:
    db.init()
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd, rest = args[0], args[1:]
    if cmd == "reset-password" and rest:
        reset_password(rest[0], rest[1] if len(rest) > 1 else None)
    elif cmd == "list-users":
        list_users()
    elif cmd == "show-seed":
        show_seed()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
