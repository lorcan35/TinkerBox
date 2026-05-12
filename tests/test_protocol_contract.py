"""W9-C contract test: protocol.md ↔ _handle_ws_voice dispatcher parity.

Wave 9-C of the cross-stack cohesion audit (2026-05-11).  The audit found
the protocol surface drifts silently — `_handle_ws_voice` adds a new
``cmd_type ==`` branch (user_media, widget_action, etc.) and protocol.md
isn't updated, or vice versa.  Tab5 firmware authors lose hours chasing
"is this a real verb?" against grep output.

This test enforces a bidirectional contract:

  1. Every Tab5→Dragon verb the dispatcher handles MUST have a
     corresponding ``### N.M verb (Tab5 -> Dragon)`` heading in
     ``docs/protocol.md``.
  2. Every ``### N.M verb (Tab5 -> Dragon)`` heading in protocol.md
     MUST correspond to a dispatcher branch.

A small allowlist captures intentional asymmetries (deprecated verbs
still in code; protocol-only docs like binary PCM frames that have no
``cmd_type ==`` branch).  The allowlist is the visible source of truth
for "we know this is asymmetric and we accept it".

Run:
    python3 -m pytest tests/test_protocol_contract.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = REPO_ROOT / "dragon_voice" / "server.py"
PROTOCOL_MD = REPO_ROOT / "docs" / "protocol.md"

# Verbs that exist in the dispatcher but legitimately have no protocol.md
# heading.  Anything added here MUST justify itself with a code comment.
DISPATCHER_ONLY_ALLOWLIST = {
    # Deprecated; the inline log message points users to dictation mode.
    "record_start",
    "record_stop",
    # config_ack is a Tab5-side ACK to Dragon's config_update reply (the
    # outer protocol section covers config_update; the ack is a debug
    # signal only).
    "config_ack",
}

# Headings in protocol.md that legitimately have no dispatcher branch.
# Binary frames (PCM, AUD0, VID0) and Dragon→Tab5 messages don't show up
# in `_handle_ws_voice`'s cmd_type dispatch.
PROTOCOL_ONLY_ALLOWLIST_PATTERNS = [
    # Binary frames don't have a cmd_type
    re.compile(r"^Binary PCM Audio", re.I),
    re.compile(r"^Binary TTS Audio", re.I),
    re.compile(r"^Binary frame magic", re.I),
    re.compile(r"^Client-Side VAD", re.I),  # Tab5-internal
    # Subsection headings that aren't verbs
    re.compile(r"^(WebSocket Endpoint|Handshake|Reconnection|Sequence|"
               r"Session Lifecycle|Reverse|Glossary|Overview|Architecture)", re.I),
]


def _extract_dispatcher_verbs() -> set[str]:
    """Return the set of Tab5→Dragon cmd_types the dispatcher branches on."""
    src = SERVER_PY.read_text(encoding="utf-8")
    # Match `cmd_type == "verb"` and `cmd_type == "v1" or cmd_type == "v2"`
    verbs = set(re.findall(r'cmd_type\s*==\s*"([a-z_]+)"', src))
    return verbs


def _extract_protocol_tab5_to_dragon_verbs() -> set[str]:
    """Return verbs from `### N.M ... (Tab5 -> Dragon)` headings.

    Tolerant of:
      * ASCII `->` and Unicode `→` arrows
      * `### N.M verb (Tab5 -> Dragon)`
      * `### N.M Action — \\`verb\\` (Tab5 -> Dragon)` (widget-action style)
      * `### N.M verb with dictate mode (Tab5 -> Dragon)` (qualifier suffix)

    Headings whose body matches `PROTOCOL_ONLY_ALLOWLIST_PATTERNS` (e.g.,
    "Binary PCM Audio") are skipped — those aren't verbs by design.
    """
    text = PROTOCOL_MD.read_text(encoding="utf-8")
    arrow = r"(?:->|→)"
    pattern = re.compile(
        rf"^###\s+\d+\.\d+\s+(?P<body>.*?)\(Tab5\s*{arrow}\s*Dragon\)",
        re.MULTILINE,
    )
    # A verb must be a backtick-wrapped identifier OR the FIRST identifier
    # in the body.  Backtick-wrapped wins to avoid catching "mode" from
    # "start with dictate mode".
    backtick_verb = re.compile(r"`([a-z_][a-z0-9_]*)`")
    bare_first_verb = re.compile(r"^([a-z_][a-z0-9_]*)")
    verbs: set[str] = set()
    for match in pattern.finditer(text):
        body = match.group("body").strip().rstrip("-—–").strip()
        if _allowed_protocol_only(body):
            continue
        m_bt = backtick_verb.search(body)
        if m_bt:
            verbs.add(m_bt.group(1))
            continue
        m_first = bare_first_verb.match(body)
        if m_first:
            verbs.add(m_first.group(1))
    return verbs


def _allowed_protocol_only(heading_body: str) -> bool:
    return any(p.match(heading_body) for p in PROTOCOL_ONLY_ALLOWLIST_PATTERNS)


def test_dispatcher_verbs_documented_in_protocol():
    """Every cmd_type branch in `_handle_ws_voice` is documented."""
    dispatcher = _extract_dispatcher_verbs()
    documented = _extract_protocol_tab5_to_dragon_verbs()
    assert dispatcher, "regex failed to find any cmd_type branches in server.py"
    assert documented, "regex failed to find any Tab5->Dragon headings in protocol.md"

    undocumented = (dispatcher - documented) - DISPATCHER_ONLY_ALLOWLIST
    assert not undocumented, (
        f"Dispatcher handles {sorted(undocumented)} but protocol.md has no "
        f"`### N.M <verb> (Tab5 -> Dragon)` section.  Either add the docs "
        f"or update DISPATCHER_ONLY_ALLOWLIST in tests/test_protocol_contract.py."
    )


def test_protocol_verbs_handled_by_dispatcher():
    """Every `Tab5 -> Dragon` verb in protocol.md has a dispatcher branch."""
    dispatcher = _extract_dispatcher_verbs()
    documented = _extract_protocol_tab5_to_dragon_verbs()
    orphaned = documented - dispatcher
    # No allowlist needed here — every documented verb should dispatch.
    # Deprecated verbs should be removed from protocol.md, not allowlisted.
    assert not orphaned, (
        f"protocol.md documents {sorted(orphaned)} but `_handle_ws_voice` "
        f"has no `cmd_type == '<verb>'` branch.  Either implement the verb "
        f"or remove the section from protocol.md."
    )


def test_dispatcher_allowlist_is_real():
    """Every entry in DISPATCHER_ONLY_ALLOWLIST must actually appear in
    the dispatcher.  Stale allowlist entries hide real drift."""
    dispatcher = _extract_dispatcher_verbs()
    stale = DISPATCHER_ONLY_ALLOWLIST - dispatcher
    assert not stale, (
        f"DISPATCHER_ONLY_ALLOWLIST has {sorted(stale)} that are no longer "
        f"in the dispatcher.  Remove them so the contract test stays honest."
    )


def test_known_core_verbs_present():
    """Smoke check: the core verbs every Tab5 release uses must be
    present on BOTH sides.  Catches the case where a refactor accidentally
    deletes a branch without removing its docs (the contract would still
    pass via the allowlist mechanism if both sides went dark)."""
    dispatcher = _extract_dispatcher_verbs()
    documented = _extract_protocol_tab5_to_dragon_verbs()
    for verb in ("register", "start", "stop", "cancel", "clear", "text",
                 "ping", "config_update", "segment"):
        assert verb in dispatcher, f"core verb {verb!r} missing from dispatcher"
        assert verb in documented, f"core verb {verb!r} missing from protocol.md"
