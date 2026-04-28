"""Debug / diagnostic HTTP handlers.

These endpoints exist for developer diagnostics (memory trace, widget
emit test) and are gated by the usual bearer-token middleware.  They're
extracted from :class:`dragon_voice.server.VoiceServer` so the main
server module stays focused on request routing + lifecycle.

Each handler is a plain async callable that takes ``request`` plus the
deps it needs.  The four ``handle_widget_*`` handlers only need the
surface manager, so they take it explicitly.  ``handle_debug_mem``
reaches into many server attributes (lazily-attached baselines, uptime,
active-connection count) so it takes the server as a single handle.
When a debug endpoint's coupling to server state is narrow enough to
express as a 1–2 arg signature, the handler should do that; when it's
broad, ``server`` is acceptable.
"""
from __future__ import annotations

import gc
import logging
import time
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


# ── W15-C01: tracemalloc-based mem-diff probe ───────────────────────────────

async def handle_debug_mem(request: web.Request, *, server: Any) -> web.Response:
    """GET /debug/mem — top-N allocation-growth snapshot since baseline.

    Baseline is set lazily on first call; subsequent calls diff against
    the stored baseline.  ``?reset=1`` replaces the baseline with the
    current snapshot; ``?n=NN`` caps the result set (default 25);
    ``?trim=1`` forces ``gc.collect()`` + ``malloc_trim(0)`` before the
    snapshot so we can measure reclaimable vs genuinely-leaked bytes.

    Requires ``DRAGON_TRACEMALLOC=1`` in the env — otherwise returns
    503 with a hint.
    """
    import tracemalloc
    import resource

    if not tracemalloc.is_tracing():
        return web.json_response(
            {
                "error": "tracemalloc_disabled",
                "hint": "export DRAGON_TRACEMALLOC=1 and restart voice service",
            },
            status=503,
        )

    snap = tracemalloc.take_snapshot()
    snap = snap.filter_traces(
        (
            tracemalloc.Filter(False, tracemalloc.__file__),
            tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
            tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
        )
    )
    reset = request.query.get("reset", "0") == "1"
    topn = int(request.query.get("n", "25"))

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss_kb / 1024.0
    out: dict = {
        "rss_mb": round(rss_mb, 1),
        "uptime_seconds": round(time.time() - server._start_time, 1),
        "active_connections": len(server._active_connections),
        "tracemalloc_peak_mb": round(tracemalloc.get_traced_memory()[1] / 1024 / 1024, 1),
    }

    if request.query.get("trim", "0") == "1":
        before_rss = rss_mb
        import ctypes
        gc.collect()
        try:
            libc = ctypes.CDLL("libc.so.6")
            libc.malloc_trim(0)
        except (OSError, AttributeError) as e:
            out["malloc_trim_error"] = str(e)
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss_kb / 1024.0
        out["rss_mb_after_trim"] = round(rss_mb, 1)
        out["rss_reclaimed_mb"] = round(before_rss - rss_mb, 1)

    baseline = getattr(server, "_mem_baseline", None)
    if baseline is None or reset:
        server._mem_baseline = snap
        server._mem_baseline_rss_mb = rss_mb
        out["note"] = "baseline set — call again after load to see diff"
    else:
        stats = snap.compare_to(baseline, "lineno")
        out["rss_delta_mb"] = round(rss_mb - server._mem_baseline_rss_mb, 1)
        out["top"] = [
            {
                "file": f"{s.traceback[0].filename.split('/')[-1]}:{s.traceback[0].lineno}",
                "size_mb": round(s.size / 1024 / 1024, 3),
                "size_diff_mb": round(s.size_diff / 1024 / 1024, 3),
                "count": s.count,
                "count_diff": s.count_diff,
            }
            for s in stats[:topn]
        ]

    # gc type-count diff — catches leaks tracemalloc misses (objects
    # held in long-lived caches).
    from collections import Counter
    type_counts = Counter(type(o).__name__ for o in gc.get_objects())
    base_counts = getattr(server, "_mem_type_counts", None)
    if base_counts is None or reset:
        server._mem_type_counts = type_counts
        out["top_types"] = [
            {"type": k, "count": v} for k, v in type_counts.most_common(25)
        ]
    else:
        diff = {k: type_counts[k] - base_counts.get(k, 0) for k in type_counts}
        growers = sorted(diff.items(), key=lambda kv: -kv[1])[:25]
        out["top_type_growers"] = [
            {"type": k, "delta": d, "now": type_counts[k]}
            for k, d in growers if d > 0
        ]
    return web.json_response(out)


# ── Audit widget emitters (B2 / B5 / B6 / K3) ───────────────────────────────
#
# Each takes the SurfaceManager directly so the coupling to
# :class:`VoiceServer` is zero.  If ``surface_mgr`` is None (startup
# hasn't finished wiring it) we return 503 so the dashboard shows a
# clear "not ready" signal rather than a 500.

def _iter_surface_sessions(surface_mgr) -> list[tuple[str, Any]]:
    """Return a snapshot list of ``(session_id, state)`` pairs.

    Small helper so the widget emitters all walk the same path and we
    can stub it in tests without knowing SurfaceManager internals.
    Returns a list (not a live iterator) so concurrent register /
    unregister during emission doesn't mutate what we're iterating.
    """
    return list(surface_mgr._sessions.items())


async def handle_debug_widget_chart(request: web.Request, *, surface_mgr) -> web.Response:
    """POST /debug/widget_chart — audit B5 evidence.

    Emits a chart widget on every registered Tab5Surface.
    Body: ``{title, values, chart_max}``.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    title = data.get("title", "Audit chart")
    values = data.get("values", [3, 7, 12, 9, 15, 18, 22, 16, 11, 8, 14, 20])
    chart_max = float(data.get("chart_max", 0))
    if surface_mgr is None:
        return web.json_response({"error": "surface_mgr not ready"}, status=503)
    count = 0
    for sid, state in _iter_surface_sessions(surface_mgr):
        try:
            await state.surface.chart(
                title=title, values=values, chart_max=chart_max,
                skill_id="audit", card_id="audit_chart_" + sid[:6],
            )
            count += 1
        except Exception as e:
            logger.warning("debug chart emit failed for %s: %s", sid, e)
    return web.json_response({"emitted": count, "values": values})


async def handle_debug_widget_prompt(request: web.Request, *, surface_mgr) -> web.Response:
    """POST /debug/widget_prompt — audit B6/K3 evidence.

    Emits a ``widget_prompt`` on every registered Tab5Surface AND
    registers a handler so tap round-trips back to Dragon.
    Body: ``{title, body, choices: [[text, event], ...]}``.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    title = data.get("title", "Confirm?")
    body = data.get("body", "")
    choices_raw = data.get("choices", [["Yes", "audit_yes"], ["No", "audit_no"]])
    choices = []
    for c in choices_raw[:3]:
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            choices.append((str(c[0]), str(c[1])))
    if surface_mgr is None:
        return web.json_response({"error": "surface_mgr not ready"}, status=503)
    count = 0
    for sid, state in _iter_surface_sessions(surface_mgr):
        try:
            card_id = f"audit_prompt_{sid[:6]}"

            async def _handle(event: str, payload: dict, _sid=sid, _cid=card_id) -> None:
                logger.info(
                    "audit widget_prompt tapped: session=%s event=%s payload=%s",
                    _sid, event, payload,
                )
                try:
                    await state.surface.dismiss(_cid)
                except Exception:
                    pass
                surface_mgr.unregister_action(_sid, _cid)

            surface_mgr.register_action(sid, card_id, _handle)
            await state.surface.prompt(
                title=title, body=body, choices=choices,
                skill_id="audit", card_id=card_id,
            )
            count += 1
        except Exception as e:
            logger.warning("debug prompt emit failed for %s: %s", sid, e)
    return web.json_response({"emitted": count, "choices": choices})


async def handle_debug_widget_card(request: web.Request, *, surface_mgr) -> web.Response:
    """POST /debug/widget_card — audit B2 evidence.

    Emits a ``widget_card`` on every registered Tab5Surface.  Cards go
    to chat (not home).  Body: ``{title, body, tone}``.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    title = data.get("title", "Workshop draft ready")
    body = data.get("body", "Two edits queued from yesterday. Review before 10:30.")
    tone = data.get("tone", "info")
    # Phase 2 (#70): optional action button.  Pass {"action_label":"View",
    # "action_event":"audit_open"} to render an amber pill at bottom-right
    # that fires widget_action(card_id, action_event) on tap.  Backwards-
    # compat: omitting both leaves the legacy plain-card render unchanged.
    action_label = data.get("action_label")
    action_event = data.get("action_event")
    action = (action_label, action_event) if action_label and action_event else None
    if surface_mgr is None:
        return web.json_response({"error": "surface_mgr not ready"}, status=503)
    count = 0
    for sid, state in _iter_surface_sessions(surface_mgr):
        try:
            cid = "audit_card_" + sid[:6]
            await state.surface.card(
                title=title, body=body, tone=tone,
                skill_id="audit", card_id=cid, action=action,
            )
            # Register a default-dismiss handler for the action so the
            # tap round-trip lands in the surface manager's logger
            # without needing a real skill behind it.
            if action:
                async def _action_handler(event, payload, _sid=sid, _cid=cid):
                    logger.info(
                        "audit widget_card action: session=%s card=%s event=%s",
                        _sid, _cid, event,
                    )
                surface_mgr.register_action(sid, cid, _action_handler)
            count += 1
        except Exception as e:
            logger.warning("debug card emit failed for %s: %s", sid, e)
    return web.json_response({"emitted": count, "with_action": bool(action)})


async def handle_debug_widget_media(request: web.Request, *, surface_mgr) -> web.Response:
    """POST /debug/widget_media — audit B5 evidence.

    Emits a ``widget_media`` pointing at a previously-uploaded image.
    Body: ``{url, width, height, alt}``.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    url = data.get("url", "")
    # width + height are carried through to the payload default in the
    # surface emitter; keep the parse so malformed input fails loud.
    int(data.get("width", 480))
    int(data.get("height", 300))
    alt = data.get("alt", "Audit media")
    if not url:
        return web.json_response({"error": "url required"}, status=400)
    if surface_mgr is None:
        return web.json_response({"error": "surface_mgr not ready"}, status=503)
    count = 0
    for sid, state in _iter_surface_sessions(surface_mgr):
        try:
            await state.surface.media(
                url=url, alt=alt, title="Audit media",
                skill_id="audit", card_id="audit_media_" + sid[:6],
            )
            count += 1
        except Exception as e:
            logger.warning("debug media emit failed for %s: %s", sid, e)
    return web.json_response({"emitted": count, "url": url})
