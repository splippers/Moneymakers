"""Calibration checks tied to scoring_guidance.md §7 (paths optional if missing)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import projectscan as ps


def _repo(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / name


class TestBoreDoomMassMarket(unittest.TestCase):
    def test_segment_and_value_cap_with_zero_traction(self) -> None:
        p = _repo("BoreDOOM")
        if not (p / ".git").is_dir():
            self.skipTest("BoreDOOM checkout missing")

        with patch.object(ps, "_lookup_github_stars", return_value=0), patch.object(
            ps, "_npm_last_week_downloads", return_value=None
        ):
            r = ps.analyze_repo(p)

        self.assertEqual(r["market_segment"], "mass_market_computer_game")
        self.assertEqual(r["market_segment_label"], "Computer game for the masses")
        self.assertEqual(r["market_tag"], "GAME")
        self.assertLessEqual(r["scores"]["value"], 30)


class TestBrickwisePortfolio(unittest.TestCase):
    def test_total_score_exceeds_45_with_observed_stars_and_package(self) -> None:
        """§7 'value=45' matches portfolio weighted score here (not raw scores['value'])."""
        p = _repo("Brickwise")
        if not (p / ".git").is_dir():
            self.skipTest("Brickwise checkout missing")
        if not (p / "pyproject.toml").is_file():
            self.skipTest("Brickwise pyproject missing")

        r = ps.analyze_repo(p)
        self.assertTrue(r["has_package"])
        self.assertGreater(r["total_score"], 45.0)


if __name__ == "__main__":
    unittest.main()
