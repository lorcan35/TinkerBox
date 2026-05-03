"""Tests for ``dragon_voice.conn_state.ConnState`` (audit follow-up).

Pins three contracts so the dict-compat shim doesn't drift:

  1. **Typed surface** — every field declared in the dataclass is
     readable as both ``state.field`` AND ``state["field"]``.
  2. **Backward compat** — the dict-protocol subset the existing
     server.py uses (``get``, ``setdefault``, ``__getitem__``,
     ``__setitem__``, ``__contains__``) behaves identically to a
     plain dict.
  3. **Typo guard** — assigning to a non-existent field via dict-
     style ``state["wrong"] = ...`` raises KeyError instead of
     silently extending the state (the bug the dict era enabled).
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock

from dragon_voice.conn_state import ConnState


def _minimal_state() -> ConnState:
    """Build a ConnState with only the four required fields filled."""
    return ConnState(
        ws_id="ws-test",
        ws=MagicMock(),
        conn_lock=asyncio.Lock(),
        config=MagicMock(),
    )


class ConnStateTypedAccessTests(unittest.TestCase):
    """Field access via ``.field`` — the typed win."""

    def test_required_fields_set_via_constructor(self):
        s = _minimal_state()
        self.assertEqual(s.ws_id, "ws-test")
        self.assertIsNotNone(s.ws)
        self.assertIsInstance(s.conn_lock, asyncio.Lock)
        self.assertIsNotNone(s.config)

    def test_optional_fields_default_to_none(self):
        s = _minimal_state()
        self.assertIsNone(s.pipeline)
        self.assertIsNone(s.session_id)
        self.assertIsNone(s.device_id)
        self.assertIsNone(s.conversation)

    def test_default_factory_fields_isolated_per_instance(self):
        """``field(default_factory=...)`` must not share state across
        instances — the classic mutable-default bug."""
        s1 = _minimal_state()
        s2 = _minimal_state()
        s1.bg_tasks.add("task-1")
        s1.tool_calls_this_turn.append("call-1")
        s1.handler_tasks["k"] = "v"
        s1.widget_capabilities["w"] = "x"
        # s2 must NOT see s1's mutations.
        self.assertEqual(s2.bg_tasks, set())
        self.assertEqual(s2.tool_calls_this_turn, [])
        self.assertEqual(s2.handler_tasks, {})
        self.assertEqual(s2.widget_capabilities, {})

    def test_scalar_defaults(self):
        s = _minimal_state()
        self.assertFalse(s.registered)
        self.assertEqual(s.mode, "ask")
        self.assertEqual(s.response_mode, "always_speak")
        self.assertEqual(s.voice_mode, 0)
        self.assertEqual(s._last_config_update_ts, 0.0)


class ConnStateDictCompatTests(unittest.TestCase):
    """The dict-protocol shim is the migration carrier — every
    existing ``conn_state.get("key")`` / ``conn_state["key"]`` /
    ``conn_state["key"] = ...`` site must keep working unchanged."""

    def test_getitem_routes_to_field(self):
        s = _minimal_state()
        s.session_id = "sess-123"
        self.assertEqual(s["session_id"], "sess-123")

    def test_setitem_routes_to_field(self):
        s = _minimal_state()
        s["session_id"] = "sess-via-dict"
        self.assertEqual(s.session_id, "sess-via-dict")

    def test_get_returns_field_value_even_when_none(self):
        """Matches dict.get semantics: a key that EXISTS with value
        ``None`` returns ``None``, not the default.  This pins
        backward-compat with the pre-extract dict literal which
        initialised ``session_id: None`` — readers that passed a
        default like ``conn_state.get("session_id", "")`` always
        got ``None`` post-init, never the ``""``."""
        s = _minimal_state()
        self.assertIsNone(s.get("session_id"))
        # Default IGNORED because the field exists (as None).
        self.assertIsNone(s.get("session_id", "fallback"))

    def test_get_with_default_when_field_set(self):
        s = _minimal_state()
        s.session_id = "real"
        # When the field has a value, default is ignored — matches
        # dict.get semantics.
        self.assertEqual(s.get("session_id", "fallback"), "real")

    def test_get_unknown_field_returns_default(self):
        """Unlike a declared field, an unknown attribute really IS
        missing — so the default kicks in.  Keeps typo-tolerant
        callers that pass a sentinel like ``state.get("typo", 0)``
        from crashing."""
        s = _minimal_state()
        self.assertIsNone(s.get("nonexistent_field"))
        self.assertEqual(s.get("nonexistent_field", "x"), "x")

    def test_contains_field_name(self):
        s = _minimal_state()
        self.assertIn("ws_id", s)
        self.assertIn("session_id", s)
        # Non-string keys + unknown names are not contained.
        self.assertNotIn("nonexistent_field", s)
        self.assertNotIn(42, s)

    def test_setdefault_returns_existing_value(self):
        s = _minimal_state()
        s.session_id = "already_set"
        out = s.setdefault("session_id", "new_default")
        self.assertEqual(out, "already_set")
        self.assertEqual(s.session_id, "already_set")

    def test_setdefault_sets_when_field_is_none(self):
        s = _minimal_state()
        out = s.setdefault("session_id", "fresh")
        self.assertEqual(out, "fresh")
        self.assertEqual(s.session_id, "fresh")

    def test_setdefault_for_factory_fields_returns_existing_container(self):
        """Per-turn lists/dicts populated via ``setdefault`` should
        return the SAME container instance so callers can mutate it.
        Mirrors the existing
        ``conn_state.setdefault("tool_calls_this_turn", [])``
        pattern at server.py:1212+."""
        s = _minimal_state()
        # default_factory created a fresh empty list at construction
        same = s.setdefault("tool_calls_this_turn", [])
        # Should return the SAME list (not a copy of the default arg)
        self.assertIs(same, s.tool_calls_this_turn)
        same.append("tool_X")
        self.assertEqual(s.tool_calls_this_turn, ["tool_X"])


class ConnStateTypoGuardTests(unittest.TestCase):
    """Pre-fix ``conn_state["pippeline"] = pipeline`` silently
    succeeded and the reader at the actual key got ``None``.  The
    dataclass shim catches typos at write time."""

    def test_setitem_unknown_field_raises(self):
        s = _minimal_state()
        with self.assertRaises(KeyError) as ctx:
            s["pippeline"] = "anything"  # typo
        self.assertIn("pippeline", str(ctx.exception))

    def test_getitem_unknown_field_raises(self):
        """``[key]`` semantics — KeyError on miss, mirrors dict."""
        s = _minimal_state()
        with self.assertRaises(KeyError):
            _ = s["pippeline"]

    def test_get_unknown_field_does_not_raise(self):
        """``.get()`` semantics — never raises (mirrors dict.get)."""
        s = _minimal_state()
        self.assertIsNone(s.get("pippeline"))


class ConnStateRoundTripTests(unittest.TestCase):
    """``dict(state)``-style use in test mocks should round-trip."""

    def test_keys_returns_all_field_names(self):
        s = _minimal_state()
        ks = s.keys()
        # Spot-check a few critical fields are listed.
        self.assertIn("ws_id", ks)
        self.assertIn("pipeline", ks)
        self.assertIn("session_id", ks)
        self.assertIn("conn_lock", ks)
        self.assertIn("config", ks)


if __name__ == "__main__":
    unittest.main()
