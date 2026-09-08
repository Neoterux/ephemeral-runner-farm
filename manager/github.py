"""GitHub App client — mints an installation token (cached, auto-renewed ~5 min
before expiry) and wraps the handful of org runner endpoints the manager uses."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import jwt

from config import CFG

_API = "https://api.github.com"


class GitHubApp:
    def __init__(self) -> None:
        # PEM is read lazily so the manager (UI, disk monitoring) still starts
        # before the GitHub App exists. Runner registration fails clearly until
        # the key + app_id/installation_id are in place.
        self._pem: str | None = None
        self._inst_token: str | None = None
        self._inst_exp: float = 0.0

    # -- auth --------------------------------------------------------------

    def _pem_text(self) -> str:
        if self._pem is None:
            p = Path(CFG.gh_private_key_file)
            if not p.exists():
                raise RuntimeError(
                    f"GitHub App private key not found at {p} — add it and set "
                    f"github.app_id / github.installation_id in manager.toml"
                )
            if not CFG.gh_app_id or not CFG.gh_installation_id:
                raise RuntimeError("github.app_id / github.installation_id are still 0 in manager.toml")
            self._pem = p.read_text()
        return self._pem

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": CFG.gh_app_id}
        return jwt.encode(payload, self._pem_text(), algorithm="RS256")

    async def _installation_token(self) -> str:
        if self._inst_token and time.time() < self._inst_exp - 300:
            return self._inst_token
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{_API}/app/installations/{CFG.gh_installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {self._app_jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        r.raise_for_status()
        data = r.json()
        self._inst_token = data["token"]
        self._inst_exp = time.mktime(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
        return self._inst_token

    async def _client(self) -> httpx.AsyncClient:
        token = await self._installation_token()
        return httpx.AsyncClient(
            base_url=_API,
            timeout=20,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    # -- runner endpoints -----------------------------------------------

    async def registration_token(self) -> dict[str, Any]:
        async with await self._client() as c:
            r = await c.post(f"/orgs/{CFG.gh_org}/actions/runners/registration-token")
        r.raise_for_status()
        return r.json()  # {"token": ..., "expires_at": ...}

    async def list_runners(self) -> list[dict[str, Any]]:
        runners: list[dict[str, Any]] = []
        page = 1
        async with await self._client() as c:
            while True:
                r = await c.get(f"/orgs/{CFG.gh_org}/actions/runners",
                                params={"per_page": 100, "page": page})
                r.raise_for_status()
                batch = r.json().get("runners", [])
                runners.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        return runners

    async def delete_runner(self, runner_id: int) -> None:
        async with await self._client() as c:
            r = await c.delete(f"/orgs/{CFG.gh_org}/actions/runners/{runner_id}")
        if r.status_code not in (204, 404):
            r.raise_for_status()

    async def list_runner_groups(self) -> list[dict[str, Any]]:
        async with await self._client() as c:
            r = await c.get(f"/orgs/{CFG.gh_org}/actions/runner-groups", params={"per_page": 100})
        if r.status_code == 403:
            return []  # Administration:read not granted — non-fatal
        r.raise_for_status()
        return r.json().get("runner_groups", [])


APP = GitHubApp()
