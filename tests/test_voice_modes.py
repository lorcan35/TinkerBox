"""Tests for ``dragon_voice.voice_modes.VoiceMode`` (audit OCP-1/2/6).

Pins three contracts that future refactors must not break:
  1. The integer wire values are part of the protocol (Tab5 sends
     ``"voice_mode": N`` JSON ints).  Reordering breaks Tab5 firmware
     in the field.
  2. ``from_int`` is a safe parser — never raises on bad input.
  3. Each semantic predicate matches the real-world behavior the
     code was checking with magic numbers pre-extract.
"""
from __future__ import annotations

import unittest

from dragon_voice.voice_modes import VoiceMode


class VoiceModeWireValuesTests(unittest.TestCase):
    """The integer values are part of the WS protocol contract."""

    def test_local_is_zero(self):
        self.assertEqual(int(VoiceMode.LOCAL), 0)

    def test_hybrid_is_one(self):
        self.assertEqual(int(VoiceMode.HYBRID), 1)

    def test_cloud_is_two(self):
        self.assertEqual(int(VoiceMode.CLOUD), 2)

    def test_tinkerclaw_is_three(self):
        self.assertEqual(int(VoiceMode.TINKERCLAW), 3)

    def test_onboard_is_four(self):
        self.assertEqual(int(VoiceMode.ONBOARD), 4)

    def test_total_modes(self):
        # If a 6th mode is added, also update Tab5 firmware's enum.
        self.assertEqual(len(VoiceMode), 5)


class VoiceModeFromIntTests(unittest.TestCase):
    """``from_int`` is a safe parser — never raises."""

    def test_valid_ints_round_trip(self):
        for i in range(5):
            with self.subTest(value=i):
                vm = VoiceMode.from_int(i)
                self.assertIsNotNone(vm)
                self.assertEqual(int(vm), i)

    def test_none_returns_none(self):
        self.assertIsNone(VoiceMode.from_int(None))

    def test_out_of_range_returns_none(self):
        self.assertIsNone(VoiceMode.from_int(-1))
        self.assertIsNone(VoiceMode.from_int(5))
        self.assertIsNone(VoiceMode.from_int(99))

    def test_string_returns_none(self):
        # Tab5 firmware should never send a string, but if a future
        # protocol change does, we shouldn't crash.
        self.assertIsNone(VoiceMode.from_int("local"))
        self.assertIsNone(VoiceMode.from_int(""))

    def test_string_int_works(self):
        # JSON sometimes deserialises numeric strings — accept those.
        self.assertEqual(VoiceMode.from_int("2"), VoiceMode.CLOUD)


class VoiceModeSingleTierPredicatesTests(unittest.TestCase):
    """Each tier predicate matches exactly one mode."""

    def test_is_local_only_local(self):
        for vm in VoiceMode:
            with self.subTest(mode=vm.name):
                self.assertEqual(vm.is_local(), vm == VoiceMode.LOCAL)

    def test_is_hybrid_only_hybrid(self):
        for vm in VoiceMode:
            with self.subTest(mode=vm.name):
                self.assertEqual(vm.is_hybrid(), vm == VoiceMode.HYBRID)

    def test_is_cloud_only_cloud(self):
        for vm in VoiceMode:
            with self.subTest(mode=vm.name):
                self.assertEqual(vm.is_cloud(), vm == VoiceMode.CLOUD)

    def test_is_tinkerclaw_only_tc(self):
        for vm in VoiceMode:
            with self.subTest(mode=vm.name):
                self.assertEqual(vm.is_tinkerclaw(), vm == VoiceMode.TINKERCLAW)

    def test_is_onboard_only_onboard(self):
        for vm in VoiceMode:
            with self.subTest(mode=vm.name):
                self.assertEqual(vm.is_onboard(), vm == VoiceMode.ONBOARD)


class VoiceModeSemanticGroupingTests(unittest.TestCase):
    """The semantic groupings encode policy.  These tests pin which
    modes belong to which group so future additions don't silently
    break the OpenRouter-key check or the cloud-STT/TTS routing.
    """

    def test_needs_cloud_stt_tts_is_hybrid_and_cloud(self):
        # Pre-extract: voice_mode in (1, 2) at server.py:2240.
        expected = {VoiceMode.HYBRID, VoiceMode.CLOUD}
        actual = {vm for vm in VoiceMode if vm.needs_cloud_stt_tts()}
        self.assertEqual(actual, expected)

    def test_needs_openrouter_key_currently_matches_cloud_stt_tts(self):
        for vm in VoiceMode:
            with self.subTest(mode=vm.name):
                self.assertEqual(vm.needs_openrouter_key(), vm.needs_cloud_stt_tts())

    def test_dragon_managed_pipeline_excludes_tc_and_onboard(self):
        # TinkerClaw bypasses to the gateway; Onboard is Tab5-side-only.
        # Dragon never sees mode 4 in practice.
        expected = {VoiceMode.LOCAL, VoiceMode.HYBRID, VoiceMode.CLOUD}
        actual = {vm for vm in VoiceMode if vm.is_dragon_managed_pipeline()}
        self.assertEqual(actual, expected)

    def test_tinkerclaw_is_not_dragon_managed(self):
        # Specific anchor — TC mode bypasses Dragon's STT/LLM/TTS chain.
        self.assertFalse(VoiceMode.TINKERCLAW.is_dragon_managed_pipeline())

    def test_onboard_is_not_dragon_managed(self):
        # Onboard is Tab5-side-only — Tab5 maps it to LOCAL on the wire.
        self.assertFalse(VoiceMode.ONBOARD.is_dragon_managed_pipeline())


class VoiceModeIntCompatTests(unittest.TestCase):
    """IntEnum compatibility — must JSON-serialise as plain int and
    round-trip through dict / NVS / DB writes that previously stored
    ``int(voice_mode)``."""

    def test_int_addition_works(self):
        # Sanity: IntEnum values usable in arithmetic contexts.
        self.assertEqual(VoiceMode.LOCAL + 0, 0)
        self.assertEqual(VoiceMode.CLOUD + 0, 2)

    def test_eq_int_works(self):
        # Critical: existing code paths that still use raw int
        # comparisons (DB writes, REST payloads) must keep working
        # during the migration.
        self.assertTrue(VoiceMode.LOCAL == 0)
        self.assertTrue(VoiceMode.CLOUD == 2)
        self.assertFalse(VoiceMode.LOCAL == 2)

    def test_in_int_tuple_works(self):
        # Pre-extract: voice_mode in (1, 2)
        # Post-extract: vm.needs_cloud_stt_tts() — but the raw form
        # still needs to work for places we haven't migrated yet.
        self.assertIn(VoiceMode.CLOUD, (1, 2))
        self.assertNotIn(VoiceMode.LOCAL, (1, 2))


if __name__ == "__main__":
    unittest.main()
