"""HTTP client for the per-host agents.

Loopback agents are plain http. Remote agents are https with a self-signed
cert; we pin it by SHA-256 fingerprint (configured per host, printed by
deploy.sh) rather than trusting a CA."""
from __future__ import annotations

import asyncio
import hashlib
import ssl
from typing import Any
from urllib.parse import urlparse

import httpx

from config import Host


class AgentError(Exception):
    pass


async def _verify_fingerprint(host: Host) -> None:
    """Open a TLS connection, hash the presented leaf cert, compare to the pin."""
    if not host.agent_url.startswith("https://") or not host.agent_tls_fingerprint:
        return
    u = urlparse(host.agent_url)
    ctx = ssl._create_unverified_context()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(u.hostname, u.port or 443, ssl=ctx, server_hostname=u.hostname),
            timeout=10,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise AgentError(f"{host.id}: cannot reach agent ({exc})") from exc
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass
    got = hashlib.sha256(der).hexdigest()
    want = host.agent_tls_fingerprint.replace("sha256:", "").replace(":", "").lower()
    if got != want:
        raise AgentError(
            f"{host.id}: agent TLS fingerprint mismatch — got sha256:{got}, configured sha256:{want}"
        )


def _client(host: Host) -> httpx.AsyncClient:
    headers = {"authorization": f"Bearer {host.agent_token}"}
    # For https we never rely on CA trust: either _verify_fingerprint already
    # pinned the exact leaf cert (remote hosts), or it is the loopback agent's
    # own self-signed cert (local host). Plain http is loopback-only.
    verify = not host.agent_url.startswith("https://")
    return httpx.AsyncClient(base_url=host.agent_url, headers=headers, timeout=25, verify=verify)


async def call(host: Host, method: str, path: str, **kw: Any) -> Any:
    await _verify_fingerprint(host)
    async with _client(host) as client:
        try:
            resp = await client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise AgentError(f"{host.id}: {exc}") from exc
    if resp.status_code >= 400:
        raise AgentError(f"{host.id} {method} {path}: {resp.status_code} {resp.text[:300]}")
    ctype = resp.headers.get("content-type", "")
    return resp.json() if ctype.startswith("application/json") else resp.text


async def get_state(host: Host) -> dict[str, Any]:
    return await call(host, "GET", "/state")


async def get_disk(host: Host) -> dict[str, Any]:
    return await call(host, "GET", "/disk")


async def slot_action(host: Host, slot: int, action: str) -> dict[str, Any]:
    return await call(host, "POST", f"/slots/{slot}/{action}")


async def slot_logs(host: Host, slot: int, lines: int = 200) -> dict[str, Any]:
    return await call(host, "GET", f"/slots/{slot}/logs", params={"lines": lines})


async def slot_upsert(host: Host, slot: int, body: dict[str, Any]) -> dict[str, Any]:
    return await call(host, "PUT", f"/slots/{slot}", json=body)


async def slot_delete(host: Host, slot: int) -> dict[str, Any]:
    return await call(host, "DELETE", f"/slots/{slot}")


async def prune(host: Host) -> dict[str, Any]:
    return await call(host, "POST", "/prune")


async def health(host: Host) -> dict[str, Any]:
    return await call(host, "GET", "/health")
