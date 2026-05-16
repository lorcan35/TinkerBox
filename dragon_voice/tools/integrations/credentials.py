"""Per-integration credential storage (#341 / #347).

Each integration owns a **directory** of small JSON cred files under
``{base}/{provider}/{account_id}.json`` (mode ``0o600``).  This lets a
single provider hold multiple connected accounts (e.g. work + personal
Gmail).  ``account_id`` is whatever the provider considers a stable
user handle — for Google integrations it's the verified email; for
Spotify it's the user's id; for Home Assistant it's the host string.

Schema is opaque to this layer — the integration backend defines what
gets stored (OAuth tokens + expiry + refresh, or a static long-lived
token, etc.).

Atomic write: writes to ``{name}.json.tmp`` then ``os.replace`` so a
crash mid-write doesn't corrupt the cred file.  Read returns ``None``
when the file is missing OR malformed — caller treats that as "not
connected" and prompts the user to reconnect.

Backward compat: pre-#347 builds stored a single file per provider at
``{base}/{provider}.json``.  ``ProviderCredentialDir.migrate_legacy``
moves that to ``{base}/{provider}/_legacy.json`` (the account_id is
filled in by the integration on first successful health-check via
userinfo lookup) so existing connections survive an upgrade.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default location.  Override via TINKERCLAW_INTEGRATIONS_DIR env var
# (used by tests + on Dragon to redirect away from ~/.tinkerclaw/ which
# is OpenClaw's config dir AND read-only under systemd hardening).
_DEFAULT_DIR = Path.home() / ".tinkerclaw" / "integrations"

# Sentinel used by the legacy-migration shim.  A real account_id is
# filled in once the integration can look it up (e.g. Google userinfo
# returns the verified email).
LEGACY_ACCOUNT_ID = "_legacy"

# Reserved account_id values that aren't real accounts.  Listed so the
# REST list_accounts surface can hide them.
_RESERVED_ACCOUNT_IDS = frozenset({LEGACY_ACCOUNT_ID})

# Filesystem-safe account_id pattern.  Emails fit; arbitrary user
# input is sanitised by `sanitise_account_id` below.
_ALLOWED_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9._@+\-]+$")


def _resolve_dir() -> Path:
    override = os.environ.get("TINKERCLAW_INTEGRATIONS_DIR")
    if override:
        return Path(override).expanduser()
    return _DEFAULT_DIR


def sanitise_account_id(raw: str) -> str:
    """Turn an arbitrary id into a filesystem-safe slug.

    Keeps the email-ish characters (alnum, dot, at, plus, dash,
    underscore) and replaces anything else with ``-``.  Empty after
    sanitisation → raises ValueError because that's a programming
    error, not an integration concern.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._@+\-]+", "-", raw.strip())
    cleaned = cleaned.strip("-._")
    if not cleaned:
        raise ValueError(f"account_id sanitises to empty: {raw!r}")
    return cleaned


class CredentialStore:
    """Per-(provider, account) cred file at ``{base}/{provider}/{account_id}.json``.

    Thread-safe via a per-instance asyncio.Lock — writes are serialised
    but reads run unlocked (file-level atomic).  Cross-process safety
    is not a goal: a single dragon_voice process owns its creds.

    Construction:

        store = CredentialStore("google-calendar", account_id="me@example.com")
        await store.save({"tokens": {...}})
        data = await store.load()
    """

    def __init__(
        self,
        provider: str,
        account_id: str = LEGACY_ACCOUNT_ID,
        base_dir: Optional[Path] = None,
    ) -> None:
        self._provider = provider
        self._account_id = sanitise_account_id(account_id) if account_id != LEGACY_ACCOUNT_ID else account_id
        base = base_dir if base_dir is not None else _resolve_dir()
        self._dir = base / provider
        self._path = self._dir / f"{self._account_id}.json"
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def account_id(self) -> str:
        return self._account_id

    async def load(self) -> Optional[dict[str, Any]]:
        """Read credentials.  Returns None on missing OR malformed file."""
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
        """Atomic write + chmod 0o600."""
        async with self._lock:
            await asyncio.to_thread(self._save_sync, data)

    def _save_sync(self, data: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._dir, 0o700)
        except OSError:
            pass
        # Also tighten the integrations root so a sibling provider
        # can't be world-readable.
        try:
            os.chmod(self._dir.parent, 0o700)
        except OSError:
            pass
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, self._path)

    async def delete(self) -> None:
        async with self._lock:
            try:
                self._path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Failed to delete %s: %s", self._path, e)

    async def exists(self) -> bool:
        return self._path.exists()

    async def rename_to(self, new_account_id: str) -> None:
        """Move this store's file to a different account_id.

        Used by the legacy-migration shim once the real account_id is
        known (e.g. Google userinfo returned the email).  Atomic via
        ``os.replace``.  Silently no-op when the source doesn't exist.
        """
        async with self._lock:
            if not self._path.exists():
                return
            new_account = sanitise_account_id(new_account_id)
            new_path = self._dir / f"{new_account}.json"
            if new_path == self._path:
                return
            os.replace(self._path, new_path)
            self._account_id = new_account
            self._path = new_path


class ProviderCredentialDir:
    """List + manage all accounts under a single provider.

    Each integration backend holds one of these to enumerate the
    accounts it currently owns and to perform per-provider operations
    (legacy migration, bulk-disconnect, etc.).
    """

    def __init__(self, provider: str, base_dir: Optional[Path] = None) -> None:
        self._provider = provider
        base = base_dir if base_dir is not None else _resolve_dir()
        self._dir = base / provider
        self._base = base

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def path(self) -> Path:
        return self._dir

    def list_accounts(self, include_reserved: bool = False) -> list[str]:
        """Return all account_ids that have a cred file on disk.

        Empty list when the provider dir doesn't exist or is empty.
        ``_legacy`` is hidden by default — callers that need to act
        on it (the migration shim) pass include_reserved=True.
        """
        if not self._dir.is_dir():
            return []
        accounts = []
        for entry in self._dir.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            if entry.name.endswith(".tmp"):
                continue
            account = entry.stem
            if not include_reserved and account in _RESERVED_ACCOUNT_IDS:
                continue
            accounts.append(account)
        return sorted(accounts)

    def store_for(self, account_id: str) -> CredentialStore:
        """Build a CredentialStore for one account under this provider."""
        return CredentialStore(self._provider, account_id, base_dir=self._base)

    def migrate_legacy(self) -> Optional[CredentialStore]:
        """Look for a pre-#347 flat ``{base}/{provider}.json`` file and
        move it to ``{base}/{provider}/_legacy.json`` so the integration
        keeps loading after the upgrade.

        Returns the CredentialStore pointing at the migrated file when
        a migration happened, None otherwise.  Idempotent — calling
        twice is safe.
        """
        legacy_flat = self._base / f"{self._provider}.json"
        if not legacy_flat.exists():
            return None
        target = self.store_for(LEGACY_ACCOUNT_ID)
        if target.path.exists():
            # Already migrated; just remove the stale flat file.
            try:
                legacy_flat.unlink()
            except OSError as e:
                logger.warning("Failed to delete stale flat %s: %s", legacy_flat, e)
            return target
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(legacy_flat, target.path)
        logger.info(
            "Migrated legacy single-account creds for %s to %s — "
            "the integration will fold this into a per-account slot "
            "the next time it confirms the account identity.",
            self._provider, target.path,
        )
        return target


__all__ = [
    "LEGACY_ACCOUNT_ID",
    "CredentialStore",
    "ProviderCredentialDir",
    "sanitise_account_id",
]
