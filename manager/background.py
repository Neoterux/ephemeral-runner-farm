"""Periodic loops: fleet reconcile, disk sampling, sample pruning."""
from __future__ import annotations

import asyncio
import contextlib
import time

import db
import disk
import fleet
from config import CFG


async def _every(seconds: int, coro_fn, name: str) -> None:
    while True:
        start = time.time()
        try:
            await coro_fn()
        except Exception as exc:  # noqa: BLE001 — a loop must never die
            db.audit("system", f"{name}-error", detail=str(exc)[:400], result="error")
        elapsed = time.time() - start
        await asyncio.sleep(max(1.0, seconds - elapsed))


async def _prune_samples_daily() -> None:
    while True:
        removed = db.prune_disk_samples(CFG.disk_history_days)
        if removed:
            db.audit("system", "prune-disk-samples", detail=f"removed {removed} rows")
        await asyncio.sleep(86400)


class Background:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        # Prime once so the first page load has data.
        with contextlib.suppress(Exception):
            await fleet.reconcile()
        with contextlib.suppress(Exception):
            await disk.sample_and_enforce()
        self._tasks = [
            asyncio.create_task(_every(CFG.reconcile_seconds, fleet.reconcile, "reconcile")),
            asyncio.create_task(_every(CFG.disk_sample_seconds, disk.sample_and_enforce, "disk-sample")),
            asyncio.create_task(_prune_samples_daily()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


BG = Background()
