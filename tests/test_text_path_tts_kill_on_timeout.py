"""Phase 2 L3 (#89, refs #94): text-path TTS kill-on-timeout.

The voice path's L3 fix shipped earlier in this phase
(`pipeline._synthesize_and_send` calls `kill_active_procs` on
TimeoutError — see test_tts_flush_and_kill.py).  The text path was
left behind: `_handle_text_body` wrapped `pipeline._tts.synthesize`
with a `wait_for(timeout=tts_timeout)` but the surrounding
`except Exception` only logged + sent `tts_end`, leaving any
in-flight Piper subprocess to run to completion and hold the audio
device + an FD.

Same class of bug as A1 (cancel didn't kill text-path Piper) — but
on the timeout edge.  This test pins down via source inspection so
a future refactor of `_handle_text_body` doesn't accidentally drop
the kill.
"""
from __future__ import annotations

import inspect


def test_handle_text_body_kills_piper_on_tts_timeout() -> None:
    """``synthesize_and_stream_text_response``'s TTS except branch must
    call ``kill_active_procs()`` to clean up an in-flight Piper
    subprocess on timeout / failure.  Mirrors the voice path at
    ``pipeline.py:1411-1421`` (audit L3 / Phase 2 of #89).

    SOLID-audit follow-up: the text-path TTS chunk was extracted
    from ``_handle_text_body`` into
    ``dragon_voice.text_path_tts.synthesize_and_stream_text_response``
    in PR-E of round 4.  This test now chases the inspection into
    that module so the L3 zombie-kill invariant still gets pinned.
    """
    from dragon_voice.text_path_tts import (
        synthesize_and_stream_text_response,
    )
    src = inspect.getsource(synthesize_and_stream_text_response)
    # The except must catch TimeoutError explicitly so the timeout
    # branch is distinguishable from a generic synthesize failure.
    assert "asyncio.TimeoutError" in src, (
        "text-path TTS except must catch TimeoutError so we can log "
        "the right thing on a real timeout vs a generic failure"
    )
    # The kill itself must be present and guarded by the
    # hasattr check so non-Piper TTS backends don't blow up.
    assert "kill_active_procs" in src, (
        "text-path TTS except must invoke kill_active_procs to clean "
        "up the in-flight Piper subprocess on timeout (Phase 2 L3)"
    )
    # Must still send tts_end so Tab5 doesn't hang in SPEAKING.
    assert '"tts_end"' in src and '"tts_ms": 0' in src, (
        "text-path TTS except must still emit tts_end with tts_ms=0 "
        "so Tab5 leaves SPEAKING state"
    )
