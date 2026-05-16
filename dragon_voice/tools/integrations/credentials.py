"""Per-integration credential storage (#341).

Each integration owns a small JSON file under
``~/.tinkerclaw/integrations/{name}.json`` (mode ``0o600``).  Schema is
opaque to this layer — the integration backend defines what gets
stored (OAuth tokens + expiry + refresh, or a static long-lived
token, etc.).

Atomic write: writes to ``{name}.json.tmp`` then ``os.replace`` so a
crash mid-write doesn't corrupt the cred file.  Read returns ``None``
when the file is missing OR malformed — caller treats that as "not
connected" and prompts the user to reconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default location.  Override via TINKERCLAW_INTEGRATIONS_DIR env var
# (used by tests to redirect to a temp dir).
_DEFAULT_DIR = Path.home() / ".tinkerclaw" / "integrations"


def _resolve_dir() -> Path:
    override = os.environ.get("TINKERCLAW_INTEGRATIONS_DIR")
    if override:
        return Path(override).expanduser()
    return _DEFAULT_DIR


class CredentialStore:
    """Per-integration cred file at ``{base}/{name}.json``.

    Thread-safe via a per-instance asyncio.Lock — writes are serialized
    but reads run unlocked (file-level atomic).  Cross-process safety
    is not a goal: a single dragon_voice process owns its creds.
    """

    def __init__(self, name: str, base_dir: Optional[Path] = None) -> None:
        self._name = name
        self._dir = base_dir if base_dir is not None else _resolve_dir()
        self._path = self._dir / f"{name}.json"
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> Optional[dict[str, Any]]:
        """Read credentials.  Returns None on missing OR malformed file.

        Malformed-file path also logs a warning so an operator sees the
        corruption rather than silently treating it as "disconnected".
        """
        if not self._path.exists():
            return None
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.warning(
                    "Credential file %s is not a JSON object — ignoring",
                    self._path,
                )
                return None
            return data
        except json.JSONDecodeError:
            logger.warning(
                "Credential file %s is corrupt JSON — ignoring (operator "
                "should delete the file + reconnect)",
                self._path,
            )
            return None
        except OSError as e:
            logger.warning("Failed to read %s: %s", self._path, e)
            return None

    async def save(self, data: dict[str, Any]) -> None:
        """Atomic write + chmod 0o600.

        Creates the parent dir if missing.  Raises OSError on
        underlying filesystem failure — caller decides whether that's
        fatal (probably yes; if we can't persist creds the user has to
        reconnect every restart).
        """
        async with self._lock:
            await asyncio.to_thread(self._save_sync, data)

    def _save_sync(self, data: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Make sure the directory itself isn't world-readable.
        try:
            os.chmod(self._dir, 0o700)
        except OSError:
            pass
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        # Mode 0o600 — owner-only read+write.
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, self._path)

    async def delete(self) -> None:
        """Remove the credential file.  No-op if already missing."""
        async with self._lock:
            try:
                self._path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Failed to delete %s: %s", self._path, e)

    async def exists(self) -> bool:
        return self._path.exists()
