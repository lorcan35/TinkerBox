"""W4-D (cross-stack audit 2026-05-11): Dragon-side coredump scraper.

Periodically polls each configured Tab5 for a coredump on flash and
archives it under `save_dir/{device_id}/dump-{epoch}.bin` so operators
have a durable record without manually curling Tab5 + running
`espcoredump.py`.

Pre-W4-D, coredumps lived only on Tab5 flash.  When Tab5 had an
unexplained `SW` reset, the stale dump on flash was from a *prior*
firmware build (SHA mismatch) — invisible to anyone not actively
SSH-ing to Tab5 looking for it.

## Loop semantics

* `enabled=False` → loop never spawns (default).
* `enabled=True` with empty `targets` → loop spawns, logs once, idles.
* Each tick (`poll_interval_s`): probe each target's `/info`; if
  `coredump_present` is true, GET `/coredump` with the target's
  bearer + save the raw bytes.
* **Dedupe by SHA-256:** if a dump with the same content hash already
  exists on disk, skip — Tab5 holds the same blob across reboots until
  it's cleared, so we don't want a new file every minute.
* Best-effort symbolicate: if `firmware_elf` is set + readable, run
  `espcoredump.py info_corefile --core <bin> --core-format raw <elf>`
  in a subprocess.  Output (incl. SHA mismatch) lands in a sibling
  `.txt` file.  Symbolication failure is logged + ignored — the raw
  binary is always preserved.
* Each successful pull writes a `tab5.coredump_pulled` event row so
  the dashboard surfaces the pull alongside other system events.

## What this is NOT

* No automatic deletion from Tab5 flash — that's a separate ops
  decision (Tab5's `coredump` partition is FIFO-ish; new dumps
  overwrite old ones eventually).
* No alerting / paging — just durable archival.  W4-D follow-up could
  fire a notification when a *new* (different SHA) dump arrives.
* No retroactive replay of old dumps from disk — the scraper is
  forward-looking from the moment it starts.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class ScrapeResult:
    """One target-tick outcome.  Used by tests + dashboard."""

    target_device_id: str
    ok: bool                          # reached + parsed /info
    coredump_present: bool             # what /info said
    saved_path: Optional[str] = None
    saved_bytes: int = 0
    sha256: str = ""
    deduped: bool = False              # True if SHA matched an existing file
    symbolicated_path: Optional[str] = None
    error: str = ""


# Public so the lifecycle wiring + tests can introspect.
LAST_RESULTS: dict[str, ScrapeResult] = {}


async def probe_info(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    timeout_s: float,
    token: str = "",
) -> tuple[Optional[dict], str]:
    """GET `/crashlog` on a Tab5 — surfaces `coredump_present` + reset
    reason in one cheap JSON.  Bearer-auth gated.

    NOTE: there's also a public `/info` endpoint, but it carries
    heap/uptime/wifi state — NOT `coredump_present`.  Live-verified
    on Tab5 192.168.1.90 (2026-05-13).  Using `/crashlog` (bearer)
    is the only correct shape.

    Returns (parsed_json_or_None, error_str).  Never raises.
    """
    url = f"http://{host}:{port}/crashlog"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status != 200:
                return None, f"crashlog HTTP {resp.status}"
            return await resp.json(), ""
    except asyncio.TimeoutError:
        return None, f"crashlog timeout after {timeout_s}s"
    except aiohttp.ClientError as e:
        return None, f"crashlog {type(e).__name__}: {e}"[:200]
    except Exception as e:  # noqa: BLE001
        return None, f"crashlog unexpected {type(e).__name__}: {e}"[:200]


async def pull_coredump(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    token: str,
    timeout_s: float,
) -> tuple[Optional[bytes], str]:
    """GET `/coredump` with bearer auth.  Returns (bytes_or_None, error)."""
    url = f"http://{host}:{port}/coredump"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status != 200:
                return None, f"coredump HTTP {resp.status}"
            return await resp.read(), ""
    except asyncio.TimeoutError:
        return None, f"coredump timeout after {timeout_s}s"
    except aiohttp.ClientError as e:
        return None, f"coredump {type(e).__name__}: {e}"[:200]
    except Exception as e:  # noqa: BLE001
        return None, f"coredump unexpected {type(e).__name__}: {e}"[:200]


def _dump_already_archived(device_dir: Path, sha256: str) -> Optional[str]:
    """Walk `device_dir` looking for a saved dump with the same SHA.

    Returns the existing file path if found; else None.  Dedupe key is
    a `*.sha256` sidecar so we don't have to re-hash every file each
    tick.
    """
    if not device_dir.exists():
        return None
    for sha_file in device_dir.glob("*.sha256"):
        try:
            if sha_file.read_text().strip() == sha256:
                bin_path = sha_file.with_suffix(".bin")
                if bin_path.exists():
                    return str(bin_path)
        except OSError:
            continue
    return None


async def symbolicate(
    bin_path: Path, elf_path: Path, timeout_s: float = 30.0,
) -> tuple[Optional[str], str]:
    """Best-effort `espcoredump.py info_corefile` invocation.

    Returns (output_path, error_str).  Subprocess timeout-bounded to
    avoid blocking the scraper loop on a stuck symbolicator.
    """
    if not elf_path.exists():
        return None, f"elf not found: {elf_path}"
    out_path = bin_path.with_suffix(".txt")
    cmd = [
        "espcoredump.py", "info_corefile",
        "--core", str(bin_path),
        "--core-format", "raw",
        str(elf_path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return None, f"symbolicate timeout after {timeout_s}s"
    except FileNotFoundError:
        return None, "espcoredump.py not on PATH"
    except Exception as e:  # noqa: BLE001
        return None, f"symbolicate {type(e).__name__}: {e}"[:200]
    # Always write output — even SHA mismatch returns useful diagnostic.
    out_path.write_bytes(stdout or b"")
    return str(out_path), ""


async def scrape_target(
    session: aiohttp.ClientSession,
    target: Any,
    save_dir: str,
    firmware_elf: str,
    request_timeout_s: float,
    db: Any = None,
) -> ScrapeResult:
    """One probe + pull cycle.  Never raises — wraps everything,
    including filesystem errors like a sandbox-blocked save_dir."""
    result = ScrapeResult(target_device_id=target.device_id,
                          ok=False, coredump_present=False)
    try:
        return await _scrape_target_inner(
            session, target, save_dir, firmware_elf,
            request_timeout_s, db, result,
        )
    except Exception as e:  # noqa: BLE001 — keep loop alive
        # Filesystem (EROFS on sandboxed save_dir), DB log_event raise,
        # or any other unexpected raise lands here.  Preserve whatever
        # the inner step already wrote into `result` and surface the
        # exception text for the listing endpoint.
        result.error = f"{type(e).__name__}: {e}"[:200]
        return result


async def _scrape_target_inner(
    session: aiohttp.ClientSession,
    target: Any,
    save_dir: str,
    firmware_elf: str,
    request_timeout_s: float,
    db: Any,
    result: ScrapeResult,
) -> ScrapeResult:
    """W4-D: actual scrape body — outer `scrape_target` wraps in a
    try/except so even hard raises still land in LAST_RESULTS."""
    if not target.host or not target.device_id:
        result.error = "incomplete target (missing host or device_id)"
        return result

    info, err = await probe_info(session, target.host, target.port,
                                  request_timeout_s, token=target.token)
    if info is None:
        result.error = err
        return result
    result.ok = True
    cd_present = bool(info.get("coredump_present"))
    result.coredump_present = cd_present
    if not cd_present:
        return result

    # Pull body.  If token is empty, /coredump will 401 — surface that.
    body, err = await pull_coredump(session, target.host, target.port,
                                     target.token, request_timeout_s)
    if body is None or not body:
        result.error = err or "empty coredump body"
        return result

    # Dedupe by SHA-256 against on-disk sidecars.
    sha = hashlib.sha256(body).hexdigest()
    result.sha256 = sha
    result.saved_bytes = len(body)
    device_dir = Path(save_dir) / target.device_id
    existing = _dump_already_archived(device_dir, sha)
    if existing is not None:
        result.saved_path = existing
        result.deduped = True
        return result

    # New dump — write atomically.
    device_dir.mkdir(parents=True, exist_ok=True)
    epoch = int(time.time())
    bin_path = device_dir / f"dump-{epoch}.bin"
    tmp_path = bin_path.with_suffix(".bin.tmp")
    tmp_path.write_bytes(body)
    os.replace(tmp_path, bin_path)
    (device_dir / f"dump-{epoch}.sha256").write_text(sha)
    result.saved_path = str(bin_path)

    # Best-effort symbolicate.
    if firmware_elf:
        out_path, sym_err = await symbolicate(bin_path, Path(firmware_elf))
        if out_path is not None:
            result.symbolicated_path = out_path
        elif sym_err:
            # Don't let symbolicate failure mark the whole result as
            # not-ok — the binary is saved.  Log + carry on.
            logger.info("W4-D symbolicate skipped for %s: %s",
                        target.device_id, sym_err)

    # Record a dashboard event so /events surfaces the pull.
    if db is not None:
        with contextlib.suppress(Exception):
            await db.log_event(
                event_type="tab5.coredump_pulled",
                session_id=None,
                device_id=target.device_id,
                payload={
                    "path": result.saved_path,
                    "bytes": result.saved_bytes,
                    "sha256": sha,
                    "symbolicated_path": result.symbolicated_path,
                },
            )
    return result


async def scraper_loop(server: Any) -> None:
    """Periodic scrape over `server._config.coredump_scraper.targets`.

    Sleeps `poll_interval_s` between full sweeps.  All target probes
    in a sweep run concurrently via `asyncio.gather(return_exceptions
    =True)` so one slow Tab5 doesn't stall the others.  Per-target
    failures are logged + recorded in `LAST_RESULTS` so the listing
    endpoint can surface them.
    """
    cfg = server._config.coredump_scraper
    if not cfg.enabled:
        logger.info("W4-D scraper not enabled — loop will not start")
        return
    if not cfg.targets:
        logger.info(
            "W4-D scraper enabled but no targets configured — loop idle")
    logger.info(
        "W4-D scraper starting: %d target(s), interval=%.0fs, save_dir=%s",
        len(cfg.targets), cfg.poll_interval_s, cfg.save_dir,
    )

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if cfg.targets:
                    results = await asyncio.gather(
                        *[
                            scrape_target(
                                session, t, cfg.save_dir,
                                cfg.firmware_elf, cfg.request_timeout_s,
                                db=getattr(server, "_db", None),
                            ) for t in cfg.targets
                        ],
                        return_exceptions=True,
                    )
                    for t, r in zip(cfg.targets, results):
                        if isinstance(r, ScrapeResult):
                            LAST_RESULTS[t.device_id] = r
                            if r.saved_path and not r.deduped:
                                logger.info(
                                    "W4-D pulled coredump from %s: %s "
                                    "(%d bytes, sha=%s)",
                                    t.device_id, r.saved_path,
                                    r.saved_bytes, r.sha256[:12],
                                )
                            elif r.error:
                                logger.debug(
                                    "W4-D %s probe: %s",
                                    t.device_id, r.error,
                                )
                        else:
                            # scrape_target() promises never to raise,
                            # but defensively record the surprise anyway
                            # so /api/v1/coredumps reflects truth.
                            LAST_RESULTS[t.device_id] = ScrapeResult(
                                target_device_id=t.device_id,
                                ok=False, coredump_present=False,
                                error=f"unexpected raise: "
                                      f"{type(r).__name__}: {r}"[:200],
                            )
                            logger.warning(
                                "W4-D %s probe raised: %s",
                                t.device_id, r,
                            )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("W4-D scraper sweep failed — continuing")
            await asyncio.sleep(cfg.poll_interval_s)


def list_archived(save_dir: str) -> list[dict]:
    """List all dumps on disk for the `/api/v1/coredumps` endpoint.

    Walks `save_dir/*/dump-*.bin`, sorts newest-first.  Returns
    plain dicts — JSON-ready.
    """
    root = Path(save_dir)
    if not root.exists():
        return []
    entries: list[dict] = []
    for device_dir in root.iterdir():
        if not device_dir.is_dir():
            continue
        for bin_path in device_dir.glob("dump-*.bin"):
            try:
                stat = bin_path.stat()
            except OSError:
                continue
            sha_path = bin_path.with_suffix(".sha256")
            txt_path = bin_path.with_suffix(".txt")
            entries.append({
                "device_id": device_dir.name,
                "filename": bin_path.name,
                "path": str(bin_path),
                "ts": int(stat.st_mtime),
                "bytes": stat.st_size,
                "sha256": (sha_path.read_text().strip()
                            if sha_path.exists() else ""),
                "symbolicated": str(txt_path) if txt_path.exists() else None,
            })
    entries.sort(key=lambda e: e["ts"], reverse=True)
    return entries
