"""Tests for Meta Cursor heuristic schema v1.1 (Meta-Cursor-heuristic-algorythm-guidance.md)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import projectscan as ps


def _repo(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / name


class TestDemandV11(unittest.TestCase):
    def test_best_signal_stars_only_not_averaged_down(self) -> None:
        _, audit = ps.calc_demand_evidence_v11(
            {"stars": 60, "npm": None, "pypi": None, "waitlist": None}
        )
        self.assertAlmostEqual(float(audit["demand_combined_100"]), 35.707, places=2)
        self.assertEqual(audit["demand_source_used"], "stars")

    def test_best_signal_picks_stronger_npm(self) -> None:
        _, audit = ps.calc_demand_evidence_v11(
            {"stars": 60, "npm": 2_000_000, "pypi": None, "waitlist": None}
        )
        self.assertEqual(audit["demand_source_used"], "npm")


class TestRevenueV11(unittest.TestCase):
    def test_zero_demand_p50_cap(self) -> None:
        p10, p50, p90, asm = ps.estimate_revenue_band_monte_carlo(
            demand_pts=0, market_tag="DEVTOOL", manual_notes=""
        )
        self.assertLessEqual(p50, 5000)
        self.assertEqual(p10, 0)
        self.assertEqual(asm.get("note"), "no_traction_bands")


class TestInfraV11(unittest.TestCase):
    def test_infra_breakdown_sums_to_pts(self) -> None:
        pts, bd = ps.infra_breakdown_v11(
            has_package=True,
            has_api=True,
            has_docker=True,
            commit_count=100,
            has_readme=True,
            has_license=True,
        )
        self.assertEqual(pts, min(35, sum(bd.values())))


class TestRecencyV11(unittest.TestCase):
    def test_recency_halves_basePastThreshold(self) -> None:
        self.assertEqual(min(100, int(round(40 * 0.5))), 20)


class TestGameNotCapped(unittest.TestCase):
    def test_high_traction_game_can_exceed_legacy_cap(self) -> None:
        p = _repo("BoreDOOM")
        if not (p / ".git").is_dir():
            self.skipTest("BoreDOOM checkout missing")
        with patch.object(ps, "_lookup_github_stars", return_value=200_000):
            r = ps.analyze_repo(p)
        self.assertEqual(r["market_tag"], "GAME")
        self.assertGreater(r["scores"]["value"], 29)


class TestBoreDoomMassMarket(unittest.TestCase):
    def test_segment_and_no_market_pull_flag(self) -> None:
        p = _repo("BoreDOOM")
        if not (p / ".git").is_dir():
            self.skipTest("BoreDOOM checkout missing")

        with patch.object(ps, "_lookup_github_stars", return_value=0), patch.object(
            ps, "_npm_last_week_downloads", return_value=None
        ):
            r = ps.analyze_repo(p)

        self.assertEqual(r["market_segment"], "mass_market_computer_game")
        self.assertEqual(r["market_segment_label"], "Computer game for the masses")
        sb = r.get("score_breakdown") or {}
        self.assertIn("NO_MARKET_PULL", sb.get("risk_flags") or [])


class TestBrickwiseGtm(unittest.TestCase):
    def test_gtm_readiness_band(self) -> None:
        p = _repo("Brickwise")
        if not (p / ".git").is_dir():
            self.skipTest("Brickwise checkout missing")
        if not (p / "pyproject.toml").is_file():
            self.skipTest("Brickwise pyproject missing")

        r = ps.analyze_repo(p)
        self.assertTrue(r["has_package"])
        self.assertGreater(float(r.get("gtm_readiness") or r["total_score"]), 45.0)


if __name__ == "__main__":
    unittest.main()
