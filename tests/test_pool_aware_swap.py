"""Tests for ``dragon_voice.pool_aware_swap.swap_one_backend``.

Pin every branch of the swap matrix:
  * config_changed=False → no shutdown, no pool lookup, no init
  * config_changed=True + pooled-old → no shutdown, pool lookup
  * config_changed=True + not-pooled-old → shutdown, pool lookup
  * config_changed=True + pool hit → reuse pooled, mark pooled
  * config_changed=True + pool miss → factory, mark not-pooled, init task

Plus the W15-C01 closure: shutting down a pooled instance MUST
NOT happen (pool owns lifecycle).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dragon_voice.pool_aware_swap import (
    BackendInitTask,
    SwapResult,
    swap_one_backend,
)


def _make_backend(*, name: str = "backend") -> MagicMock:
    backend = MagicMock(name=name)
    backend.shutdown = AsyncMock()
    backend.initialize = AsyncMock()
    return backend


# ─── No-change branch ────────────────────────────────────────


class TestNoChange:
    @pytest.mark.asyncio
    async def test_returns_old_instance_unchanged(self):
        old = _make_backend()
        result = await swap_one_backend(
            kind="stt",
            config_changed=False,
            old_instance=old,
            old_is_pooled=False,
            new_signature=("moonshine",),
            new_factory=lambda: _make_backend(),  # MUST NOT be called
            pool={},
        )
        assert result.new_instance is old
        assert result.is_pooled is False
        assert result.init_task is None

    @pytest.mark.asyncio
    async def test_no_change_does_not_shutdown_old(self):
        old = _make_backend()
        await swap_one_backend(
            kind="stt",
            config_changed=False,
            old_instance=old,
            old_is_pooled=False,
            new_signature=("x",),
            new_factory=lambda: _make_backend(),
            pool=None,
        )
        old.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_change_does_not_invoke_factory(self):
        factory_calls = {"n": 0}

        def _factory():
            factory_calls["n"] += 1
            return _make_backend()

        await swap_one_backend(
            kind="stt",
            config_changed=False,
            old_instance=_make_backend(),
            old_is_pooled=False,
            new_signature=("x",),
            new_factory=_factory,
            pool=None,
        )
        assert factory_calls["n"] == 0

    @pytest.mark.asyncio
    async def test_no_change_preserves_pooled_flag(self):
        old = _make_backend()
        result = await swap_one_backend(
            kind="stt",
            config_changed=False,
            old_instance=old,
            old_is_pooled=True,
            new_signature=("x",),
            new_factory=lambda: _make_backend(),
            pool=None,
        )
        # Old pooled flag preserved
        assert result.is_pooled is True


# ─── Shutdown branch ─────────────────────────────────────────


class TestShutdown:
    @pytest.mark.asyncio
    async def test_changed_non_pooled_shutdown_old(self):
        old = _make_backend()
        await swap_one_backend(
            kind="stt",
            config_changed=True,
            old_instance=old,
            old_is_pooled=False,
            new_signature=("new",),
            new_factory=lambda: _make_backend(),
            pool=None,
        )
        old.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_changed_pooled_does_NOT_shutdown_old(self):
        """W15-C01 closure pin: a pooled instance is the pool's
        responsibility to shut down — tearing it down here would
        break other connections sharing it."""
        old = _make_backend()
        await swap_one_backend(
            kind="stt",
            config_changed=True,
            old_instance=old,
            old_is_pooled=True,
            new_signature=("new",),
            new_factory=lambda: _make_backend(),
            pool=None,
        )
        old.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_changed_with_no_old_instance_does_not_crash(self):
        """Initial install (no old instance yet) must not crash
        on the shutdown path."""
        await swap_one_backend(
            kind="stt",
            config_changed=True,
            old_instance=None,
            old_is_pooled=False,
            new_signature=("new",),
            new_factory=lambda: _make_backend(),
            pool=None,
        )
        # Just must not raise


# ─── Pool-hit branch ────────────────────────────────────────


class TestPoolHit:
    @pytest.mark.asyncio
    async def test_pool_hit_reuses_pooled_instance(self):
        pooled = _make_backend(name="pooled-instance")
        pool = {("openrouter",): pooled}

        result = await swap_one_backend(
            kind="stt",
            config_changed=True,
            old_instance=_make_backend(),
            old_is_pooled=False,
            new_signature=("openrouter",),
            new_factory=lambda: _make_backend(name="should-not-create"),
            pool=pool,
        )

        assert result.new_instance is pooled
        assert result.is_pooled is True
        assert result.init_task is None

    @pytest.mark.asyncio
    async def test_pool_hit_does_not_call_factory(self):
        pool = {("k",): _make_backend()}
        factory_calls = {"n": 0}

        def _factory():
            factory_calls["n"] += 1
            return _make_backend()

        await swap_one_backend(
            kind="stt",
            config_changed=True,
            old_instance=_make_backend(),
            old_is_pooled=False,
            new_signature=("k",),
            new_factory=_factory,
            pool=pool,
        )
        assert factory_calls["n"] == 0


# ─── Pool-miss branch ───────────────────────────────────────


class TestPoolMiss:
    @pytest.mark.asyncio
    async def test_pool_miss_creates_via_factory(self):
        new = _make_backend(name="new-via-factory")

        result = await swap_one_backend(
            kind="stt",
            config_changed=True,
            old_instance=_make_backend(),
            old_is_pooled=False,
            new_signature=("k",),
            new_factory=lambda: new,
            pool={},  # empty pool
        )

        assert result.new_instance is new
        assert result.is_pooled is False
        assert result.init_task is not None
        assert result.init_task.instance is new
        assert result.init_task.key == ("k",)
        assert result.init_task.kind == "stt"

    @pytest.mark.asyncio
    async def test_no_pool_at_all_creates_via_factory(self):
        new = _make_backend()

        result = await swap_one_backend(
            kind="tts",
            config_changed=True,
            old_instance=_make_backend(),
            old_is_pooled=False,
            new_signature=("k",),
            new_factory=lambda: new,
            pool=None,  # pool disabled
        )

        assert result.new_instance is new
        assert result.is_pooled is False
        assert result.init_task is not None

    @pytest.mark.asyncio
    async def test_pool_miss_does_NOT_initialize_in_helper(self):
        """The init_task is a *deferred* call — the helper
        returns it for the caller to batch with asyncio.gather.
        Pin: initialize() is NOT called inside the helper."""
        new = _make_backend()

        await swap_one_backend(
            kind="llm",
            config_changed=True,
            old_instance=None,
            old_is_pooled=False,
            new_signature=("k",),
            new_factory=lambda: new,
            pool={},
        )

        new.initialize.assert_not_awaited()


# ─── Return-value contract ──────────────────────────────────


class TestReturnContract:
    @pytest.mark.asyncio
    async def test_returns_swap_result_namedtuple(self):
        result = await swap_one_backend(
            kind="stt",
            config_changed=False,
            old_instance=_make_backend(),
            old_is_pooled=False,
            new_signature=("k",),
            new_factory=lambda: _make_backend(),
            pool=None,
        )
        assert isinstance(result, SwapResult)
        # Three fields
        assert hasattr(result, "new_instance")
        assert hasattr(result, "is_pooled")
        assert hasattr(result, "init_task")

    @pytest.mark.asyncio
    async def test_init_task_is_namedtuple_with_three_fields(self):
        new = _make_backend()
        result = await swap_one_backend(
            kind="tts",
            config_changed=True,
            old_instance=None,
            old_is_pooled=False,
            new_signature=("k",),
            new_factory=lambda: new,
            pool={},
        )
        assert isinstance(result.init_task, BackendInitTask)
        assert result.init_task.instance is new
        assert result.init_task.key == ("k",)
        assert result.init_task.kind == "tts"
