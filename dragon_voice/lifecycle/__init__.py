"""Server lifecycle helpers — boot, shutdown, and long-running monitors.

Each submodule has one responsibility:

* :mod:`monitors`  — ``get_rss_mb`` / ``get_cpu_temp`` helpers and the
  ``memory_monitor_loop`` periodic task.
* :mod:`purge`     — ``periodic_purge_loop`` (messages + events) and
  ``media_cleanup_loop``.
* :mod:`startup`   — ``run_startup(server, app)``: wires DB, session
  manager, memory + tools, conversation engine, REST routes, notes
  routes, MCP bridge, and starts the periodic tasks.
* :mod:`shutdown`  — ``run_shutdown(server, app)``: cancels periodic
  tasks, drains active pipelines, releases the backend pool, closes
  shared HTTP sessions + foundation modules.

Startup and shutdown take the ``VoiceServer`` instance as a single
handle.  Their coupling to server state is wide (~20 attributes) —
a 20-arg signature would be worse, not better.  Tests for individual
startup steps should exercise the real module each step delegates to
(the DB, the session manager, …), not this orchestration layer.
"""

from dragon_voice.lifecycle import monitors, purge, shutdown, startup

__all__ = ["monitors", "purge", "shutdown", "startup"]
