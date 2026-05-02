"""Tests for ConversationEngine.swap_llm + fleet_summary (#202, Wave 22b).

The methods extracted from server.py's inline swap path. These tests
pin the four swap branches (router / pool-hit / pool-miss / first-swap)
plus the fleet_summary contract.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.config import LLMConfig
from dragon_voice.conversation import ConversationEngine


def _make_engine() -> ConversationEngine:
    """Build a minimal ConversationEngine for swap-path testing.

    DB / MessageStore are mocked since swap_llm doesn't touch them.
    """
    return ConversationEngine(
        db=MagicMock(),
        message_store=MagicMock(),
        llm_config=LLMConfig(backend="ollama", ollama_model="ministral-3:3b"),
    )


def _make_fake_backend(name: str = "fake") -> AsyncMock:
    """A backend stand-in: just enough surface for swap_llm to drive it."""
    be = AsyncMock()
    be.name = name
    be.shutdown = AsyncMock()
    be.initialize = AsyncMock()
    return be


# ─────────────────────────────────────────────────────────────────────
# fleet_summary
# ─────────────────────────────────────────────────────────────────────


def test_fleet_summary_returns_none_for_non_router_backend():
    eng = _make_engine()
    eng._llm = _make_fake_backend("ollama-fake")
    assert eng.fleet_summary(0) is None


def test_fleet_summary_delegates_to_router_summarize():
    from dragon_voice.llm.router import CapabilityAwareRouter

    eng = _make_engine()
    router = MagicMock(spec=CapabilityAwareRouter)
    router.summarize.return_value = {"text": "fake-text-model"}
    eng._llm = router

    out = eng.fleet_summary(2)
    assert out == {"text": "fake-text-model"}
    router.summarize.assert_called_once_with(2)


def test_fleet_summary_when_llm_is_none():
    eng = _make_engine()
    # _llm starts as None until initialize()
    assert eng.fleet_summary(0) is None


# ─────────────────────────────────────────────────────────────────────
# swap_llm — router branch
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_swap_llm_router_branch_does_not_swap():
    """Router stays warm; only voice_mode + config are updated."""
    from dragon_voice.llm.router import CapabilityAwareRouter

    eng = _make_engine()
    router = MagicMock(spec=CapabilityAwareRouter)
    router.set_voice_mode = MagicMock()
    router.name = "Router(...)"
    eng._llm = router

    new_cfg = LLMConfig(backend="router", ollama_model="ministral-3:3b")
    pool: dict = {}

    new_llm, was_pooled = await eng.swap_llm(new_cfg, pool=pool, voice_mode=2)

    assert new_llm is router
    assert was_pooled is False
    router.set_voice_mode.assert_called_once_with(2)
    assert eng._llm_config is new_cfg
    # Pool stays empty — no backend creation in the router branch
    assert pool == {}


@pytest.mark.asyncio
async def test_swap_llm_router_branch_skips_set_voice_mode_when_voice_mode_none():
    from dragon_voice.llm.router import CapabilityAwareRouter

    eng = _make_engine()
    router = MagicMock(spec=CapabilityAwareRouter)
    router.set_voice_mode = MagicMock()
    eng._llm = router

    await eng.swap_llm(
        LLMConfig(backend="router"),
        pool={},
        voice_mode=None,
    )
    router.set_voice_mode.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# swap_llm — non-router branch
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_swap_llm_pool_hit_reuses_and_does_not_shutdown_old(monkeypatch):
    eng = _make_engine()
    old = _make_fake_backend("old")
    pooled = _make_fake_backend("pooled")
    eng._llm = old

    new_cfg = LLMConfig(backend="ollama", ollama_model="gemma3:4b")
    # _llm_sig must produce a stable key
    monkeypatch.setattr(
        "dragon_voice.pipeline._llm_sig", lambda cfg: "ollama:gemma3:4b"
    )
    # create_llm should NOT be called on a pool hit
    create_llm_mock = MagicMock(side_effect=AssertionError("create_llm called on pool hit"))
    monkeypatch.setattr("dragon_voice.llm.create_llm", create_llm_mock)

    pool = {"ollama:gemma3:4b": pooled}
    new_llm, was_pooled = await eng.swap_llm(new_cfg, pool=pool, voice_mode=0)

    assert new_llm is pooled
    assert was_pooled is True
    assert eng._llm is pooled
    assert eng._llm_config is new_cfg
    # Old wasn't pooled, so it gets shutdown
    old.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_swap_llm_pool_miss_creates_and_pools(monkeypatch):
    eng = _make_engine()
    old = _make_fake_backend("old")
    eng._llm = old

    new_backend = _make_fake_backend("brand-new")
    create_llm_mock = MagicMock(return_value=new_backend)
    monkeypatch.setattr("dragon_voice.llm.create_llm", create_llm_mock)
    monkeypatch.setattr(
        "dragon_voice.pipeline._llm_sig", lambda cfg: "ollama:fresh"
    )

    pool: dict = {}
    new_cfg = LLMConfig(backend="ollama", ollama_model="fresh-model")
    new_llm, was_pooled = await eng.swap_llm(new_cfg, pool=pool, voice_mode=0)

    assert new_llm is new_backend
    assert was_pooled is False
    new_backend.initialize.assert_awaited_once()
    assert pool == {"ollama:fresh": new_backend}
    old.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_swap_llm_first_swap_no_old_no_shutdown(monkeypatch):
    eng = _make_engine()
    eng._llm = None  # initialize() never ran

    new_backend = _make_fake_backend("first")
    monkeypatch.setattr("dragon_voice.llm.create_llm",
                        MagicMock(return_value=new_backend))
    monkeypatch.setattr("dragon_voice.pipeline._llm_sig", lambda cfg: "key")

    pool: dict = {}
    new_llm, was_pooled = await eng.swap_llm(
        LLMConfig(backend="ollama", ollama_model="x"),
        pool=pool,
        voice_mode=0,
    )

    assert new_llm is new_backend
    assert was_pooled is False
    # No old to shut down — nothing should fail
    new_backend.initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_swap_llm_skips_old_shutdown_if_still_pooled(monkeypatch):
    """If the old backend is still referenced in the pool (e.g. the new
    config maps to the same key), don't shut it down."""
    eng = _make_engine()
    shared = _make_fake_backend("shared")
    eng._llm = shared
    pool = {"only-key": shared}

    monkeypatch.setattr("dragon_voice.pipeline._llm_sig", lambda cfg: "only-key")
    monkeypatch.setattr("dragon_voice.llm.create_llm",
                        MagicMock(side_effect=AssertionError("should not create")))

    new_llm, was_pooled = await eng.swap_llm(
        LLMConfig(backend="ollama", ollama_model="x"),
        pool=pool,
        voice_mode=0,
    )

    assert new_llm is shared
    assert was_pooled is True
    # shared is the new + old, never gets shut down
    shared.shutdown.assert_not_awaited()
