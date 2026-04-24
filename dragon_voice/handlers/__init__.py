"""HTTP request handlers extracted from :mod:`dragon_voice.server`.

Each submodule owns a small, cohesive set of endpoints with one
stakeholder (e.g. ``debug`` for dev/diagnostics, ``status`` for ops /
product, ``config_api`` for product).  Handlers here are plain async
callables — the URL routing + bearer-auth middleware wiring stays in
``server.create_app``.
"""

from dragon_voice.handlers import config_api, debug, status

__all__ = ["config_api", "debug", "status"]
