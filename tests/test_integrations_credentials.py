"""#341 / #342 — CredentialStore: atomic JSON cred file with 0o600 mode."""

from __future__ import annotations

import json
import os
import stat

import pytest

from dragon_voice.tools.integrations.credentials import CredentialStore


@pytest.fixture
def store(tmp_path):
    return CredentialStore("test-integration", base_dir=tmp_path)


@pytest.mark.asyncio
async def test_load_returns_none_when_file_missing(store):
    assert (await store.load()) is None
    assert not (await store.exists())


@pytest.mark.asyncio
async def test_save_then_load_roundtrip(store):
    payload = {"access_token": "abc", "refresh_token": "xyz", "expires_at": 12345}
    await store.save(payload)
    assert await store.exists()
    loaded = await store.load()
    assert loaded == payload


@pytest.mark.asyncio
async def test_save_writes_mode_0600(store):
    await store.save({"token": "x"})
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


@pytest.mark.asyncio
async def test_save_creates_parent_dir(tmp_path):
    nested = tmp_path / "deep" / "nest"
    store = CredentialStore("nested-test", base_dir=nested)
    await store.save({"k": "v"})
    assert nested.exists()
    # Parent dir 0o700 — owner-only.
    mode = stat.S_IMODE(os.stat(nested).st_mode)
    assert mode == 0o700


@pytest.mark.asyncio
async def test_corrupt_file_treated_as_missing(store, caplog):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("not valid JSON {{{", encoding="utf-8")
    with caplog.at_level("WARNING"):
        result = await store.load()
    assert result is None
    assert any("corrupt JSON" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_non_object_file_treated_as_missing(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("[1, 2, 3]", encoding="utf-8")
    assert (await store.load()) is None


@pytest.mark.asyncio
async def test_delete_removes_file(store):
    await store.save({"k": "v"})
    assert await store.exists()
    await store.delete()
    assert not await store.exists()


@pytest.mark.asyncio
async def test_delete_idempotent(store):
    await store.delete()  # no-op when missing
    assert not await store.exists()


@pytest.mark.asyncio
async def test_atomic_write_no_partial_on_failure(store, monkeypatch):
    """If json.dump crashes mid-write, the original file (if any) is
    intact because we write to a temp first then os.replace."""
    await store.save({"v": 1})
    # First read confirms baseline.
    assert (await store.load()) == {"v": 1}

    # Force json.dump to raise so the write fails after temp open.
    real_dump = json.dump

    def broken_dump(*a, **kw):
        raise RuntimeError("simulated disk full")

    monkeypatch.setattr(json, "dump", broken_dump)
    with pytest.raises(RuntimeError):
        await store.save({"v": 2})

    monkeypatch.setattr(json, "dump", real_dump)
    # Original payload still intact — temp file never got swapped in.
    assert (await store.load()) == {"v": 1}
