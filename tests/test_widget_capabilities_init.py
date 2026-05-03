"""Tests for ``dragon_voice.widget_capabilities_init``.

Pin the client-supplied / default-fallback / defensive-skip
branches and the default-shape constant.
"""
from __future__ import annotations

import pytest

from dragon_voice.widget_capabilities_init import (
    _DEFAULT_WIDGET_CAPABILITIES,
    init_widget_capabilities,
)


class TestClientSupplied:
    def test_full_widget_block_passes_through(self):
        client_widgets = {
            "types": ["live", "card", "list", "chart", "media", "prompt"],
            "list_max_items": 12,
            "chart_max_points": 64,
            "prompt_max_choices": 5,
            "media_max_bytes": 524288,
        }
        conn_state: dict = {}

        init_widget_capabilities(
            conn_state,
            capabilities={"widgets": client_widgets},
            device_id="dev-A",
        )

        assert conn_state["widget_capabilities"] is client_widgets

    def test_partial_widget_block_passes_through_verbatim(self):
        """A client-supplied block must NOT be merged with the
        defaults — Tab5 declared exactly what it can render and
        we trust it.  The defaults are only for legacy clients
        without the field at all."""
        partial = {"types": ["live"], "list_max_items": 1}
        conn_state: dict = {}

        init_widget_capabilities(
            conn_state,
            capabilities={"widgets": partial},
            device_id="dev-B",
        )

        # Pin: NO merge with defaults.
        assert conn_state["widget_capabilities"] == partial
        # Specifically NOT containing the default keys
        assert "chart_max_points" not in conn_state["widget_capabilities"]


class TestDefaultFallback:
    def test_no_capabilities_uses_defaults(self):
        """Legacy register frame with no capabilities block at all."""
        conn_state: dict = {}

        init_widget_capabilities(
            conn_state,
            capabilities=None,
            device_id="dev-C",
        )

        assert conn_state["widget_capabilities"] == _DEFAULT_WIDGET_CAPABILITIES

    def test_capabilities_without_widgets_field_uses_defaults(self):
        """Capabilities block exists (e.g. has `audio_codec`) but
        `widgets` is missing → defaults."""
        conn_state: dict = {}

        init_widget_capabilities(
            conn_state,
            capabilities={"audio_codec": ["pcm"]},
            device_id="dev-D",
        )

        assert conn_state["widget_capabilities"] == _DEFAULT_WIDGET_CAPABILITIES

    def test_widgets_explicitly_empty_uses_defaults(self):
        """`widgets: {}` should fall through to defaults — an
        empty dict is falsy in the `or dict(...)` expression."""
        conn_state: dict = {}

        init_widget_capabilities(
            conn_state,
            capabilities={"widgets": {}},
            device_id="dev-E",
        )

        # Pre-extract behaviour: `widget_caps or {default}` —
        # empty dict triggers the default branch.
        assert conn_state["widget_capabilities"] == _DEFAULT_WIDGET_CAPABILITIES

    def test_default_dict_is_a_copy_not_a_shared_reference(self):
        """Two connections sharing the default must NOT see each
        other's mutations.  Pin so we don't accidentally hand out
        the module-level constant directly."""
        conn_state_1: dict = {}
        conn_state_2: dict = {}

        init_widget_capabilities(
            conn_state_1, capabilities=None, device_id="d1",
        )
        init_widget_capabilities(
            conn_state_2, capabilities=None, device_id="d2",
        )

        assert conn_state_1["widget_capabilities"] is not conn_state_2["widget_capabilities"]
        # Mutating conn_state_1 must not bleed into conn_state_2
        conn_state_1["widget_capabilities"]["types"].append("custom")
        assert "custom" not in conn_state_2["widget_capabilities"]["types"]


class TestDefensiveSkip:
    def test_capabilities_not_a_dict_falls_back_to_defaults(self):
        """Defensive: if Tab5 sends a malformed capabilities field
        (e.g. a string) we silently use defaults rather than
        crashing the whole register flow."""
        conn_state: dict = {}

        init_widget_capabilities(
            conn_state,
            capabilities="malformed-string",  # type: ignore[arg-type]
            device_id="dev-F",
        )

        assert conn_state["widget_capabilities"] == _DEFAULT_WIDGET_CAPABILITIES


class TestDefaultShape:
    def test_default_capabilities_constant_shape(self):
        """Pin the default shape so a future refactor can't
        silently widen the floor (e.g. allowing 12 list items by
        default would crash early-Wave Tab5 firmware)."""
        assert _DEFAULT_WIDGET_CAPABILITIES["types"] == ["live", "card"]
        assert _DEFAULT_WIDGET_CAPABILITIES["list_max_items"] == 3
        assert _DEFAULT_WIDGET_CAPABILITIES["chart_max_points"] == 8
        assert _DEFAULT_WIDGET_CAPABILITIES["prompt_max_choices"] == 2
