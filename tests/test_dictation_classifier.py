"""Tests for the PR 4 dictation classifier.

Run from repo root:
    python3 -m pytest tests/test_dictation_classifier.py -q
"""

from __future__ import annotations

import pytest

from dragon_voice.dictation_classifier import classify_dictation


# ── Reminder detection ───────────────────────────────────────────────


def test_reminder_with_time_cue():
    r = classify_dictation("Call mom Tuesday at 6 PM")
    assert r["kind"] == "reminder"
    assert r["confidence"] >= 0.75
    assert "label" in r["payload"]


def test_reminder_with_in_phrase():
    r = classify_dictation("Remind me to take out the trash in 2 hours")
    assert r["kind"] == "reminder"
    assert r["confidence"] >= 0.75


def test_reminder_with_tomorrow():
    r = classify_dictation("Schedule dentist appointment tomorrow morning")
    assert r["kind"] == "reminder"
    assert r["confidence"] >= 0.75


def test_reminder_payload_when_parseable():
    r = classify_dictation("Call mom Tuesday at 6 PM")
    # parsed_when may or may not resolve depending on TZ + parser
    # heuristics, but the payload shape must be present.
    assert "when" in r["payload"]
    assert "label" in r["payload"]


# ── List detection ───────────────────────────────────────────────────


def test_list_comma_separated_short_items():
    r = classify_dictation("eggs, bread, butter, oat milk")
    assert r["kind"] == "list"
    assert r["confidence"] >= 0.75


def test_list_with_shopping_keyword():
    r = classify_dictation("shopping list: apples, oranges, bananas")
    assert r["kind"] == "list"
    assert r["confidence"] >= 0.75


def test_list_with_grocer_keyword():
    r = classify_dictation("grocery items I need to pick up: milk, cheese, butter")
    assert r["kind"] == "list"
    assert r["confidence"] >= 0.75


# ── None / low-confidence ────────────────────────────────────────────


def test_random_thought_is_none():
    r = classify_dictation(
        "I was thinking about how the weather has been weird this week"
    )
    # No keyword hit, no commas → confidence below floor.
    assert r["confidence"] < 0.75


def test_empty_transcript():
    r = classify_dictation("")
    assert r == {"kind": "none", "confidence": 0.0, "payload": {}}


def test_none_transcript():
    r = classify_dictation(None)  # type: ignore[arg-type]
    assert r == {"kind": "none", "confidence": 0.0, "payload": {}}


def test_single_comma_is_not_a_list():
    # One comma alone shouldn't trigger the list classification — too
    # common in normal prose ("Hello, world").
    r = classify_dictation("Hello, friend")
    assert r["kind"] == "none" or r["confidence"] < 0.75


# ── Reminder vs list disambiguation ─────────────────────────────────


def test_reminder_wins_when_time_cue_present():
    # Time cue + commas — reminder should win.
    r = classify_dictation("Meeting Tuesday at 6pm with Alice, Bob, Carol")
    assert r["kind"] == "reminder"


def test_list_wins_when_no_time_cue():
    # Lots of commas, no time cue — list wins.
    r = classify_dictation("Items: pencil, paper, eraser, ruler")
    assert r["kind"] == "list"


# ── Output shape ────────────────────────────────────────────────────


def test_output_shape_invariants():
    r = classify_dictation("Call mom Tuesday at 6 PM")
    assert set(r.keys()) == {"kind", "confidence", "payload"}
    assert r["kind"] in {"reminder", "list", "none"}
    assert 0.0 <= r["confidence"] <= 1.0
    assert isinstance(r["payload"], dict)


def test_confidence_clamped_to_below_one():
    # Stack as many positive signals as possible — should not exceed 0.99.
    r = classify_dictation(
        "Remind me to email John tomorrow morning at 9am about the meeting"
    )
    assert r["confidence"] <= 0.99


# ── Sad-path / robustness ───────────────────────────────────────────


def test_handles_unicode_transcript():
    r = classify_dictation("Llámame mañana — recoge los huevos, pan, leche")
    # Spanish doesn't hit our English keywords; should fall to "none" or
    # match the comma list heuristic.  Either is fine, just shouldn't crash.
    assert r["kind"] in {"none", "list", "reminder"}


def test_handles_very_long_transcript():
    text = "buy " + ", ".join([f"item{i}" for i in range(50)])
    r = classify_dictation(text)
    assert r["kind"] == "list"
