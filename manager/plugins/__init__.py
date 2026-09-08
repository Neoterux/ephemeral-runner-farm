"""Plugin loader. Enabled plugins are listed in manager.toml:

    [plugins]
    enabled = ["webhook", "prometheus"]

    [plugins.webhook]
    ...plugin-specific...

Each name resolves to `plugins.<name>` (bundled) or an importable module path.
The module must define:

    def setup(hub, config: dict) -> None

where `hub` is events.HUB (see events.py for on/route/metric) and `config` is
that plugin's TOML sub-table.
"""
from __future__ import annotations

import importlib

import db
from config import CFG
from events import HUB


def load(app) -> list[str]:
    loaded: list[str] = []
    for name in CFG.plugins_enabled:
        try:
            mod = importlib.import_module(f"plugins.{name}")
        except ModuleNotFoundError:
            mod = importlib.import_module(name)
        cfg = CFG.plugin_config.get(name, {})
        mod.setup(HUB, cfg)
        loaded.append(name)
        db.audit("system", "plugin-loaded", target=name)

    # Register any routes the plugins added.
    for r in HUB.routes:
        app.add_api_route(r["path"], r["endpoint"], methods=r["methods"], name=r["name"])

    return loaded
