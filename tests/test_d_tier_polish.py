"""Tests for D-tier polish:
  * D3 (#137): /api/v1/system exposes inference_executor depth
  * D4 (#137): MediaPipeline.has_renderable_content + the call sites
    emit a media_rendering progress frame when there's renderable
    content to render
"""
from __future__ import annotations

import asyncio

import pytest

# ─────────────────────────── D3 — has_renderable_content


from dragon_voice.media.pipeline import MediaPipeline  # noqa: E402


def test_has_renderable_content_detects_code_block() -> None:
    text = "Sure, here's the snippet:\n```python\nprint('hi')\n```"
    assert MediaPipeline.has_renderable_content(text) is True


def test_has_renderable_content_detects_image_url() -> None:
    text = "See https://example.com/diagram.png for the layout."
    assert MediaPipeline.has_renderable_content(text) is True


def test_has_renderable_content_detects_markdown_table() -> None:
    text = (
        "| Col1 | Col2 |\n"
        "| ---- | ---- |\n"
        "| a    | b    |\n"
    )
    assert MediaPipeline.has_renderable_content(text) is True


def test_has_renderable_content_returns_false_for_plain_text() -> None:
    text = "The capital of France is Paris.  Anything else you'd like to know?"
    assert MediaPipeline.has_renderable_content(text) is False


def test_has_renderable_content_handles_empty_input() -> None:
    assert MediaPipeline.has_renderable_content("") is False
    assert MediaPipeline.has_renderable_content(None) is False  # type: ignore[arg-type]


# ─────────────────────────── D3 — system endpoint exposes executor metrics


def test_system_endpoint_includes_inference_executor_block() -> None:
    """The /api/v1/system handler must include an `inference_executor`
    block carrying max_workers, busy, queued.  Verified by inspecting
    the source of `system_info`."""
    import inspect
    from dragon_voice.api.system import SystemRoutes
    src = inspect.getsource(SystemRoutes.system_info)
    assert "inference_executor" in src, (
        "system_info must build the inference_executor metrics block (D3)"
    )
    assert "max_workers" in src and "queued" in src and "busy" in src, (
        "expected max_workers/queued/busy keys in inference_executor block"
    )


# ─────────────────────────── D4 — render-progress emit at call sites


def test_text_path_emits_media_rendering_progress_before_render() -> None:
    """Pin via source inspection so a refactor that drops the progress
    emit re-opens the perceived-stall window between llm_done and
    media frames.  Audit B1 (#165) extracted the body to
    `_handle_text_body`; the emit lives there now."""
    import inspect
    from dragon_voice.server import VoiceServer
    src = inspect.getsource(VoiceServer._handle_text_body)
    assert "has_renderable_content(response_text)" in src, (
        "_handle_text_body must guard the progress emit on has_renderable_content"
    )
    assert '"media_rendering"' in src and '"start"' in src, (
        "_handle_text_body must emit type=media_rendering, stage=start"
    )


def test_voice_path_emits_media_rendering_progress_before_render() -> None:
    """Same D4 guard for the voice path in pipeline._process_utterance."""
    import inspect
    from dragon_voice.pipeline import VoicePipeline
    src = inspect.getsource(VoicePipeline._process_utterance)
    assert "has_renderable_content(full_response)" in src, (
        "_process_utterance must guard progress emit on has_renderable_content"
    )
    assert '"media_rendering"' in src, (
        "_process_utterance must emit type=media_rendering"
    )
