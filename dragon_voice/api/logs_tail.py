"""W4-C (cross-stack audit 2026-05-11): journald log-tail REST endpoint.

`GET /api/v1/logs/tail` shells out to `journalctl --output=json` for the
configured systemd unit (default `tinkerclaw-voice`) and returns a
parsed, level-filtered, paginated slice of recent log lines.

Pre-W4-C the dashboard's "Logs" tab queried `events` (the SQLite event
ring — bus messages, tool calls) which is great for product
observability but useless when triaging "voice WS died at 04:12 with no
error event."  The journald output is the only surface for that kind of
incident; ops had to SSH to read it.  Now they don't.

## Wire shape

```
GET /api/v1/logs/tail
  ?unit=tinkerclaw-voice  (default; allowlist below)
  &n=100                   (default; max 1000)
  &level=INFO              (DEBUG | INFO | WARNING | ERROR; default INFO)
  &since=900               (seconds-ago integer; optional)
  &since_cursor=<opaque>   (journald __CURSOR; optional)
  &grep=<substring>         (server-side MESSAGE filter; optional)
```

Response:

```json
{
  "unit": "tinkerclaw-voice",
  "count": 100,
  "head_cursor": "s=...;i=...",
  "tail_cursor": "s=...;i=...",
  "items": [
    {"ts": 1715648400.123, "level": "INFO", "pid": 825, "message": "..."},
    ...
  ]
}
```

## Security notes

* Bearer-auth gated by the existing middleware allowlist (this endpoint
  is NOT in the public-prefix set in `middleware/auth.py`).
* `unit` is restricted to an allowlist so a hostile caller can't tail
  every service on the host (e.g. `sshd`, `dbus`, etc.).
* `journalctl` is invoked with explicit argv (no shell), so even a
  malicious `grep` query can't shell-escape.  Substring filtering is
  done in-process after the JSON parse.
* Subprocess timeout 5 s + stdout cap 8 MB so a buggy query can't
  stall the endpoint or blow memory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aiohttp import web

from dragon_voice.api.utils import json_error

logger = logging.getLogger(__name__)

# Allowlist for the `unit` query param.  Keeps the endpoint scoped to
# Dragon's own services — no `sshd`, no `dbus`, no generic poking.
ALLOWED_UNITS = frozenset({
    "tinkerclaw-voice",
    "tinkerclaw",
    "tinkerclaw-dashboard",
    "tinkerclaw-ngrok",
    "tinkerclaw-gateway",
    "ollama",
})

# Subprocess + buffer caps.  Sized generously — n=1000 JSON lines ~600 KB.
_JOURNALCTL_TIMEOUT_S = 5.0
_JOURNALCTL_MAX_STDOUT_BYTES = 8 * 1024 * 1024  # 8 MB

# Default + max n per request.
_DEFAULT_N = 100
_MAX_N = 1000

# Default level cap.  Maps to the journald --priority value passed in.
_DEFAULT_LEVEL = "INFO"


# Syslog priority → human level.  journald `PRIORITY` field is a string
# digit "0".."7"; map to one of DEBUG/INFO/WARNING/ERROR for the API
# response.
def _priority_to_level(pri: Any) -> str:
   """Map journald PRIORITY (0..7 string or int) → DEBUG/INFO/WARNING/ERROR."""
   try:
      p = int(pri)
   except (TypeError, ValueError):
      return "INFO"
   if p <= 3:
      return "ERROR"   # 0=EMERG, 1=ALERT, 2=CRIT, 3=ERR
   if p == 4:
      return "WARNING"
   if p <= 6:
      return "INFO"    # 5=NOTICE, 6=INFO
   return "DEBUG"      # 7=DEBUG


# Reverse: human level → maximum syslog priority to pass `journalctl
# --priority=`.  Higher priorities (lower numbers) are also included
# by journalctl when you specify a max — so `--priority=4` returns
# 0..4 (everything WARNING-and-above).
def _level_to_max_priority(level: str) -> int:
   """ERROR=3, WARNING=4, INFO=6, DEBUG=7 (default INFO if unrecognised)."""
   norm = (level or "").strip().upper()
   if norm in ("ERROR", "ERR"):
      return 3
   if norm in ("WARNING", "WARN"):
      return 4
   if norm == "DEBUG":
      return 7
   return 6  # INFO + above (default)


def _parse_n(raw: str | None) -> int:
   """Clamp to 1..MAX_N; default if unparseable."""
   if not raw:
      return _DEFAULT_N
   try:
      n = int(raw)
   except ValueError:
      return _DEFAULT_N
   return max(1, min(n, _MAX_N))


def _parse_since(raw: str | None) -> str | None:
   """Convert `?since=N` (integer seconds-ago) → journalctl-friendly
   `-N seconds ago` string.  Returns None if unset or unparseable —
   the caller then omits --since."""
   if not raw:
      return None
   try:
      secs = int(raw)
   except ValueError:
      return None
   if secs <= 0:
      return None
   # journalctl accepts `-NN seconds ago` natively.
   return f"{secs} seconds ago"


def _parse_journal_line(line: bytes) -> dict | None:
   """Parse one journalctl --output=json line.

   Returns the trimmed Dragon-facing dict, or None if the line is
   malformed.  Never raises.
   """
   try:
      obj = json.loads(line.decode("utf-8", errors="replace"))
   except json.JSONDecodeError:
      return None
   ts_us = obj.get("__REALTIME_TIMESTAMP")
   try:
      ts = int(ts_us) / 1_000_000 if ts_us is not None else time.time()
   except (TypeError, ValueError):
      ts = time.time()
   message = obj.get("MESSAGE", "")
   if isinstance(message, list):
      # Some entries (e.g. multiline) come through as a byte-array.
      message = "".join(chr(b) for b in message if isinstance(b, int))
   if not isinstance(message, str):
      message = str(message)
   return {
      "ts": round(ts, 3),
      "level": _priority_to_level(obj.get("PRIORITY")),
      "pid": int(obj["_PID"]) if obj.get("_PID", "").isdigit() else None,
      "cursor": obj.get("__CURSOR", ""),
      "message": message,
   }


async def _run_journalctl(args: list[str]) -> tuple[bytes, str]:
   """Run journalctl with the given args.

   Returns (stdout_bytes, error_str).  Never raises — wraps every
   failure mode into the error string for caller surfacing.
   """
   try:
      proc = await asyncio.create_subprocess_exec(
          "journalctl", *args,
          stdout=asyncio.subprocess.PIPE,
          stderr=asyncio.subprocess.PIPE,
      )
   except FileNotFoundError:
      return b"", "journalctl not on PATH"
   except Exception as e:  # noqa: BLE001
      return b"", f"spawn {type(e).__name__}: {e}"[:200]

   try:
      stdout, stderr = await asyncio.wait_for(
          proc.communicate(), timeout=_JOURNALCTL_TIMEOUT_S,
      )
   except asyncio.TimeoutError:
      try:
         proc.kill()
      except ProcessLookupError:
         pass
      return b"", f"journalctl timeout after {_JOURNALCTL_TIMEOUT_S}s"

   if len(stdout) > _JOURNALCTL_MAX_STDOUT_BYTES:
      stdout = stdout[:_JOURNALCTL_MAX_STDOUT_BYTES]

   if proc.returncode != 0:
      err = (stderr or b"").decode("utf-8", errors="replace")[:200]
      return stdout, f"journalctl exit {proc.returncode}: {err}"

   return stdout, ""


class LogsTailRoutes:
   """`GET /api/v1/logs/tail` — journald reader."""

   def register(self, app: web.Application) -> None:
      app.router.add_get("/api/v1/logs/tail", self.get_logs_tail)

   async def get_logs_tail(self, request: web.Request) -> web.Response:
      unit = request.query.get("unit", "tinkerclaw-voice").strip()
      if unit not in ALLOWED_UNITS:
         return json_error(
             f"unit not in allowlist: {sorted(ALLOWED_UNITS)}",
             status=400,
         )
      n = _parse_n(request.query.get("n"))
      level = request.query.get("level", _DEFAULT_LEVEL).strip()
      max_pri = _level_to_max_priority(level)
      since_str = _parse_since(request.query.get("since"))
      since_cursor = request.query.get("since_cursor", "").strip()
      grep = request.query.get("grep", "").strip()

      args = [
          "--no-pager",
          "--output=json",
          "--unit", unit,
          "-n", str(n),
          f"--priority={max_pri}",
      ]
      if since_str is not None:
         args += ["--since", since_str]
      if since_cursor:
         args += ["--after-cursor", since_cursor]

      stdout, err = await _run_journalctl(args)
      items: list[dict] = []
      for line in stdout.splitlines():
         if not line:
            continue
         entry = _parse_journal_line(line)
         if entry is None:
            continue
         if grep and grep not in entry["message"]:
            continue
         items.append(entry)

      head_cursor = items[-1]["cursor"] if items else ""
      tail_cursor = items[0]["cursor"] if items else ""

      response: dict[str, Any] = {
          "unit": unit,
          "level": level,
          "count": len(items),
          "head_cursor": head_cursor,
          "tail_cursor": tail_cursor,
          "items": items,
      }
      if err:
         response["error"] = err
      return web.json_response(response)
