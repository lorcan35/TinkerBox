"""Tests for ``LMStudioBackend.generate_stream_with_messages`` (LSP-1, audit 2026-05-03).

Pre-fix LMStudioBackend inherited the broken default impl from
:class:`LLMBackend` which f-string-formats messages and re-routes
through ``generate_stream(prompt, system_prompt)``.  When a user
message's ``content`` is a multimodal array (OpenAI vision shape),
the f-string produced ``"User: [{'type': 'image_url', ...}]"`` and
the model never saw the image — silent text-only degradation.

These tests pin:
  1. The override exists on LMStudioBackend (not the base default).
  2. A multimodal content array is forwarded verbatim to the
     ``/chat/completions`` payload.
  3. SSE streaming surfaces tokens correctly from the override path.

Run:
    python3 -m pytest -v tests/test_lmstudio_multimodal.py
"""
from __future__ import annotations

import inspect
import unittest

import pytest

from dragon_voice.config import LLMConfig
from dragon_voice.llm.base import LLMBackend
from dragon_voice.llm.lmstudio_llm import LMStudioBackend


def _make_backend() -> LMStudioBackend:
    cfg = LLMConfig(
        backend="lmstudio",
        lmstudio_url="http://localhost:1234/v1",
        lmstudio_model="test-model",
        max_tokens=64,
        temperature=0.5,
    )
    return LMStudioBackend(cfg)


class _FakeSseResponse:
    """Mimic an aiohttp streaming response for the SSE path."""

    def __init__(self, lines: list[bytes], status: int = 200):
        self.status = status
        self.content = self._iter_lines(lines)
        self._text = b"".join(lines).decode()

    @staticmethod
    async def _iter_lines(lines):
        for line in lines:
            yield line

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    """Minimal aiohttp.ClientSession stub.  Captures the post payload."""

    def __init__(self, sse_lines: list[bytes], status: int = 200):
        self.closed = False
        self._sse_lines = sse_lines
        self._status = status
        self.last_payload: dict | None = None
        self.last_url: str | None = None

    def post(self, url, *, json):  # noqa: A002 — aiohttp's keyword is `json`
        self.last_url = url
        self.last_payload = json
        return _FakeSseResponse(self._sse_lines, status=self._status)

    async def close(self):
        self.closed = True


class LmStudioMultimodalTests(unittest.TestCase):
    def test_override_exists_on_subclass(self):
        """LSP-1 anchor: the override must be defined on
        LMStudioBackend itself, not inherited from LLMBackend."""
        # Inspect the method resolution: it should resolve to the
        # subclass, not the base.
        own = LMStudioBackend.generate_stream_with_messages
        base = LLMBackend.generate_stream_with_messages
        self.assertIsNot(
            own,
            base,
            "LMStudioBackend.generate_stream_with_messages must override"
            " the base default — see LSP-1 in docs/AUDIT-solid-2026-05-03.md.",
        )
        # It should still be an async generator function (yields tokens).
        self.assertTrue(inspect.isasyncgenfunction(own))


class TestLmStudioMultimodalAsync:
    """pytest-style async tests for the streaming path.

    Class name starts with `Test` so pytest collects these (unittest's
    ``unittest.TestCase`` doesn't play well with ``@pytest.mark.asyncio``).
    """

    @pytest.mark.asyncio
    async def test_multimodal_content_array_forwarded_verbatim(self):
        """The vision content array must reach the /chat/completions
        payload as a list, not a string repr."""
        sse = [
            b'data: {"choices":[{"delta":{"content":"a red"}}]}\n',
            b'data: {"choices":[{"delta":{"content":" chair"}}]}\n',
            b"data: [DONE]\n",
        ]
        backend = _make_backend()
        backend._session = _FakeSession(sse)

        multimodal = [
            {"role": "system", "content": "You are a vision assistant."},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,/9j/..."},
                    },
                    {"type": "text", "text": "what color is the chair?"},
                ],
            },
        ]

        tokens = []
        async for tok in backend.generate_stream_with_messages(multimodal):
            tokens.append(tok)

        # Tokens streamed from SSE.
        assert tokens == ["a red", " chair"]

        # Critical: the payload sent to LM Studio must contain the
        # ORIGINAL list — not a string-formatted repr.  This is the
        # exact failure mode LSP-1 documented.
        payload = backend._session.last_payload
        assert payload is not None
        assert payload["model"] == "test-model"
        user_content = payload["messages"][1]["content"]
        assert isinstance(user_content, list), (
            f"multimodal content array must reach /chat/completions as a list, "
            f"got {type(user_content).__name__}: {user_content!r}"
        )
        # Round-trip the parts to confirm nothing got stringified.
        assert user_content[0]["type"] == "image_url"
        assert user_content[0]["image_url"]["url"].startswith("data:image/jpeg")
        assert user_content[1] == {"type": "text", "text": "what color is the chair?"}

    @pytest.mark.asyncio
    async def test_does_not_mutate_self_conversation(self):
        """Override must NOT touch self._conversation — that history
        belongs to the legacy generate_stream() path used by the
        non-ConvEngine code paths.  ConvEngine manages its own
        context (mirrors openrouter_llm.py:321 design)."""
        sse = [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n', b"data: [DONE]\n"]
        backend = _make_backend()
        backend._session = _FakeSession(sse)
        backend._conversation = [{"role": "user", "content": "previous"}]

        async for _ in backend.generate_stream_with_messages(
            [{"role": "user", "content": "new"}]
        ):
            pass

        assert backend._conversation == [{"role": "user", "content": "previous"}], (
            "generate_stream_with_messages must not mutate self._conversation"
        )

    @pytest.mark.asyncio
    async def test_status_error_yields_error_marker(self):
        backend = _make_backend()
        backend._session = _FakeSession([], status=503)

        tokens = []
        async for tok in backend.generate_stream_with_messages(
            [{"role": "user", "content": "x"}]
        ):
            tokens.append(tok)
        # Single error marker; matches the legacy generate_stream shape.
        assert tokens == ["[LM Studio error: 503]"]


if __name__ == "__main__":
    unittest.main()
