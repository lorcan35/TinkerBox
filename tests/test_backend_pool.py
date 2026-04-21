"""Wave 15 W15-C01 regression — two VoicePipelines sharing a backend pool.

The production regression we're pinning here: without the pool, every
time Tab5's WS reconnected the server would tear down and re-load
Moonshine (~140 MB / reconnect).  With the pool, the second pipeline
sees a pool hit and re-uses the instance initialized by the first.

This test fakes the STT/TTS/LLM backends so it runs fast and without
downloading models — the important thing is the pool semantics, not
Moonshine itself.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from dragon_voice.pipeline import VoicePipeline, _stt_sig, _tts_sig, _llm_sig


class _FakeBackend:
    """Minimal stand-in for STT/TTS/LLM backends.

    Counts init/shutdown calls so the test can assert lifecycle.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.init_calls = 0
        self.shutdown_calls = 0
        self.sample_rate = 16000

    async def initialize(self) -> None:
        self.init_calls += 1

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def _cfg(stt="moonshine", stt_model="medium",
         tts="piper", piper_model="en_US-lessac-medium",
         llm="ollama", ollama_model="qwen3:1.7b"):
    return SimpleNamespace(
        stt=SimpleNamespace(backend=stt, model=stt_model),
        tts=SimpleNamespace(backend=tts, piper_model=piper_model,
                            kokoro_model=""),
        llm=SimpleNamespace(
            backend=llm, ollama_model=ollama_model,
            openrouter_model="", tinkerclaw_model="", lmstudio_model="",
        ),
    )


@pytest.fixture
def patch_factories(monkeypatch):
    """Replace the create_* factories with fakes that record names."""
    created: list[_FakeBackend] = []

    def mk(name_prefix):
        def _factory(config):
            fb = _FakeBackend(f"{name_prefix}({getattr(config, 'model', '') or getattr(config, 'piper_model', '') or getattr(config, 'ollama_model', '')})")
            created.append(fb)
            return fb
        return _factory

    monkeypatch.setattr("dragon_voice.pipeline.create_stt", mk("fake-stt"))
    monkeypatch.setattr("dragon_voice.pipeline.create_tts", mk("fake-tts"))
    monkeypatch.setattr("dragon_voice.pipeline.create_llm", mk("fake-llm"))
    return created


async def _noop(_):  # on_audio / on_event stub
    return None


def test_sig_helpers_produce_identical_keys():
    cfg_a = _cfg()
    cfg_b = _cfg()
    assert _stt_sig(cfg_a.stt) == _stt_sig(cfg_b.stt)
    assert _tts_sig(cfg_a.tts) == _tts_sig(cfg_b.tts)
    assert _llm_sig(cfg_a.llm) == _llm_sig(cfg_b.llm)


def test_sig_helpers_differ_on_model():
    cfg_a = _cfg(stt_model="medium")
    cfg_b = _cfg(stt_model="small")
    assert _stt_sig(cfg_a.stt) != _stt_sig(cfg_b.stt)


def test_pooled_pipeline_reuses_backends(patch_factories):
    """Second pipeline sharing a pool must NOT re-initialise backends."""
    created = patch_factories
    pool: dict = {}

    cfg = _cfg()

    p1 = VoicePipeline(
        cfg, _noop, _noop, backend_pool=pool,
    )
    asyncio.run(p1.initialize())

    # First pipeline: three fresh backends created + initialized.
    assert len(created) == 3
    assert all(b.init_calls == 1 for b in created)
    # After registering them in the pool, p1 flips its own pooled
    # flags so its eventual shutdown doesn't kill the shared instances.
    assert p1._pooled_stt is True
    assert p1._pooled_tts is True
    assert p1._pooled_llm is True

    # Pool is populated.
    assert len(pool) == 3

    # Second pipeline, same config, same pool.
    p2 = VoicePipeline(
        cfg, _noop, _noop, backend_pool=pool,
    )
    asyncio.run(p2.initialize())

    # No new backends were created.
    assert len(created) == 3
    # Each existing backend still has init_calls=1 (not re-initialised).
    assert all(b.init_calls == 1 for b in created)
    # p2 correctly marked every backend as pooled.
    assert p2._pooled_stt is True
    assert p2._pooled_tts is True
    assert p2._pooled_llm is True
    # And the pipelines share backend identity.
    assert p1._stt is p2._stt
    assert p1._tts is p2._tts
    assert p1._llm is p2._llm


def test_pooled_shutdown_is_noop(patch_factories):
    """Per-pipeline shutdown must NOT close pooled backends."""
    created = patch_factories
    pool: dict = {}

    p1 = VoicePipeline(
        _cfg(), _noop, _noop, backend_pool=pool,
    )
    asyncio.run(p1.initialize())

    p2 = VoicePipeline(
        _cfg(), _noop, _noop, backend_pool=pool,
    )
    asyncio.run(p2.initialize())

    # Shutdown p2 — pool-borrowed backends stay alive.
    asyncio.run(p2.shutdown())
    assert all(b.shutdown_calls == 0 for b in created)

    # Shutdown p1 — still no-op for pooled ones.
    asyncio.run(p1.shutdown())
    assert all(b.shutdown_calls == 0 for b in created)

    # Pool still owns the live instances.
    assert len(pool) == 3


def test_no_pool_pipeline_owns_backends(patch_factories):
    """Without a pool, pipelines retain wave-14 lifecycle."""
    created = patch_factories
    p = VoicePipeline(_cfg(), _noop, _noop)  # no backend_pool
    asyncio.run(p.initialize())
    assert len(created) == 3
    asyncio.run(p.shutdown())
    # Un-pooled: shutdown propagates to each backend.
    assert all(b.shutdown_calls == 1 for b in created)
