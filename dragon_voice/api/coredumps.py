"""W4-D: Coredump archive REST routes.

Surfaces the on-disk coredumps pulled from Tab5 devices by the
background scraper (`dragon_voice/coredump_scraper.py`).  Read-only
for v1 — the scraper is the only writer.

## Endpoints

    GET /api/v1/coredumps                  → list all archived dumps
    GET /api/v1/coredumps?device_id=foo    → filter by device

Response shape (newest-first):

```json
{
  "save_dir": "/home/radxa/tab5-coredumps",
  "count": 2,
  "items": [
    {
      "device_id": "tab5-aabbcc",
      "filename":  "dump-1716000000.bin",
      "path":      "/home/radxa/tab5-coredumps/tab5-aabbcc/dump-1716000000.bin",
      "ts":        1716000000,
      "bytes":     37540,
      "sha256":    "5819c31f...",
      "symbolicated": "/home/radxa/.../dump-1716000000.txt" | null
    }
  ],
  "last_results": {
    "tab5-aabbcc": {
      "ok": true,
      "coredump_present": true,
      "saved_path": "...",
      "deduped": false,
      "sha256": "...",
      "error": ""
    }
  }
}
```

`last_results` is the most-recent `ScrapeResult` per target so
operators can tell at a glance whether the loop is running.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from aiohttp import web

from dragon_voice import coredump_scraper

logger = logging.getLogger(__name__)


class CoredumpRoutes:
    def __init__(self, server: Any) -> None:
        self._server = server

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/v1/coredumps", self.get_coredumps)

    async def get_coredumps(self, request: web.Request) -> web.Response:
        cfg = self._server._config.coredump_scraper
        items = coredump_scraper.list_archived(cfg.save_dir)
        device_filter = request.query.get("device_id", "").strip()
        if device_filter:
            items = [it for it in items if it["device_id"] == device_filter]
        last_results = {
            did: asdict(res)
            for did, res in coredump_scraper.LAST_RESULTS.items()
        }
        return web.json_response({
            "save_dir": cfg.save_dir,
            "count": len(items),
            "items": items,
            "last_results": last_results,
            "enabled": cfg.enabled,
        })
