"""
Unit tests for build_feedpak_tuning.

Run with: python3 -m unittest discover tests
"""

import os
import sys
import unittest

# Make the project root importable when running from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import songsterr_to_feedpak as stf


def _tuning(strings: list[int]) -> stf.Tuning:
    return stf.Tuning(strings)


class BuildFeedpakTuningTest(unittest.TestCase):
    """build_feedpak_tuning returns semitone offsets from standard tuning."""

    def test_bass_standard_4_string(self):
        # G2 D2 A1 E1
        self.assertEqual(stf.build_feedpak_tuning(_tuning([43, 38, 33, 28]), True), [0, 0, 0, 0])

    def test_bass_drop_d_4_string(self):
        # G2 D2 A1 D1 (low E dropped to D)
        self.assertEqual(stf.build_feedpak_tuning(_tuning([43, 38, 33, 26]), True), [-2, 0, 0, 0])

    def test_bass_eb_standard_4_string(self):
        # Gb2 Db2 Ab1 Eb1
        self.assertEqual(stf.build_feedpak_tuning(_tuning([42, 37, 32, 27]), True), [-1, -1, -1, -1])

    def test_bass_standard_5_string(self):
        # G2 D2 A1 E1 B0
        self.assertEqual(stf.build_feedpak_tuning(_tuning([43, 38, 33, 28, 23]), True), [0, 0, 0, 0, 0])

    def test_bass_drop_a_5_string(self):
        # G2 D2 A1 E1 A0 (low B dropped to A)
        self.assertEqual(stf.build_feedpak_tuning(_tuning([43, 38, 33, 28, 21]), True), [-2, 0, 0, 0, 0])

    def test_bass_standard_6_string(self):
        # C3 G2 D2 A1 E1 B0
        self.assertEqual(stf.build_feedpak_tuning(_tuning([48, 43, 38, 33, 28, 23]), True), [0, 0, 0, 0, 0, 0])

    def test_guitar_standard_6_string(self):
        # E4 B3 G3 D3 A2 E2
        self.assertEqual(stf.build_feedpak_tuning(_tuning([64, 59, 55, 50, 45, 40]), False), [0, 0, 0, 0, 0, 0])

    def test_guitar_drop_d_6_string(self):
        # E4 B3 G3 D3 A2 D2
        self.assertEqual(stf.build_feedpak_tuning(_tuning([64, 59, 55, 50, 45, 38]), False), [-2, 0, 0, 0, 0, 0])

    def test_guitar_standard_7_string(self):
        # E4 B3 G3 D3 A2 E2 B1
        self.assertEqual(stf.build_feedpak_tuning(_tuning([64, 59, 55, 50, 45, 40, 35]), False), [0, 0, 0, 0, 0, 0, 0])

    def test_guitar_drop_a_7_string(self):
        # E4 B3 G3 D3 A2 E2 A1 (low B dropped to A)
        self.assertEqual(stf.build_feedpak_tuning(_tuning([64, 59, 55, 50, 45, 40, 33]), False), [-2, 0, 0, 0, 0, 0, 0])

    def test_guitar_standard_8_string(self):
        # E4 B3 G3 D3 A2 E2 B1 E1
        self.assertEqual(stf.build_feedpak_tuning(_tuning([64, 59, 55, 50, 45, 40, 35, 30]), False), [0, 0, 0, 0, 0, 0, 0, 0])

    def test_guitar_drop_e_8_string(self):
        # E4 B3 G3 D3 A2 E2 B1 D1 (low E dropped to D)
        self.assertEqual(stf.build_feedpak_tuning(_tuning([64, 59, 55, 50, 45, 40, 35, 28]), False), [-2, 0, 0, 0, 0, 0, 0, 0])


if __name__ == "__main__":
    unittest.main()
