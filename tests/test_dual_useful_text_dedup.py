"""Tests pinning the SRP-9 dedup contract for
``dragon_voice.llm.dual``.

Pre-PR-#267 `dual.py` had its own private
`_looks_like_useful_text` that mirrored
`tools.response_wrap.looks_like_useful_text`.  Two independent
implementations of the same heuristic guaranteed drift over
time — small models hit the dual path while everything else
hit the response_wrap path.

This test pins:

  1. The dual path's `_looks_like_useful_text` IS the
     canonical `tools.response_wrap.looks_like_useful_text`
     (object-identity check).
  2. Truth-table parity: a few characteristic inputs return
     the same answer under both names (catches a future
     re-divergence at module load time).
"""
from __future__ import annotations

import pytest


def test_dual_useful_text_is_canonical_object_identity():
    """The two names MUST resolve to the same callable.  If
    someone re-introduces a private fork in dual.py, this test
    fails immediately + loudly."""
    from dragon_voice.llm.dual import _looks_like_useful_text as dual_fn
    from dragon_voice.tools.response_wrap import looks_like_useful_text as canon
    assert dual_fn is canon, (
        "dual.py's _looks_like_useful_text must be the canonical "
        "tools.response_wrap.looks_like_useful_text — re-export, don't fork"
    )


@pytest.mark.parametrize("text,expected", [
    # Empty / whitespace
    ("", False),
    ("   ", False),
    # Bracket-noise only — pre-extract heuristic returns False
    ("<><>[]", False),
    # Residual closing tag → False
    ("hi</tool>", False),
    # Real text → True
    ("hello world", True),
    ("OK.", True),
    # Single 'a' is < 3 meaningful chars → False
    ("a", False),
    # Real prose with embedded bracket noise → True
    ("The answer is 42 [confident]", True),
])
def test_dual_useful_text_truth_table_matches_canonical(text, expected):
    """Smoke parity: a few characteristic inputs go through
    dual's name and return the canonical's answer."""
    from dragon_voice.llm.dual import _looks_like_useful_text
    assert _looks_like_useful_text(text) is expected
