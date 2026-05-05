#!/usr/bin/env python3
"""
Scan a directory of git projects, score them heuristically, and optionally
serve a small local dashboard to prioritise and annotate what to work on next.

Defaults (no env): scan sibling folders of this repo's parent — i.e. with the
script at ``Projects/moneymakers/projectscan.py``, repos are ``Projects/*/`` and
the index is written to ``Projects/moneymakers/project_index/``.

Usage:
  python3 projectscan.py              # scan only
  python3 projectscan.py serve        # dashboard at http://127.0.0.1:8765
  PROJECTSCAN_ROOT=/path/to/projects python3 projectscan.py serve

Env overrides: ``PROJECTSCAN_ROOT``, ``PROJECTSCAN_INDEX_DIR``, ``PROJECTSCAN_PORT`` (serve),
``PROJECTSCAN_PUBLIC_ORIGIN`` (e.g. ``http://192.168.1.2`` — full URL for the Drive setup guide link when nginx serves the portal at a LAN address),
``PROJECTSCAN_EXTRA_ROOTS`` (comma-separated git repo paths to merge into the scan).
Optional: ``GITHUB_TOKEN`` raises GitHub API rate limits when resolving star counts for demand signals.

This checkout (the directory containing ``projectscan.py``) is **always** scanned when it has a ``.git`` folder,
so the Moneymakers / Projectscan repo appears in the dashboard even if ``PROJECTSCAN_ROOT`` points elsewhere.

Google Drive (optional): ``pip install -r requirements-google.txt``, OAuth Desktop JSON as
``client_secrets.json`` in ``project_index`` (or ``PROJECTSCAN_DRIVE_CLIENT_SECRETS``). Run
``python projectscan.py drive-auth`` once, then upload from CLI or dashboard.
Or skip OAuth: dashboard **Download report** (format + subset) or ``GET /api/report/download?format=txt&subset=all``.
Over SSH, use ``--oauth-port 9876`` (or ``$PROJECTSCAN_DRIVE_OAUTH_PORT``) and matching
``ssh -L 9876:127.0.0.1:9876``, then authorise in a browser on the machine where that tunnel runs.

Workspace + GCP step-by-step (URLs for mobile copy/paste): ``./drive_workspace_setup_wizard.py``.

Heuristic scoring changelog and audit expectations (demand curve, confidence tier, ``SCORE_BREAKDOWN``):
see ``Meta-Cursor-heuristic-algorythm-guidance.md`` (v1.1) and ``NOTES.md``.

Restrict which Google login may upload: ``PROJECTSCAN_DRIVE_ALLOWED_EMAILS=jon@splippers.com`` (comma-separated).
With an **Internal** OAuth consent screen (Google Workspace), only organisational accounts can consent; with
**External** apps in **Testing** mode, add Google’s **test users** list as well.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def projects_dir() -> Path:
    raw = os.environ.get("PROJECTSCAN_ROOT")
    if raw:
        return Path(raw).expanduser()
    # ``moneymakers`` (or any checkout) lives next to other project repos
    return _script_dir().parent


def index_dir() -> Path:
    raw = os.environ.get("PROJECTSCAN_INDEX_DIR")
    if raw:
        return Path(raw).expanduser()
    return _script_dir() / "project_index"


def index_file_json() -> Path:
    return index_dir() / "repos.json"


def index_file_csv() -> Path:
    return index_dir() / "repos.csv"


# Fields merged from previous JSON so annotations survive rescans
PERSISTED_FIELDS = (
    "manual_notes",
    "manual_value_override",
    "manual_money_low",
    "manual_money_high",
    "monetization_notes",
    "importance",
    "status",
    "hidden",
)

_FX_CACHE: dict[str, float | dict | None] = {"expires": 0.0, "payload": None}
# Free tier, no key — USD base conversion rates (multiply USD by rate to get local currency)
FX_URL = "https://open.er-api.com/v6/latest/USD"
# Offline / API-failure fallback — rough order of magnitude vs USD
FX_FALLBACK_RATES_FROM_USD: dict[str, float] = {
    "USD": 1.0,
    "EUR": 0.92,
    "GBP": 0.79,
    "JPY": 152.0,
    "CHF": 0.88,
    "CAD": 1.38,
    "AUD": 1.54,
    "NZD": 1.68,
    "INR": 83.0,
    "CNY": 7.25,
    "SEK": 10.6,
    "NOK": 10.8,
    "PLN": 4.0,
    "MXN": 17.5,
}

# Successful lookups only — avoids caching transient GitHub API failures.
_GITHUB_STARS_CACHE: dict[str, int] = {}


def run_git_command(repo_path: Path, cmd: list[str]) -> str:
    """Run git in repo; return stdout or empty string on failure."""
    try:
        result = subprocess.run(
            ["git"] + cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def estimate_money_usd(
    scores: dict[str, int],
    *,
    demand_evidence: int = 0,
    market_tag: str = "INTERNAL_TOOL",
    manual_notes: str = "",
) -> tuple[int, int]:
    """P10/P90 USD/year ARR band from heuristic Monte Carlo–style model (not financial advice)."""
    p10, _, p90, _ = estimate_revenue_band_monte_carlo(
        demand_pts=demand_evidence,
        market_tag=market_tag,
        manual_notes=manual_notes,
    )
    return p10, p90


def build_monetization(
    *,
    file_count: int,
    has_readme: bool,
    has_license: bool,
    has_package: bool,
    has_docker: bool,
    has_api: bool,
    value: int,
    market_tag: str = "INTERNAL_TOOL",
    demand_evidence: int = 0,
) -> dict:
    """Concrete 'how money could be made' lines derived from repo signals."""
    paths: list[dict[str, str]] = []
    tag = market_tag or "INTERNAL_TOOL"
    d = max(0, min(50, int(demand_evidence)))

    if tag == "GAME":
        paths.extend(
            [
                {
                    "title": "Storefront & discovery (mass market)",
                    "detail": "Quest / Horizon / App Lab positioning; trailers, ASO, and creator-led clips beat enterprise pilots — consumer AR wins on installs, retention, and seasonal beats.",
                    "model": "GAME / mass market AR",
                },
                {
                    "title": "In-app purchases & cosmetics",
                    "detail": "Battle passes, cosmetics, seasonal content — tune monetisation to D1/D7/D30 retention, not feature spreadsheets.",
                    "model": "GAME / IAP",
                },
                {
                    "title": "Ads & rewarded formats",
                    "detail": "Rewarded video / interstitials once sessions are long enough; hybrid IAP + ads is typical at consumer scale.",
                    "model": "GAME / ads",
                },
            ]
        )
    elif tag == "INTERNAL_TOOL":
        paths.append(
            {
                "title": "Productize for a narrow ICP",
                "detail": "Pick one external buyer persona, ship onboarding + billing; internal-only tools need deliberate productisation — code ≠ revenue.",
                "model": "INTERNAL → SaaS",
            }
        )

    if has_api and tag != "GAME":
        paths.append(
            {
                "title": "Usage-based API",
                "detail": "Meter requests or seats (Stripe + API keys). Fits anything with `api/`, `server/`, or `backend/`.",
                "model": "B2B SaaS / devtools",
            }
        )
    if has_package and not has_api and tag != "GAME":
        paths.append(
            {
                "title": "Library, CLI, or packaged tool",
                "detail": "Sell licenses, paid npm/pypi/crate tiers, or embed in commercial products (dual license).",
                "model": "License + support",
            }
        )
    if has_docker and tag != "GAME":
        paths.append(
            {
                "title": "Hosted or on-prem deployment",
                "detail": "Annual contracts for self-hosting, SLAs, upgrades — especially if Docker Compose is already wired.",
                "model": "Enterprise / infra",
            }
        )
    if has_readme and has_license:
        paths.append(
            {
                "title": "Open-core or sponsorship",
                "detail": "Credible OSS + sponsor tiers or paid \"cloud\" sibling; GitHub Sponsors / Open Collective complement product revenue.",
                "model": "Community + upsell",
            }
        )
    if file_count < 35:
        if tag == "GAME":
            paths.append(
                {
                    "title": "Playtests & soft launch",
                    "detail": "Store preview / sideload cohorts, Discord feedback, retention dashboards — prove fun and clarity before scaling paid UA.",
                    "model": "GAME / growth",
                }
            )
        else:
            paths.append(
                {
                    "title": "Early paid validation",
                    "detail": "Small codebase → ship a narrow paid MVP (lifetime deal, prepaid pilot) before broad features.",
                    "model": "Founder-led sales",
                }
            )
    if value >= 70 and (d >= 10 or tag == "B2B_SAAS"):
        paths.append(
            {
                "title": "Partnership / white-label",
                "detail": "Strong product + traction signals; bundle into a larger vendor SKU or a reseller channel.",
                "model": "Channel",
            }
        )
    if tag == "GAME":
        paths.append(
            {
                "title": "Community & UGC hooks",
                "detail": "Leaderboards, shareable clips, seasonal modes — mass-market AR sustains on novelty loops and social proof, not bespoke integrations.",
                "model": "GAME / community",
            }
        )
    else:
        paths.append(
            {
                "title": "Services & integrations",
                "detail": "Custom builds, onboarding, integrations — monetise depth while recurring product matures.",
                "model": "Consulting funnel",
            }
        )
    seen: set[str] = set()
    uniq: list[dict[str, str]] = []
    for p in paths:
        k = p["title"]
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    models = sorted({p.get("model", "") for p in uniq if p.get("model")})
    if tag == "GAME":
        _game_h_order = (
            "GAME / mass market AR",
            "GAME / IAP",
            "GAME / ads",
            "GAME / growth",
            "GAME / community",
        )

        def _game_headline_rank(m: str) -> tuple[int, str]:
            try:
                return (_game_h_order.index(m), m)
            except ValueError:
                return (len(_game_h_order), m)

        models_for_headline = sorted(models, key=_game_headline_rank)
    else:
        models_for_headline = models
    headline = ", ".join(models_for_headline[:4]) if models_for_headline else "Explore product-market fit first"
    return {
        "paths": uniq[:7],
        "models": models,
        "headline": headline,
    }


def pick_roi_distribution(
    *,
    file_count: int,
    has_readme: bool,
    has_license: bool,
    has_package: bool,
    has_docker: bool,
    has_api: bool,
    value: int,
    progress: int,
    feature_potential: int,
    effort_to_monetize: int,
    market_tag: str = "INTERNAL_TOOL",
    demand_evidence: int = 0,
) -> dict[str, str | float]:
    """Single distribution path estimated to maximise ROI for these repo signals."""
    ctx = {
        "fc": file_count,
        "readme": has_readme,
        "lic": has_license,
        "pkg": has_package,
        "dock": has_docker,
        "api": has_api,
        "v": value,
        "p": progress,
        "fp": feature_potential,
        "e": effort_to_monetize,
        "tag": market_tag or "INTERNAL_TOOL",
        "demand": max(0, min(50, int(demand_evidence))),
    }

    def plg_developer_score() -> float:
        s = 0.0
        if ctx["api"]:
            s += 44.0
        if ctx["pkg"]:
            s += 18.0
        if ctx["readme"]:
            s += 12.0
        if 38 <= ctx["p"] <= 88:
            s += 22.0
        if ctx["fc"] < 90:
            s += 10.0
        s += ctx["fp"] / 35.0
        if ctx["tag"] == "GAME":
            s *= 0.48
        elif ctx["tag"] == "DEVTOOL":
            s += 15.0
        if ctx["demand"] >= 20:
            s += 11.0
        elif ctx["demand"] == 0:
            s *= 0.86
        return s

    def enterprise_score() -> float:
        s = 0.0
        if ctx["dock"]:
            s += 32.0
        if ctx["p"] >= 52:
            s += 26.0
        if ctx["fc"] >= 35:
            s += 14.0
        if ctx["v"] >= 62:
            s += 22.0
        if ctx["e"] >= 62:
            s += 14.0
        if ctx["tag"] == "GAME":
            s *= 0.20
        elif ctx["tag"] == "B2B_SAAS":
            s += 17.0
        elif ctx["tag"] == "INTERNAL_TOOL":
            s *= 0.80
        if ctx["demand"] == 0:
            s *= 0.88
        return s

    def oss_flywheel_score() -> float:
        s = 0.0
        if ctx["readme"] and ctx["lic"]:
            s += 40.0
        if ctx["pkg"]:
            s += 14.0
        if ctx["dock"]:
            s += 12.0
        if ctx["fp"] >= 58:
            s += 14.0
        if not ctx["api"]:
            s += 6.0
        if ctx["tag"] == "DEVTOOL":
            s += 17.0
        if ctx["tag"] == "GAME":
            s += 8.0
        if ctx["demand"] >= 15:
            s += 8.0
        return s

    def founder_direct_score() -> float:
        s = 0.0
        if ctx["fc"] < 45:
            s += 36.0
        if ctx["p"] < 55:
            s += 28.0
        s += max(0.0, 26.0 - ctx["v"] / 5.0)
        if ctx["readme"]:
            s += 8.0
        if ctx["tag"] == "GAME":
            s += 22.0
        elif ctx["tag"] == "INTERNAL_TOOL":
            s += 19.0
        if ctx["demand"] == 0:
            s += 6.0
        return s

    def partner_channel_score() -> float:
        s = 0.0
        if ctx["api"]:
            s += 24.0
        if ctx["v"] >= 72:
            s += 26.0
        if ctx["p"] >= 58:
            s += 22.0
        if ctx["pkg"]:
            s += 12.0
        if ctx["tag"] == "GAME":
            s *= 0.52
        elif ctx["tag"] == "B2B_SAAS":
            s += 13.0
        elif ctx["tag"] == "INTERNAL_TOOL":
            s *= 0.84
        if ctx["demand"] >= 18:
            s += 14.0
        elif ctx["demand"] == 0:
            s *= 0.76
        return s

    playbooks = [
        {
            "id": "plg_developer",
            "score": plg_developer_score(),
            "pathway_title": "Product-led growth via developers",
            "distribution_method": (
                "Free tier + Stripe usage limits, public docs, SEO and GitHub/README flywheel; "
                "list on Zapier/Make or stack-specific marketplaces; expand via API keys."
            ),
            "why_roi": (
                "Scales revenue without proportional headcount once metering and onboarding are wired — "
                "best when the product exposes an API or package boundary."
            ),
        },
        {
            "id": "enterprise_direct",
            "score": enterprise_score(),
            "pathway_title": "Enterprise direct + proofs (Docker / on-prem)",
            "distribution_method": (
                "Targeted outbound to teams that self-host (security, infra, compliance leads); pilots with "
                "Docker/Compose artefacts; ROI case studies; annual commits over seat trials."
            ),
            "why_roi": (
                "Fewer-but-larger wins fit deployable artefacts and higher build maturity — recovers founder "
                "time versus broad PLG campaigns."
            ),
        },
        {
            "id": "oss_cloud_upsell",
            "score": oss_flywheel_score(),
            "pathway_title": "Open-source distribution → managed upsell",
            "distribution_method": (
                "Community GitHub/GitLab presence, OSS license clarity, roadmap transparency; monetise hosted "
                "cloud, SSO, quotas, sponsors, and SLAs—not just stars."
            ),
            "why_roi": (
                "Earns trusted distribution cheaply via contributors and issues; recurring margin lives in "
                "managed tier once README and licence signal seriousness."
            ),
        },
        {
            "id": "founder_concierge",
            "score": founder_direct_score(),
            "pathway_title": "Founder-led concierge GTM",
            "distribution_method": (
                "10–30 design-partner conversations, manual onboarding, invoiced pilots before automation; referrals "
                "from adjacent tools; postpone paid ads."
            ),
            "why_roi": (
                "Maximises learning-per-pound while the codebase is still small — avoids burning budget on funnel "
                "that the product cannot yet convert."
            ),
        },
        {
            "id": "partner_integration",
            "score": partner_channel_score(),
            "pathway_title": "Partner & integration wedge",
            "distribution_method": (
                "Build one killer integration into a hub (Slack/O365/CRM/cloud); pursue co-selling, MDF, reseller "
                "and marketplace placement where your API is the SKU."
            ),
            "why_roi": (
                "Borrowed distribution slashes CAC when you are differentiated enough to integrate into incumbents; "
                "works when maturity and positioning scores are higher."
            ),
        },
    ]

    best = max(playbooks, key=lambda b: float(b["score"]))
    ordered_pb = sorted(playbooks, key=lambda b: float(b["score"]), reverse=True)
    alts = [p["id"] for p in ordered_pb[1:3]]

    return {
        "playbook_id": str(best["id"]),
        "pathway_title": str(best["pathway_title"]),
        "distribution_method": str(best["distribution_method"]),
        "why_roi": str(best["why_roi"]),
        "alternative_ids": ",".join(alts),
    }


def _readme_preview(repo_path: Path) -> str:
    for fname in ("README.md", "readme.md"):
        p = repo_path / fname
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8", errors="ignore")[:26000]
            except OSError:
                break
    return ""


def _tracked_paths_blob(repo_path: Path) -> str:
    ls = run_git_command(repo_path, ["ls-files"])
    return "\n".join(ls.splitlines()).lower()


def _git_grep_regex(repo_path: Path, pattern: str) -> bool:
    cmd = ["git", "-C", str(repo_path), "grep", "-l", "-I", "-E", pattern, "--"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=18)
        return r.returncode == 0 and bool((r.stdout or "").strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _parse_github_owner_repo(remote_line: str) -> tuple[str, str] | None:
    u = remote_line.strip()
    if not u:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s#]+)", u)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    return (owner, repo) if owner and repo else None


def _lookup_github_stars(repo_path: Path) -> int | None:
    remote = run_git_command(repo_path, ["remote", "get-url", "origin"])
    coord = _parse_github_owner_repo(remote)
    if not coord:
        return None
    owner, repo = coord
    key = f"{owner.lower()}/{repo.lower()}"
    if key in _GITHUB_STARS_CACHE:
        return _GITHUB_STARS_CACHE[key]
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "projectscan-moneymakers",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=14) as resp:
            payload = json.loads(resp.read().decode())
        stars = int(payload.get("stargazers_count", 0))
        _GITHUB_STARS_CACHE[key] = stars
        return stars
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _npm_last_week_downloads(repo_path: Path) -> int | None:
    pj = repo_path / "package.json"
    if not pj.is_file():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw_name = data.get("name")
    if not isinstance(raw_name, str):
        return None
    pkg = raw_name.strip()
    if not pkg:
        return None
    enc = quote(pkg, safe="@/")
    url = f"https://api.npmjs.org/downloads/point/last-week/{enc}"
    req = urllib.request.Request(url, headers={"User-Agent": "projectscan-moneymakers"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = json.loads(resp.read().decode())
        n = body.get("downloads")
        return int(n) if isinstance(n, int) else None
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _signal_norm_for_demand(count: int) -> float:
    """Log-scaled 0–100 norm for a single traction count (Meta Cursor heuristic v1.1)."""
    return min(100.0, 20.0 * math.log10(max(int(count), 0) + 1))


def calc_demand_evidence_v11(signals: dict[str, int | None]) -> tuple[int, dict]:
    """Demand 0–50 from the strongest available signal only (no averaging with missing npm/PyPI legs)."""
    available: dict[str, int] = {}
    for key in ("stars", "npm", "pypi", "waitlist"):
        v = signals.get(key)
        if v is not None:
            available[key] = int(v)
    src_line = {k: signals.get(k) for k in ("stars", "npm", "pypi", "waitlist")}
    if not available:
        return 0, {
            "sources": src_line,
            "demand_source_used": None,
            "demand_combined_100": 0.0,
            "formula": "demand_pts=min(50,round(combined_100/100*50)); combined=max(norm(signals present))",
        }
    norms = [(k, _signal_norm_for_demand(cnt)) for k, cnt in available.items()]
    best_k, combined = max(norms, key=lambda x: x[1])
    pts = min(50, max(0, int(round(combined / 100.0 * 50))))
    return pts, {
        "sources": src_line,
        "demand_source_used": best_k,
        "demand_combined_100": round(combined, 3),
        "formula": "demand_pts=min(50,round(combined_100/100*50)); combined=max(norm per signal)",
    }


def _waitlist_count_from_notes(manual_notes: str) -> int | None:
    m = re.search(r"waitlist\s*[=:]\s*(\d+)", manual_notes, flags=re.I)
    return int(m.group(1)) if m else None


def _pypi_monthly_downloads(repo_path: Path) -> int | None:
    """Reserved for PyPI proxy; return None until a stable API path is wired."""
    _ = repo_path
    return None


def infra_breakdown_v11(
    *,
    has_package: bool,
    has_api: bool,
    has_docker: bool,
    commit_count: int,
    has_readme: bool,
    has_license: bool,
) -> tuple[int, dict[str, int]]:
    breakdown = {
        "package": 10 if has_package else 0,
        "api_layout": 10 if has_api else 0,
        "docker": 5 if has_docker else 0,
        "commits_gt_50": 5 if commit_count >= 50 else 0,
        "readme_licence": 5 if (has_readme and has_license) else (3 if has_readme else 0),
    }
    total = min(35, sum(breakdown.values()))
    return total, breakdown


def _github_pain_issue_points(repo_path: Path) -> int:
    _ = repo_path
    return 0


def problem_evidence_v11(manual_notes: str, repo_path: Path) -> tuple[int, dict[str, int]]:
    pts = 0
    sources = {"manual_notes": 0, "github_issues_tagged_pain": 0, "external_mentions": 0}
    low = manual_notes.lower()
    if re.search(r"waitlist|beta users|design partners", low):
        pts += 15
        sources["manual_notes"] += 15
    if re.search(r"loi|letter of intent|prepaid|pilot\s*\$?\d", low, re.I):
        pts += 25
        sources["manual_notes"] += 25
    gh = _github_pain_issue_points(repo_path)
    sources["github_issues_tagged_pain"] = gh
    pts += gh
    ext = 0
    sources["external_mentions"] = ext
    if ext > 5:
        pts += 10
    return min(60, pts), sources


def _readme_licence_points(has_readme: bool, has_license: bool) -> int:
    return 5 if (has_readme and has_license) else (3 if has_readme else 0)


def confidence_v11(
    *,
    demand_source_used: str | None,
    readme_licence_pts: int,
    risk_flags: list[str],
) -> str:
    if "ABANDONMENT_RISK" in risk_flags or "HIT_DRIVEN_VOLATILITY" in risk_flags:
        return "Low"
    if demand_source_used is None and readme_licence_pts < 5:
        return "Low"
    if demand_source_used is not None and readme_licence_pts >= 5:
        return "High"
    if demand_source_used is not None or readme_licence_pts >= 5:
        return "Med"
    return "Low"


def collect_risk_flags_v11(
    *,
    market_tag: str,
    demand_pts: int,
    days_since_commit: int | None,
) -> list[str]:
    flags: list[str] = []
    if market_tag == "GAME" and demand_pts < 20:
        flags.append("HIT_DRIVEN_VOLATILITY")
    if demand_pts == 0:
        flags.append("NO_MARKET_PULL")
    if days_since_commit is not None and days_since_commit > 90:
        flags.append("ABANDONMENT_RISK")
    return flags


def gtm_readiness_for(effort_to_monetize: int, progress: int) -> float:
    return round(0.7 * float(effort_to_monetize) + 0.3 * float(progress), 1)


def estimate_revenue_band_monte_carlo(
    *,
    demand_pts: int,
    market_tag: str,
    manual_notes: str,
) -> tuple[int, int, int, dict]:
    """Heuristic P10/P50/P90 annual USD ARR with logged assumptions (Monte Carlo–style spread)."""
    d = max(0, min(50, int(demand_pts)))
    tag = market_tag or "INTERNAL_TOOL"
    conversion = {"DEVTOOL": 5, "GAME": 6, "B2B_SAAS": 4, "INTERNAL_TOOL": 2}.get(tag, 3)
    base_acv = {"DEVTOOL": 1200, "GAME": 800, "B2B_SAAS": 2400, "INTERNAL_TOOL": 600}.get(tag, 900)

    loi_m = re.search(r"(?:LOI|letter of intent)\s*[=:]?\s*\$?\s*([\d,]+)", manual_notes, re.I)
    if loi_m:
        try:
            loi_val = int(loi_m.group(1).replace(",", ""))
            base_acv = max(base_acv, max(500, loi_val // 10))
        except ValueError:
            pass

    if d == 0:
        assumptions = {
            "market_tag": tag,
            "demand_pts": d,
            "note": "no_traction_bands",
            "p50_anchor_usd": 2500,
        }
        return 0, 2500, 10_000, assumptions

    payers_p50 = max(1, d * conversion)
    arr_p50 = int(base_acv * payers_p50)
    arr_p10 = int(arr_p50 * 0.6)
    arr_p90 = int(arr_p50 * 1.5)
    assumptions = {
        "market_tag": tag,
        "demand_pts": d,
        "base_acv_usd_per_year": base_acv,
        "conversion_factor": conversion,
        "payers_p50": payers_p50,
        "churn_annual": 0.2,
        "method": "heuristic_p10_p50_p90_spread",
    }
    return arr_p10, arr_p50, arr_p90, assumptions


def _days_since_commit(repo_path: Path) -> int | None:
    raw = run_git_command(repo_path, ["log", "-1", "--format=%ct"])
    if raw.isdigit():
        return max(0, int((time.time() - int(raw)) / 86400))
    return None


def populate_repo_scoring(repo: dict) -> None:
    """Apply Meta Cursor heuristic schema v1.1 — mutates repo (see Meta-Cursor-heuristic-algorythm-guidance.md)."""
    path_s = repo.get("path")
    if not path_s or not isinstance(path_s, str):
        return
    rp = Path(path_s)
    if not rp.is_dir():
        return

    manual_notes = str(repo.get("manual_notes") or "")
    monetization_notes = str(repo.get("monetization_notes") or "")

    if (rp / ".git").is_dir():
        if "signal_github_stars" not in repo:
            repo["signal_github_stars"] = _lookup_github_stars(rp)
        if "signal_npm_weekly" not in repo:
            repo["signal_npm_weekly"] = _npm_last_week_downloads(rp)
        if "signal_pypi_monthly" not in repo:
            repo["signal_pypi_monthly"] = _pypi_monthly_downloads(rp)
        if "days_since_commit" not in repo:
            repo["days_since_commit"] = _days_since_commit(rp)

    stars = repo.get("signal_github_stars")
    npm = repo.get("signal_npm_weekly")
    pypi = repo.get("signal_pypi_monthly")
    waitlist = _waitlist_count_from_notes(manual_notes)

    signals: dict[str, int | None] = {
        "stars": stars if stars is None else int(stars),
        "npm": npm if npm is None else int(npm),
        "pypi": pypi if pypi is None else int(pypi),
        "waitlist": waitlist,
    }
    demand_pts, demand_audit = calc_demand_evidence_v11(signals)

    has_package = bool(repo.get("has_package"))
    has_api = bool(repo.get("has_api"))
    has_docker = bool(repo.get("has_docker"))
    has_readme = bool(repo.get("has_readme"))
    has_license = bool(repo.get("has_license"))
    commit_count = int(repo.get("commit_count") or 0)
    market_tag = str(repo.get("market_tag") or "INTERNAL_TOOL")

    infra_pts, infra_break = infra_breakdown_v11(
        has_package=has_package,
        has_api=has_api,
        has_docker=has_docker,
        commit_count=commit_count,
        has_readme=has_readme,
        has_license=has_license,
    )
    problem_pts, problem_src = problem_evidence_v11(manual_notes, rp)
    readme_lic_pts = _readme_licence_points(has_readme, has_license)

    base_value = min(100, demand_pts + infra_pts + problem_pts)

    days_since = repo.get("days_since_commit")
    if not isinstance(days_since, int):
        days_since = None

    risk_flags = collect_risk_flags_v11(
        market_tag=market_tag,
        demand_pts=demand_pts,
        days_since_commit=days_since,
    )
    recency_mult = 0.5 if (days_since is not None and days_since > 90) else 1.0
    recency_reason = f"last_commit_days={days_since if days_since is not None else 'unknown'}, threshold=90"

    final_value = min(100, int(round(base_value * recency_mult)))

    conf = confidence_v11(
        demand_source_used=demand_audit.get("demand_source_used"),
        readme_licence_pts=readme_lic_pts,
        risk_flags=risk_flags,
    )
    if demand_pts == 0:
        conf = "Low"
    if market_tag == "GAME" and demand_pts < 20:
        conf = "Low"

    paths_lower = _tracked_paths_blob(rp)
    progress = min(100, commit_count * 2)
    if has_package:
        progress += 10
    if has_docker:
        progress += 10
    progress = min(100, progress)

    effort_to_monetize = _effort_to_monetize_score(
        market_tag=market_tag,
        repo_path=rp,
        paths_lower=paths_lower,
        has_readme=has_readme,
        has_license=has_license,
        has_package=has_package,
        has_docker=has_docker,
        progress=progress,
    )

    feature_potential = 20
    sc_old = repo.get("scores")
    if isinstance(sc_old, dict) and "feature_potential" in sc_old:
        try:
            feature_potential = max(0, min(100, int(sc_old["feature_potential"])))
        except (TypeError, ValueError):
            feature_potential = 20

    repo["scores"] = {
        "value": final_value,
        "progress": progress,
        "feature_potential": feature_potential,
        "effort_to_monetize": effort_to_monetize,
    }
    repo["demand_evidence"] = demand_pts
    repo["scoring_confidence"] = conf

    sb: dict = {
        "schema": "1.1",
        "demand_pts": demand_pts,
        "demand": {
            "sources": demand_audit["sources"],
            "demand_source_used": demand_audit["demand_source_used"],
            "demand_combined_100": demand_audit["demand_combined_100"],
            "formula": demand_audit["formula"],
        },
        "infra_pts": infra_pts,
        "infra": {
            "breakdown": infra_break,
            "formula": "infra_pts = min(35, sum(breakdown))",
        },
        "base_value": base_value,
        "recency_multiplier": recency_mult,
        "recency": {"reason": recency_reason},
        "final_value": final_value,
        "formula_final": "final_value = min(100, round(base_value * recency_multiplier))",
        "risk_flags": risk_flags,
        "confidence": conf,
    }
    if problem_pts > 0:
        sb["problem_evidence_pts"] = problem_pts
        sb["problem_evidence"] = {
            "sources": problem_src,
            "formula": "manual_notes regex + GitHub pain issues (stub) + external mentions (stub)",
        }
    repo["score_breakdown"] = sb

    ra_mult = 0.5 if "ABANDONMENT_RISK" in risk_flags else 1.0
    repo["risk_adjusted_value"] = round(float(final_value) * ra_mult, 3)

    repo["data_gap"] = (
        "DATA_GAP: No GTM hypothesis"
        if (not manual_notes.strip() and not monetization_notes.strip())
        else ""
    )

    gtm = gtm_readiness_for(effort_to_monetize, progress)
    repo["gtm_readiness"] = gtm
    repo["total_score"] = gtm

    demand_hint_parts: list[str] = []
    if stars is not None:
        demand_hint_parts.append(f"GitHub ★ {stars}")
    else:
        demand_hint_parts.append("GitHub stars unknown (private host or API miss)")
    if npm is not None:
        demand_hint_parts.append(f"npm last-week ≈ {npm}")
    if waitlist is not None:
        demand_hint_parts.append(f"manual waitlist ≈ {waitlist}")
    repo["demand_hint"] = "Demand signals: " + " · ".join(demand_hint_parts)


def _payment_code_present(repo_path: Path) -> bool:
    return _git_grep_regex(
        repo_path,
        r"Stripe|stripe\.com|Paddle|LemonSqueezy|lemonsqueezy|checkout\.sessions|BillingPortal",
    )


def _has_auth_signals(paths_lower: str, repo_path: Path) -> bool:
    needles = (
        "/auth/",
        "/oauth/",
        "/sso/",
        "/saml/",
        "auth.ts",
        "auth.tsx",
        "middleware.ts",
        "clerk",
        "auth0",
        "next-auth",
        "better-auth",
        "/supabase/",
    )
    if any(n in paths_lower for n in needles):
        return True
    return _git_grep_regex(
        repo_path,
        r"ClerkProvider|NextAuth|Auth0|better-auth|firebase-admin|passport\.authenticate",
    )


def _has_pricing_surface(paths_lower: str) -> bool:
    return bool(
        "pricing.md" in paths_lower
        or "/pricing/" in paths_lower
        or "/pricing'" in paths_lower
        or '/pricing"' in paths_lower
        or "routes/pricing" in paths_lower
        or "pages/pricing" in paths_lower
        or "app/pricing" in paths_lower
    )


def _game_monetization_signals(repo_path: Path) -> bool:
    return _git_grep_regex(
        repo_path,
        r"in-app purchase|in_app_purchase|AdMob|admob|UnityAds|unity.?ads|\biap\b|battle pass|cosmetic shop",
    )


def _signals_consumer_game_home_ar(name_lower: str, readme_lower: str, repo_path: Path) -> bool:
    """Detect Quest/home AR/mass-market game scaffolding — never silently funnel these into B2B SaaS.

    Uses README intent plus lightweight Unity/XR layout cues (Meta guidance: GAME beats speculative enterprise).
    """
    rl = readme_lower
    blob = f"{name_lower}\n{rl}"

    if re.search(r"\b(boredoom|chorewars|chore wars)\b", blob.replace("!", "").replace("'", "")):
        return True

    # Explicit consumer AR game positioning (README-first — folder names alone mislead).
    if "augmented reality" in rl and re.search(r"\bgame\b|\bar\b|\bxr\b", rl):
        return True
    if re.search(r"\bar\s+chore\s+game\b|\b100%\s*ar\b.*\bgame\b|\bgame\b.*\b(meta\s+)?quest\b", rl, re.DOTALL):
        return True
    if "passthrough" in rl and re.search(r"\bgame\b|\bar\b", rl):
        return True
    if re.search(r"\b(mass[- ]market|for everyone|consumer\s+(?:vr|ar|title)|home\s+users)\b", rl):
        if re.search(r"\bgame\b|\bar\b|\bquest\b|\bxr\b", rl):
            return True

    if re.search(r"\bcomputer\s+game\b", rl) and re.search(
        r"\b(mass|masses|everyone|consumer|players?\b|home\s+users?\b)\b",
        rl,
    ):
        return True

    game_voice = bool(
        re.search(r"\b(ar game|vr game|xr game|chore game|video game|players?\b|multiplayer\b)", rl)
    )
    mass_consumer_ar = bool(
        re.search(
            r"\b(meta quest|quest\s*[23]|quest pro|passthrough ar|home ar|living\s+room ar)\b",
            rl,
        )
    )
    if mass_consumer_ar and game_voice:
        return True
    if "openxr" in rl and game_voice:
        return True
    if re.search(r"\bpokemon go\b", rl):
        return True

    unity_consumer_shell = (
        (repo_path / "Packages" / "manifest.json").is_file()
        and (repo_path / "Assets").is_dir()
    )
    if unity_consumer_shell and mass_consumer_ar and (
        re.search(r"\b(score|scoring|streak|unity)\b", rl) or re.search(r"\bchores?\b", rl)
    ):
        return True

    return False


def _infer_market_tag(
    repo_path: Path,
    name_lower: str,
    paths_lower: str,
    readme_lower: str,
    *,
    has_package: bool,
    has_api: bool,
) -> str:
    if re.search(r"\b(game|play|chore|fun)\b", name_lower):
        return "GAME"
    if name_lower == "ar" or name_lower.startswith("ar-"):
        return "GAME"
    if _signals_consumer_game_home_ar(name_lower, readme_lower, repo_path):
        return "GAME"
    if "/enterprise/" in paths_lower or re.search(r"\b(sso|saml|scim)\b", paths_lower):
        return "B2B_SAAS"
    if _has_pricing_surface(paths_lower) and (
        has_api
        or "tenant" in paths_lower
        or "organisation" in paths_lower
        or "organization" in paths_lower
    ):
        return "B2B_SAAS"
    if has_package and (has_api or "sdk" in paths_lower or "/cli/" in paths_lower or "/cmd/" in paths_lower):
        return "DEVTOOL"
    if has_package:
        return "DEVTOOL"
    if has_api:
        return "B2B_SAAS"
    return "INTERNAL_TOOL"


def _market_segment_fields(
    market_tag: str,
    name_lower: str,
    readme_lower: str,
    repo_path: Path,
) -> tuple[str, str]:
    """Audience lane for reporting — BoreDOOM-style titles → mass-market computer game."""
    if market_tag != "GAME":
        return ("na", "")
    if _signals_consumer_game_home_ar(name_lower, readme_lower, repo_path):
        return ("mass_market_computer_game", "Computer game for the masses")
    return ("computer_game", "Computer game")


def _effort_to_monetize_score(
    *,
    market_tag: str,
    repo_path: Path,
    paths_lower: str,
    has_readme: bool,
    has_license: bool,
    has_package: bool,
    has_docker: bool,
    progress: int,
) -> int:
    pay = _payment_code_present(repo_path)
    auth = _has_auth_signals(paths_lower, repo_path)
    price = _has_pricing_surface(paths_lower)
    ease = 28
    if pay and price and auth:
        ease = max(ease, 98)
    elif pay and price:
        ease = max(ease, 88)
    elif pay and auth:
        ease = max(ease, 82)
    elif pay:
        ease = max(ease, 74)
    elif price:
        ease = max(ease, 62)
    elif has_readme and has_license and has_package:
        ease = max(ease, 52)
    elif has_readme and has_package:
        ease = max(ease, 46)
    elif has_readme:
        ease = max(ease, 38)
    ease += min(12, progress // 10)
    if has_docker:
        ease += 4
    if market_tag == "GAME":
        if _game_monetization_signals(repo_path):
            ease = max(ease, 56)
        else:
            ease = min(ease, 32)
    return max(10, min(100, ease))


def analyze_repo(repo_path: Path) -> dict:
    """Extract metrics and heuristic scores for a single repo.

    Schema **v1.1** — fully auditable ``score_breakdown``, demand = max(log norms),
    ``total_score`` is **gtm_readiness** only (see ``Meta-Cursor-heuristic-algorythm-guidance.md``).
    """
    name = repo_path.name
    name_lower = name.lower()

    last_commit = run_git_command(repo_path, ["log", "-1", "--format=%cr"])
    commit_raw = run_git_command(repo_path, ["rev-list", "--count", "HEAD"])
    try:
        commit_count = int(commit_raw) if commit_raw else 0
    except ValueError:
        commit_count = 0

    ls = run_git_command(repo_path, ["ls-files"])
    file_count = len(ls.splitlines()) if ls else 0
    paths_lower = _tracked_paths_blob(repo_path)
    readme_raw = _readme_preview(repo_path)
    readme_lower = readme_raw.lower()

    has_readme = bool(readme_raw.strip()) or (repo_path / "readme.md").exists()
    has_license = any((repo_path / f).exists() for f in ("LICENSE", "LICENSE.md"))
    has_package = any(
        (repo_path / f).exists() for f in ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")
    )
    has_docker = (repo_path / "Dockerfile").exists() or (repo_path / "docker-compose.yml").exists()
    has_api = any((repo_path / d).is_dir() for d in ("api", "server", "backend"))

    market_tag = _infer_market_tag(
        repo_path,
        name_lower,
        paths_lower,
        readme_lower,
        has_package=has_package,
        has_api=has_api,
    )

    segment_slug, segment_label = _market_segment_fields(
        market_tag, name_lower, readme_lower, repo_path
    )

    repo = {
        "name": name,
        "path": str(repo_path.resolve()),
        "market_tag": market_tag,
        "market_segment": segment_slug,
        "market_segment_label": segment_label,
        "last_commit": last_commit,
        "commit_count": commit_count,
        "file_count": file_count,
        "has_readme": has_readme,
        "has_license": has_license,
        "has_package": has_package,
        "has_api": has_api,
        "has_docker": has_docker,
        "manual_notes": "",
        "manual_value_override": None,
        "manual_money_low": None,
        "manual_money_high": None,
        "monetization_notes": "",
        "importance": 3,
        "status": "active",
        "hidden": False,
        "last_updated": datetime.now().isoformat(timespec="seconds"),
    }

    populate_repo_scoring(repo)
    refresh_monetization_from_repo(repo)
    refresh_money_usd(repo)

    return repo


def refresh_monetization_from_repo(repo: dict) -> None:
    """Rebuild how-to-monetise copy from stored repo flags (after score overrides)."""
    scores = repo.get("scores")
    if not isinstance(scores, dict):
        return
    tag = str(repo.get("market_tag") or "INTERNAL_TOOL")
    dem = int(repo.get("demand_evidence") or 0)
    repo["monetization"] = build_monetization(
        file_count=int(repo.get("file_count") or 0),
        has_readme=bool(repo.get("has_readme")),
        has_license=bool(repo.get("has_license")),
        has_package=bool(repo.get("has_package")),
        has_docker=bool(repo.get("has_docker")),
        has_api=bool(repo.get("has_api")),
        value=int(scores.get("value") or 0),
        market_tag=tag,
        demand_evidence=dem,
    )
    repo["roi_distribution"] = pick_roi_distribution(
        file_count=int(repo.get("file_count") or 0),
        has_readme=bool(repo.get("has_readme")),
        has_license=bool(repo.get("has_license")),
        has_package=bool(repo.get("has_package")),
        has_docker=bool(repo.get("has_docker")),
        has_api=bool(repo.get("has_api")),
        value=int(scores.get("value") or 0),
        progress=int(scores.get("progress") or 0),
        feature_potential=int(scores.get("feature_potential") or 0),
        effort_to_monetize=int(scores.get("effort_to_monetize") or 0),
        market_tag=tag,
        demand_evidence=dem,
    )


def refresh_money_usd(repo: dict) -> None:
    """Set money_usd_low/high (P10/P90), midpoint P50, and revenue assumptions."""
    scores = repo.get("scores")
    if not isinstance(scores, dict):
        return
    tag = str(repo.get("market_tag") or "INTERNAL_TOOL")
    dem = int(repo.get("demand_evidence") or 0)
    mn = str(repo.get("manual_notes") or "")
    ml, mh = repo.get("manual_money_low"), repo.get("manual_money_high")
    if ml is not None and mh is not None:
        try:
            low = max(0, int(ml))
            high = max(low, int(mh))
            repo["money_usd_low"] = low
            repo["money_usd_high"] = high
            repo["money_usd_mid"] = int(round((low + high) / 2))
            repo["revenue_assumptions"] = {"note": "manual_band_override"}
            return
        except (TypeError, ValueError):
            repo["manual_money_low"] = None
            repo["manual_money_high"] = None
    p10, p50, p90, asm = estimate_revenue_band_monte_carlo(
        demand_pts=dem,
        market_tag=tag,
        manual_notes=mn,
    )
    repo["money_usd_low"], repo["money_usd_mid"], repo["money_usd_high"] = p10, p50, p90
    repo["revenue_assumptions"] = asm


def get_fx_payload() -> dict:
    """Cached USD-based FX rates via open.er-api.com, with static fallback."""
    now = time.time()
    cached = _FX_CACHE.get("payload")
    exp = float(_FX_CACHE.get("expires") or 0.0)
    if isinstance(cached, dict) and now < exp:
        return cached
    out: dict[str, str | dict[str, float]]
    try:
        req = urllib.request.Request(
            FX_URL,
            headers={
                "User-Agent": "projectscan/1 (+https://github.com/)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=16) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("result") != "success":
            raise ValueError("FX API result not success")
        rates_raw = data.get("rates")
        if isinstance(rates_raw, dict):
            rates: dict[str, float] = {}
            for k, v in rates_raw.items():
                try:
                    rates[str(k)] = float(v)
                except (TypeError, ValueError):
                    pass
        else:
            rates = {}
        rates["USD"] = 1.0
        out = {
            "base": "USD",
            "rates": rates,
            "date": str(data.get("time_last_update_utc") or data.get("time_next_update_utc") or ""),
            "source": "open.er-api.com",
        }
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ValueError, TypeError):
        out = {
            "base": "USD",
            "rates": {k: float(v) for k, v in FX_FALLBACK_RATES_FROM_USD.items()},
            "date": "",
            "source": "static-fallback",
        }
    _FX_CACHE["payload"] = out
    _FX_CACHE["expires"] = now + 3600
    return out


def merge_persisted(repo: dict, old: dict | None) -> None:
    if old is not None:
        for key in PERSISTED_FIELDS:
            if key in old:
                repo[key] = old[key]
    populate_repo_scoring(repo)
    flags = list((repo.get("score_breakdown") or {}).get("risk_flags") or [])
    ra_mult = 0.5 if "ABANDONMENT_RISK" in flags else 1.0
    override = repo.get("manual_value_override")
    if override is not None:
        try:
            v = int(override)
            v = max(0, min(100, v))
            repo["scores"]["value"] = v
            sb = repo.get("score_breakdown")
            if isinstance(sb, dict):
                sb["final_value"] = v
        except (TypeError, ValueError):
            repo["manual_value_override"] = None
    repo["risk_adjusted_value"] = round(float((repo.get("scores") or {}).get("value") or 0) * ra_mult, 3)
    sc = repo.get("scores") or {}
    ef = int(sc.get("effort_to_monetize") or 0)
    prog = int(sc.get("progress") or 0)
    repo["gtm_readiness"] = gtm_readiness_for(ef, prog)
    repo["total_score"] = repo["gtm_readiness"]
    refresh_monetization_from_repo(repo)
    refresh_money_usd(repo)


def sort_key(repo: dict) -> tuple:
    """Hidden last; importance; heuristic upside ``scores.value``; confidence tier; name."""
    imp = repo.get("importance")
    try:
        imp_n = int(imp) if imp is not None else 3
    except (TypeError, ValueError):
        imp_n = 3
    imp_n = max(1, min(5, imp_n))
    hidden = bool(repo.get("hidden"))
    conf = repo.get("scoring_confidence") or "Low"
    conf_n = {"High": 3, "Med": 2, "Low": 1}.get(conf, 1)
    val = float((repo.get("scores") or {}).get("value") or 0)
    return (hidden, -imp_n, -val, -conf_n, repo.get("name", "").lower())


def assign_risk_adjusted_ranks(repos: list[dict]) -> None:
    ordered = sorted(
        repos,
        key=lambda r: (
            -float(r.get("risk_adjusted_value") or (r.get("scores") or {}).get("value") or 0),
            str(r.get("name", "")).lower(),
        ),
    )
    rank_map = {id(r): i + 1 for i, r in enumerate(ordered)}
    for r in repos:
        r["risk_adjusted_rank"] = rank_map.get(id(r), len(repos))


def _scan_candidate_dirs(project_root: Path) -> list[Path]:
    """Resolved git repo roots: children of project_root, this checkout, and PROJECTSCAN_EXTRA_ROOTS."""
    by_resolved: dict[Path, Path] = {}

    def consider(path: Path) -> None:
        try:
            p = path.expanduser().resolve()
        except OSError:
            return
        if not p.is_dir() or not (p / ".git").exists():
            return
        key = p.resolve()
        if key not in by_resolved:
            by_resolved[key] = p

    pr = project_root.expanduser().resolve()
    if pr.is_dir():
        try:
            for item in sorted(pr.iterdir(), key=lambda x: x.name.lower()):
                if item.is_dir():
                    consider(item)
        except OSError:
            pass

    consider(_script_dir())

    raw = (os.environ.get("PROJECTSCAN_EXTRA_ROOTS") or "").strip()
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if part:
                consider(Path(part))

    ordered = sorted(by_resolved.values(), key=lambda p: p.name.lower())
    return ordered


def scan_projects(root: Path | None = None) -> list[dict]:
    """Collect git repos (see `_scan_candidate_dirs`), analyse, merge manual data, write index."""
    project_root = (root or projects_dir()).expanduser().resolve()
    idx = index_dir()
    idx.mkdir(parents=True, exist_ok=True)

    old_data: dict[str, dict] = {}
    jpath = index_file_json()
    if jpath.exists():
        try:
            with open(jpath, encoding="utf-8") as f:
                old_repos = json.load(f)
            if isinstance(old_repos, list):
                old_data = {r["name"]: r for r in old_repos if isinstance(r, dict) and "name" in r}
        except (OSError, json.JSONDecodeError):
            old_data = {}

    repos: list[dict] = []
    if not project_root.is_dir():
        print(f"Warning: projects directory does not exist: {project_root}", file=sys.stderr)

    for item in _scan_candidate_dirs(project_root):
        print(f"Analysing {item.name}...")
        repo = analyze_repo(item)
        merge_persisted(repo, old_data.get(repo["name"]))
        repos.append(repo)

    repos.sort(key=sort_key)
    assign_risk_adjusted_ranks(repos)

    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(repos, f, indent=2)

    csv_path = index_file_csv()
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "name",
                "market_tag",
                "demand_evidence",
                "market_segment",
                "market_segment_label",
                "scoring_confidence",
                "total_score",
                "value",
                "progress",
                "feature_potential",
                "effort_to_monetize",
                "money_usd_low",
                "money_usd_high",
                "monetization_headline",
                "roi_pathway_title",
                "roi_playbook_id",
                "importance",
                "status",
                "hidden",
                "commit_count",
                "last_commit",
                "has_api",
                "manual_notes",
                "path",
            ]
        )
        for r in repos:
            s = r["scores"]
            mon = r.get("monetization") or {}
            headline = mon.get("headline", "")
            roi = r.get("roi_distribution") or {}
            writer.writerow(
                [
                    r["name"],
                    r.get("market_tag", ""),
                    r.get("demand_evidence", ""),
                    r.get("market_segment", ""),
                    r.get("market_segment_label", ""),
                    r.get("scoring_confidence", ""),
                    r["total_score"],
                    s["value"],
                    s["progress"],
                    s["feature_potential"],
                    s["effort_to_monetize"],
                    r.get("money_usd_low"),
                    r.get("money_usd_high"),
                    headline,
                    roi.get("pathway_title", ""),
                    roi.get("playbook_id", ""),
                    r.get("importance", 3),
                    r.get("status", "active"),
                    r.get("hidden", False),
                    r["commit_count"],
                    r["last_commit"],
                    r["has_api"],
                    r["manual_notes"],
                    r["path"],
                ]
            )

    return repos


def load_repos() -> list[dict]:
    jpath = index_file_json()
    if not jpath.exists():
        return []
    try:
        with open(jpath, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_repos(repos: list[dict]) -> None:
    idx = index_dir()
    idx.mkdir(parents=True, exist_ok=True)
    for r in repos:
        if r.get("path"):
            populate_repo_scoring(r)
        override = r.get("manual_value_override")
        if override is not None:
            try:
                v = max(0, min(100, int(override)))
                r["scores"]["value"] = v
                sb = r.get("score_breakdown")
                if isinstance(sb, dict):
                    sb["final_value"] = v
            except (TypeError, ValueError):
                pass
        flags = list((r.get("score_breakdown") or {}).get("risk_flags") or [])
        ra_mult = 0.5 if "ABANDONMENT_RISK" in flags else 1.0
        if isinstance(r.get("scores"), dict):
            r["risk_adjusted_value"] = round(float(r["scores"].get("value") or 0) * ra_mult, 3)
            sc = r["scores"]
            r["gtm_readiness"] = gtm_readiness_for(
                int(sc.get("effort_to_monetize") or 0),
                int(sc.get("progress") or 0),
            )
            r["total_score"] = r["gtm_readiness"]
        refresh_monetization_from_repo(r)
        refresh_money_usd(r)
        r["last_updated"] = datetime.now().isoformat(timespec="seconds")
    repos.sort(key=sort_key)
    assign_risk_adjusted_ranks(repos)
    with open(index_file_json(), "w", encoding="utf-8") as f:
        json.dump(repos, f, indent=2)

    csv_path = index_file_csv()
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "name",
                "market_tag",
                "demand_evidence",
                "market_segment",
                "market_segment_label",
                "scoring_confidence",
                "total_score",
                "value",
                "progress",
                "feature_potential",
                "effort_to_monetize",
                "money_usd_low",
                "money_usd_high",
                "monetization_headline",
                "roi_pathway_title",
                "roi_playbook_id",
                "importance",
                "status",
                "hidden",
                "commit_count",
                "last_commit",
                "has_api",
                "manual_notes",
                "path",
            ]
        )
        for r in repos:
            s = r["scores"]
            mon = r.get("monetization") or {}
            headline = mon.get("headline", "")
            roi = r.get("roi_distribution") or {}
            writer.writerow(
                [
                    r["name"],
                    r.get("market_tag", ""),
                    r.get("demand_evidence", ""),
                    r.get("market_segment", ""),
                    r.get("market_segment_label", ""),
                    r.get("scoring_confidence", ""),
                    r["total_score"],
                    s["value"],
                    s["progress"],
                    s["feature_potential"],
                    s["effort_to_monetize"],
                    r.get("money_usd_low"),
                    r.get("money_usd_high"),
                    headline,
                    roi.get("pathway_title", ""),
                    roi.get("playbook_id", ""),
                    r.get("importance", 3),
                    r.get("status", "active"),
                    r.get("hidden", False),
                    r["commit_count"],
                    r["last_commit"],
                    r["has_api"],
                    r["manual_notes"],
                    r["path"],
                ]
            )


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Revenue &amp; project prioritiser</title>
  <style>
    :root {
      --bg: #0f1419;
      --surface: #1a2332;
      --surface-hover: #243044;
      --border: #2d3a4d;
      --text: #e7ecf3;
      --muted: #8b9bb4;
      --accent: #5b9cfa;
      --accent-dim: #3d6eae;
      --good: #6bcb8e;
      --warn: #e8c547;
      --danger: #e07878;
      --radius: 10px;
      --font: system-ui, "Segoe UI", Roboto, Ubuntu, sans-serif;
      --mono: ui-monospace, "Cascadia Code", "Source Code Pro", Menlo, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(1200px 600px at 10% -10%, #1e2d45 0%, transparent 55%),
                  radial-gradient(800px 400px at 100% 0%, #152238 0%, transparent 50%),
                  var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 15px;
      line-height: 1.5;
    }
    header {
      padding: 1.25rem 1.5rem;
      border-bottom: 1px solid var(--border);
      background: rgba(26, 35, 50, 0.85);
      backdrop-filter: blur(8px);
      position: sticky;
      top: 0;
      z-index: 10;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.75rem 1.25rem;
    }
    header h1 {
      margin: 0;
      font-size: 1.15rem;
      font-weight: 600;
      letter-spacing: -0.02em;
      flex: 1 1 200px;
    }
    header .meta {
      color: var(--muted);
      font-size: 0.85rem;
      font-family: var(--mono);
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      align-items: center;
    }
    button, .pill {
      font-family: var(--font);
      font-size: 0.875rem;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      padding: 0.45rem 0.9rem;
      cursor: pointer;
      transition: background 0.15s, border-color 0.15s;
    }
    button:hover:not(:disabled) {
      background: var(--surface-hover);
      border-color: var(--accent-dim);
    }
    button.primary {
      background: linear-gradient(180deg, #4a82d9 0%, var(--accent-dim) 100%);
      border-color: #4f7fc4;
      color: #fff;
    }
    button.primary:hover:not(:disabled) {
      filter: brightness(1.08);
    }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    label.pill {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      cursor: pointer;
      user-select: none;
    }
    label.pill input { accent-color: var(--accent); }
    main { padding: 1.25rem 1.5rem 2.5rem; max-width: 1200px; margin: 0 auto; }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
      gap: 0.75rem;
      margin-bottom: 1.25rem;
    }
    .stat {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 0.75rem 1rem;
    }
    .stat .v { font-size: 1.35rem; font-weight: 600; font-family: var(--mono); }
    .stat .k { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.06em; }
    .empty {
      text-align: center;
      padding: 3rem 1rem;
      color: var(--muted);
      border: 1px dashed var(--border);
      border-radius: var(--radius);
    }
    .cards { display: flex; flex-direction: column; gap: 0.75rem; }
    article.card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1rem 1.1rem;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 0.75rem 1rem;
      align-items: start;
    }
    article.card.hidden-proj { opacity: 0.55; }
    article.card .top {
      grid-column: 1 / -1;
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0.5rem 0.75rem;
    }
    article.card h2 {
      margin: 0;
      font-size: 1.05rem;
      font-weight: 600;
    }
    article.card .path {
      font-family: var(--mono);
      font-size: 0.75rem;
      color: var(--muted);
      word-break: break-all;
    }
    .score-pill {
      font-family: var(--mono);
      font-weight: 600;
      font-size: 0.95rem;
      padding: 0.25rem 0.65rem;
      border-radius: 999px;
      background: rgba(91, 156, 250, 0.15);
      color: var(--accent);
      border: 1px solid rgba(91, 156, 250, 0.35);
    }
    .money-pill {
      font-family: var(--mono);
      font-weight: 600;
      font-size: 0.8rem;
      padding: 0.25rem 0.65rem;
      border-radius: 999px;
      background: rgba(74, 222, 128, 0.12);
      color: var(--good);
      border: 1px solid rgba(74, 222, 128, 0.35);
    }
    .money-pill.manual {
      background: rgba(232, 197, 71, 0.12);
      color: var(--warn);
      border-color: rgba(232, 197, 71, 0.35);
    }
    .toolbar-main { flex: 8 1 340px; }
    details.bells-panel {
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: rgba(15, 18, 24, 0.65);
      margin-bottom: 0.95rem;
    }
    details.bells-panel > summary {
      cursor: pointer;
      padding: 0.5rem 0.85rem;
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--muted);
      list-style: none;
      user-select: none;
    }
    details.bells-panel > summary::-webkit-details-marker { display: none; }
    details.bells-panel[open] > summary {
      color: var(--text);
      border-bottom: 1px solid var(--border);
    }
    .bells-body {
      padding: 0.85rem 0.95rem 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }
    .bells-toggles { margin-bottom: 0; }
    .intro-muted {
      font-size: 0.82rem;
      color: var(--muted);
      margin: 0 0 0.85rem;
      line-height: 1.45;
    }
    .toolbar-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.85rem;
      align-items: flex-end;
      margin-bottom: 0.95rem;
    }
    .toolbar-row .mini-field select { width: auto; max-width: 260px; }
    .disc {
      font-size: 0.8rem;
      color: var(--muted);
      margin: -0.25rem 0 1rem;
      line-height: 1.45;
      border-left: 3px solid var(--border);
      padding: 0.35rem 0 0.35rem 0.65rem;
    }
    .roi-strip {
      grid-column: 1 / -1;
      background: linear-gradient(135deg, rgba(192, 132, 252, 0.1), rgba(56, 189, 248, 0.06));
      border: 1px solid rgba(192, 132, 252, 0.28);
      border-radius: var(--radius);
      padding: 0.82rem 1rem;
      margin-bottom: 0;
    }
    .roi-strip h3 {
      margin: 0 0 0.45rem;
      font-size: 0.74rem;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      color: rgba(226, 185, 255, 0.95);
      font-weight: 600;
    }
    .roi-pathway-title {
      margin: 0 0 0.55rem;
      font-size: 1rem;
      font-weight: 600;
      letter-spacing: -0.015em;
    }
    .roi-line {
      margin: 0.4rem 0 0;
      font-size: 0.865rem;
      line-height: 1.46;
      color: var(--text);
    }
    .roi-k {
      display: block;
      font-size: 0.68rem;
      text-transform: uppercase;
      letter-spacing: 0.055em;
      color: var(--muted);
      font-weight: 600;
      margin-bottom: 0.12rem;
    }
    .roi-meta {
      margin: 0.55rem 0 0;
      font-family: var(--mono);
      font-size: 0.72rem;
      color: var(--muted);
    }
    .monetization-box {
      grid-column: 1 / -1;
      background: #121920;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 0.75rem 0.95rem;
    }
    .monetization-box h3 {
      margin: 0 0 0.35rem;
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      font-weight: 600;
    }
    .monet-headline { font-size: 0.9rem; margin-bottom: 0.45rem; }
    ul.monet-paths { margin: 0; padding-left: 1.05rem; display: grid; gap: 0.5rem; }
    ul.monet-paths li { margin: 0; }
    .monet-path-title { font-weight: 600; font-size: 0.845rem; }
    .monet-detail { color: var(--muted); font-size: 0.805rem; margin-top: 0.06rem; }
    .monet-model-tag {
      font-size: 0.67rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--accent);
      display: inline-block;
      margin-top: 0.18rem;
    }
    .bars {
      grid-column: 1 / -1;
      display: grid;
      gap: 0.45rem;
    }
    .bar-row {
      display: grid;
      grid-template-columns: 9rem 1fr 2.25rem;
      gap: 0.5rem;
      align-items: center;
      font-size: 0.8rem;
    }
    .bar-row span.label { color: var(--muted); }
    .bar-track {
      height: 8px;
      background: #0d1218;
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid var(--border);
    }
    .bar-fill {
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--accent-dim), var(--accent));
    }
    .bar-row span.num {
      font-family: var(--mono);
      text-align: right;
      color: var(--muted);
    }
    .controls {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 0.65rem;
      grid-column: 1 / -1;
    }
    .field label {
      display: block;
      font-size: 0.75rem;
      color: var(--muted);
      margin-bottom: 0.2rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    select, input[type="number"], textarea {
      width: 100%;
      font-family: var(--font);
      font-size: 0.875rem;
      padding: 0.4rem 0.5rem;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: #0d1218;
      color: var(--text);
    }
    textarea { min-height: 4rem; resize: vertical; font-family: var(--font); }
    .actions { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
    .toast {
      position: fixed;
      bottom: 1.25rem;
      right: 1.25rem;
      padding: 0.65rem 1rem;
      border-radius: 8px;
      background: var(--surface);
      border: 1px solid var(--border);
      box-shadow: 0 8px 32px rgba(0,0,0,0.35);
      font-size: 0.875rem;
      opacity: 0;
      transform: translateY(8px);
      transition: opacity 0.2s, transform 0.2s;
      pointer-events: none;
      z-index: 100;
    }
    .toast.show { opacity: 1; transform: translateY(0); }
    .toast.err { border-color: var(--danger); color: var(--danger); }
    #msg { margin: 0; color: var(--muted); font-size: 0.85rem; }
    a.portal-guide { color: var(--accent); font-size: 0.82rem; text-decoration: none; }
    a.portal-guide:hover { text-decoration: underline; }
    .guide-origin { font-size: 0.78rem; color: var(--muted); }
    .guide-origin code { font-size: 0.88em; color: var(--muted); }
    .market-tag {
      font-size: 0.72rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--accent);
      border: 1px solid var(--border);
      padding: 0.12rem 0.45rem;
      border-radius: 999px;
      white-space: nowrap;
    }
    .segment-label {
      font-size: 0.76rem;
      font-weight: 500;
      line-height: 1.25;
      color: #e9ddff;
      background: rgba(192, 132, 252, 0.12);
      border: 1px solid rgba(192, 132, 252, 0.38);
      padding: 0.18rem 0.55rem;
      border-radius: 999px;
      max-width: min(100%, 22rem);
    }
  </style>
</head>
<body>
  <header>
    <div class="toolbar-main">
      <h1>Revenue &amp; project prioritiser</h1>
      <p class="meta" id="rootLabel"></p>
    </div>
    <div class="toolbar">
      <button type="button" class="primary" id="btnScan">Rescan projects</button>
    </div>
  </header>
  <main>
    <p id="msg"></p>
    <p class="intro-muted">Heuristic prioritisation and illustrative USD bands — open <strong>Bells &amp; whistles</strong> for sorting, currency, exports, and Drive.</p>
    <details class="bells-panel" id="bellsPanel">
      <summary>Bells &amp; whistles · sorting, currency, reports, Drive</summary>
      <div class="bells-body">
        <p class="disc" style="margin:0">Rough <strong>annual revenue bands</strong> derive from heuristic scores and repo layout (illustrative only, not advice). Bands are stored in <strong>USD</strong>; display currency uses <code>open.er-api.com</code> (static fallback offline). Highest-ROI paths are playbook picks from repo signals. Google Drive uploads: <a class="portal-guide" href="@@DRIVE_GUIDE_HREF@@">setup guide</a>@@DRIVE_GUIDE_HINT@@</p>
        <div class="bells-toggles">
          <label class="pill"><input type="checkbox" id="showHidden" /> Show hidden projects</label>
        </div>
        <div class="toolbar-row">
          <div class="field mini-field">
            <label>Sort by</label>
            <select id="sortBy">
              <option value="importance_score">Importance · heuristic score</option>
              <option value="money_high">Revenue band (high end)</option>
              <option value="money_low">Revenue band (low end)</option>
              <option value="total_score">Heuristic score only</option>
              <option value="value_bar">Value bar</option>
              <option value="progress">Progress bar</option>
              <option value="name">Name A–Z</option>
            </select>
          </div>
          <div class="field mini-field">
            <label>Display currency</label>
            <select id="fxCurrency"><option value="GBP">GBP</option></select>
          </div>
          <span class="meta" id="fxMeta" style="align-self:center;font-size:.8rem;color:var(--muted);margin-left:auto;"></span>
        </div>
        <div class="toolbar-row drive-upload-row">
          <div class="field mini-field">
            <label>Report format</label>
            <select id="reportFormat" title="File format (download &amp; Drive upload)">
              <option value="txt">Plain text (.txt)</option>
              <option value="md">Markdown (.md)</option>
              <option value="csv">Spreadsheet (.csv)</option>
              <option value="json">JSON</option>
            </select>
          </div>
          <div class="field mini-field">
            <label>Report subset</label>
            <select id="reportSubset" title="Which repos to include">
              <option value="all">All projects</option>
              <option value="past_metaai">Name lexically after &quot;MetaAI&quot;</option>
            </select>
          </div>
          <button type="button" id="btnDownloadReport" title="Save locally, then drag into Drive in a browser">Download report</button>
          <button type="button" class="primary" id="btnDriveUpload">Upload to Google Drive</button>
          <span class="meta" id="driveStatusLine" style="align-self:center;font-size:0.8rem;color:var(--muted);flex:1;min-width:12rem"></span>
        </div>
      </div>
    </details>
    <div class="stats" id="stats"></div>
    <div class="cards" id="cards"></div>
  </main>
  <div class="toast" id="toast" role="status"></div>
  <script>
    const $ = (id) => document.getElementById(id);
    const toast = $('toast');
    const LS_CCY = 'projectscan_display_ccy';
    const LS_SORT = 'projectscan_sort_key';
    const LS_BELLS = 'projectscan_bells_open';
    const DEFAULT_CCY = 'GBP';
    let fxPayload = null;
    let reposCache = [];
    let lastRoot = '';

    function showToast(text, err) {
      toast.textContent = text;
      toast.className = 'toast show' + (err ? ' err' : '');
      clearTimeout(showToast._t);
      showToast._t = setTimeout(() => { toast.classList.remove('show'); }, 2800);
    }
    async function api(method, path, body) {
      const opt = { method, headers: {} };
      if (body !== undefined) {
        opt.headers['Content-Type'] = 'application/json';
        opt.body = JSON.stringify(body);
      }
      const r = await fetch(path, opt);
      const t = await r.text();
      let data = null;
      try { data = t ? JSON.parse(t) : null; } catch (_) {}
      if (!r.ok) throw new Error((data && data.error) || t || r.statusText);
      return data;
    }
    function fracDigits(ccy) {
      if (['JPY','KRW','VND'].includes(ccy)) return 0;
      return 0;
    }
    function rateFor(ccy) {
      if (!fxPayload || !fxPayload.rates) return ccy === 'USD' ? 1 : null;
      const x = fxPayload.rates[ccy];
      return typeof x === 'number' ? x : (x ? parseFloat(x) : null);
    }
    function convertFromUsd(usd, ccy) {
      const r = rateFor(ccy);
      if (r === null || r === undefined || !Number.isFinite(r)) return null;
      return usd * r;
    }
    function fmtMoney(n, ccy) {
      try {
        return new Intl.NumberFormat(undefined, {
          style: 'currency',
          currency: ccy,
          minimumFractionDigits: fracDigits(ccy),
          maximumFractionDigits: fracDigits(ccy),
        }).format(Math.round(n));
      } catch (_) {
        const sym = {'USD':'$','EUR':'EUR ','GBP':'GBP '} [ccy] || (ccy + ' ');
        return sym + Math.round(n).toLocaleString();
      }
    }
    function fmtBandUsdLowHigh(loUsd, hiUsd, dispCcy) {
      const a = Number(loUsd) || 0;
      const b = Number(hiUsd) || 0;
      if (dispCcy === 'USD') return fmtMoney(a,'USD').replace(/\\s/g,'') + ' – ' + fmtMoney(b,'USD').replace(/\\s/g,'');
      const lo = convertFromUsd(a, dispCcy);
      const hi = convertFromUsd(b, dispCcy);
      if (lo === null || hi === null) return fmtMoney(a,'USD') + ' – ' + fmtMoney(b,'USD') + ' (USD)';
      return fmtMoney(lo, dispCcy).replace(/\\s/g,'') + ' – ' + fmtMoney(hi, dispCcy).replace(/\\s/g,'');
    }
    function refreshCurrencySelect() {
      const sel = $('fxCurrency');
      const prev = localStorage.getItem(LS_CCY) || DEFAULT_CCY;
      const rates = (fxPayload && fxPayload.rates) || { USD: 1, GBP: 0.79 };
      const codes = Object.keys(rates).sort();
      sel.innerHTML = codes.map((c) => '<option value="' + c + '">' + c + '</option>').join('');
      if (codes.includes(prev)) sel.value = prev; else sel.value = DEFAULT_CCY;
      const src = fxPayload && fxPayload.source ? fxPayload.source : 'offline';
      const dt = fxPayload && fxPayload.date ? ' · ' + fxPayload.date : '';
      $('fxMeta').textContent = 'FX: ' + src + dt;
    }
    function clampImp(x) {
      const n = parseInt(x, 10);
      if (!Number.isFinite(n)) return 3;
      return Math.min(5, Math.max(1, n));
    }
    function sortRepos(rows, key) {
      const m = rows.slice();
      const byHidden = (a,b) => Number(!!a.hidden) - Number(!!b.hidden);
      if (key === 'importance_score') {
        m.sort((a,b) => {
          const h = byHidden(a,b); if (h) return h;
          const ia = clampImp(a.importance), ib = clampImp(b.importance);
          if (ib !== ia) return ib - ia;
          const ta = Number(a.total_score)||0, tb = Number(b.total_score)||0;
          if (tb !== ta) return tb - ta;
          return (a.name||'').localeCompare(b.name||'');
        });
        return m;
      }
      if (key === 'money_high') {
        m.sort((a,b) => {
          const h = byHidden(a,b); if (h) return h;
          return (Number(b.money_usd_high)||0) - (Number(a.money_usd_high)||0);
        });
        return m;
      }
      if (key === 'money_low') {
        m.sort((a,b) => {
          const h = byHidden(a,b); if (h) return h;
          return (Number(b.money_usd_low)||0) - (Number(a.money_usd_low)||0);
        });
        return m;
      }
      if (key === 'total_score') {
        m.sort((a,b) => {
          const h = byHidden(a,b); if (h) return h;
          return (Number(b.total_score)||0) - (Number(a.total_score)||0);
        });
        return m;
      }
      if (key === 'value_bar') {
        m.sort((a,b) => {
          const h = byHidden(a,b); if (h) return h;
          const sa = (a.scores||{}).value||0, sb = (b.scores||{}).value||0;
          return sb - sa;
        });
        return m;
      }
      if (key === 'progress') {
        m.sort((a,b) => {
          const h = byHidden(a,b); if (h) return h;
          const sa = (a.scores||{}).progress||0, sb = (b.scores||{}).progress||0;
          return sb - sa;
        });
        return m;
      }
      if (key === 'name') {
        m.sort((a,b) => {
          const h = byHidden(a,b); if (h) return h;
          return (a.name||'').localeCompare(b.name||'');
        });
        return m;
      }
      return sortRepos(rows, 'importance_score');
    }
    function escapeHtml(s) {
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    function bar(label, value, title) {
      const v = Math.max(0, Math.min(100, Number(value) || 0));
      const t = title ? ` title="${escapeHtml(title)}"` : '';
      return `<div class="bar-row"${t}><span class="label">${label}</span><div class="bar-track"><div class="bar-fill" style="width:${v}%"></div></div><span class="num">${v}</span></div>`;
    }
    function monetPathsHtml(mon) {
      const paths = mon && Array.isArray(mon.paths) ? mon.paths : [];
      if (!paths.length) return '<p class="monet-detail">Run <strong>Rescan</strong> to refresh how-to-monetise hints.</p>';
      return '<ul class="monet-paths">' + paths.map((p) => {
        const model = p.model ? '<span class="monet-model-tag">' + escapeHtml(p.model) + '</span>' : '';
        return '<li><div class="monet-path-title">' + escapeHtml(p.title||'') + '</div><div class="monet-detail">' + escapeHtml(p.detail||'') + '</div>' + model + '</li>';
      }).join('') + '</ul>';
    }
    function renderStats(repos, visible, ccy) {
      const el = $('stats');
      if (!repos.length) { el.innerHTML = ''; return; }
      const avg = visible.length ? visible.reduce((s, r) => s + (Number(r.total_score) || 0), 0) / visible.length : 0;
      const active = visible.filter((r) => r.status === 'active').length;
      const hiUsd = visible.reduce((s,r) => s + (Number(r.money_usd_high)||0), 0);
      let pf = '';
      if (hiUsd > 0) {
        pf = fmtMoney(hiUsd, 'USD') + ' summed highs (indexed in USD)';
        const cnv = convertFromUsd(hiUsd, ccy);
        if (ccy !== 'USD' && cnv !== null) pf += ' · ≈ ' + fmtMoney(cnv, ccy);
      } else pf = '—';
      el.innerHTML = `
        <div class="stat"><div class="k">Repositories</div><div class="v">${repos.length}</div></div>
        <div class="stat"><div class="k">Shown</div><div class="v">${visible.length}</div></div>
        <div class="stat"><div class="k">Avg score (shown)</div><div class="v">${avg.toFixed(1)}</div></div>
        <div class="stat"><div class="k">Active</div><div class="v">${active}</div></div>
        <div class="stat" style="grid-column:span 2"><div class="k">Portfolio ceiling (rough)</div><div class="v" style="font-size:0.95rem">${pf}</div></div>`;
    }
    function render(repos, rootPath) {
      lastRoot = rootPath || '';
      $('rootLabel').textContent = lastRoot;
      const showH = $('showHidden').checked;
      const sk = $('sortBy').value || 'importance_score';
      const ccy = $('fxCurrency').value || DEFAULT_CCY;
      const visibleAll = showH ? repos : repos.filter((r) => !r.hidden);
      const ordered = sortRepos(visibleAll, sk);
      renderStats(repos, visibleAll, ccy);
      const cards = $('cards');
      if (!repos.length) {
        cards.innerHTML = '<div class="empty">No index yet. Click <strong>Rescan projects</strong> to scan your projects folder.</div>';
        return;
      }
      if (!visibleAll.length) {
        cards.innerHTML = '<div class="empty">All projects are hidden. Enable <strong>Show hidden</strong> or unhide items.</div>';
        return;
      }
      cards.innerHTML = ordered.map((r) => {
        const s = r.scores || {};
        const hv = r.manual_value_override != null ? r.manual_value_override : '';
        const imp = Number(r.importance) || 3;
        const st = r.status || 'active';
        const hidCls = r.hidden ? 'hidden-proj' : '';
        const mon = r.monetization || {};
        const loU = Number(r.money_usd_low) || 0;
        const hiU = Number(r.money_usd_high) || 0;
        const manualBand = r.manual_money_low != null && r.manual_money_high != null;
        const moneyCls = 'money-pill' + (manualBand ? ' manual' : '');
        const moneyTitle = manualBand ? 'Your manual annual band (USD)' : 'Auto heuristic annual band (stored USD)';
        const bandLabel = fmtBandUsdLowHigh(loU, hiU, ccy) + ' / yr';
        const mlow = r.manual_money_low != null ? r.manual_money_low : '';
        const mhigh = r.manual_money_high != null ? r.manual_money_high : '';
        const roi = r.roi_distribution || {};
        const altHum = roi.alternative_ids
          ? String(roi.alternative_ids).split(',').map((x) => x.trim()).filter(Boolean).join(' · ')
          : '';
        const roiFootBits = [];
        if (roi.playbook_id) roiFootBits.push('playbook · ' + String(roi.playbook_id));
        if (altHum) roiFootBits.push('next best · ' + altHum);
        const roiFoot = roiFootBits.length ? escapeHtml(roiFootBits.join(' · ')) : '';
        const segHtml = (r.market_segment_label || '').trim()
          ? '<span class="segment-label" title="Audience segment (scoring_guidance.md taxonomy)">' + escapeHtml(String(r.market_segment_label).trim()) + '</span>'
          : '';
        return `<article class="card ${hidCls}" data-name="${escapeHtml(r.name)}">
          <div class="top">
            <h2>${escapeHtml(r.name)}</h2>
            <span class="market-tag" title="Heuristic market tag (Meta/Cursor taxonomy)">${escapeHtml(r.market_tag || '—')}</span>
            ${segHtml}
            <span class="score-pill" title="GTM readiness (0.7·ship/monetise ease + 0.3·progress), not upside final_value · confidence ${escapeHtml(r.scoring_confidence || '—')}">${Number(r.total_score).toFixed(1)}</span>
            <span class="${moneyCls}" title="${escapeHtml(moneyTitle)}">${escapeHtml(bandLabel)}</span>
            <span class="path">${escapeHtml(r.path)}</span>
          </div>
          <div class="roi-strip">
            <h3>Highest-ROI distribution path</h3>
            <div class="roi-pathway-title">${escapeHtml(roi.pathway_title || '—')}</div>
            <p class="roi-line"><span class="roi-k">GTM · distribution</span>${escapeHtml(roi.distribution_method || '')}</p>
            <p class="roi-line"><span class="roi-k">Why ROI is maximised</span>${escapeHtml(roi.why_roi || '')}</p>
            ${roiFoot ? '<p class="roi-meta">' + roiFoot + '</p>' : ''}
          </div>
          <div class="monetization-box">
            <h3>How money can be made</h3>
            <div class="monet-headline">${escapeHtml(mon.headline || '')}</div>
            ${monetPathsHtml(mon)}
            <div class="field" style="margin-top:0.65rem">
              <label>Your angle (notes)</label>
              <textarea class="fld-monet-notes" placeholder="Pricing idea, ICP, channel…">${escapeHtml(r.monetization_notes || '')}</textarea>
            </div>
          </div>
          <div class="bars">
            ${bar('Value', s.value, r.demand_hint || '')}
            ${bar('Progress', s.progress)}
            ${bar('Feature potential', s.feature_potential)}
            ${bar('Ship / monetise ease', s.effort_to_monetize)}
          </div>
          <div class="controls">
            <div class="field"><label>Importance (1–5)</label>
              <input type="number" min="1" max="5" step="1" class="fld-importance" value="${imp}" /></div>
            <div class="field"><label>Status</label>
              <select class="fld-status">
                <option value="idea"${st === 'idea' ? ' selected' : ''}>Idea</option>
                <option value="active"${st === 'active' ? ' selected' : ''}>Active</option>
                <option value="paused"${st === 'paused' ? ' selected' : ''}>Paused</option>
                <option value="shipped"${st === 'shipped' ? ' selected' : ''}>Shipped</option>
              </select></div>
            <div class="field"><label>Value override (0–100, empty = auto)</label>
              <input type="number" min="0" max="100" step="1" class="fld-override" value="${hv}" placeholder="auto" /></div>
            <div class="field"><label>Annual band override (USD)</label>
              <div style="display:flex;gap:0.45rem;align-items:center">
                <input type="number" min="0" step="100" class="fld-money-low" style="flex:1" value="${mlow}" placeholder="low" />
                <span style="color:var(--muted)">–</span>
                <input type="number" min="0" step="100" class="fld-money-high" style="flex:1" value="${mhigh}" placeholder="high" />
              </div>
              <span class="meta" style="font-size:0.75rem;display:block;margin-top:0.2rem">Empty both = heuristic band.</span></div>
            <div class="field" style="grid-column: span 2;">
              <label>Notes</label>
              <textarea class="fld-notes" placeholder="Why this matters, next steps…">${escapeHtml(r.manual_notes || '')}</textarea>
            </div>
          </div>
          <div class="actions">
            <button type="button" class="btn-save primary">Save</button>
            <label class="pill"><input type="checkbox" class="fld-hidden"${r.hidden ? ' checked' : ''}/> Hidden</label>
            <span class="meta">${escapeHtml(r.last_commit || '—')} · ${r.commit_count ?? 0} commits · ${r.file_count ?? 0} files</span>
          </div>
        </article>`;
      }).join('');
      cards.querySelectorAll('article.card').forEach((card) => {
        card.querySelector('.btn-save').addEventListener('click', async () => {
          const name = card.dataset.name;
          const importance = parseInt(card.querySelector('.fld-importance').value, 10);
          const status = card.querySelector('.fld-status').value;
          const ov = card.querySelector('.fld-override').value.trim();
          const manual_value_override = ov === '' ? null : parseInt(ov, 10);
          const manual_notes = card.querySelector('.fld-notes').value;
          const monetization_notes = card.querySelector('.fld-monet-notes').value;
          const hidden = card.querySelector('.fld-hidden').checked;
          const rawLo = card.querySelector('.fld-money-low').value.trim();
          const rawHi = card.querySelector('.fld-money-high').value.trim();
          let manual_money_low = null, manual_money_high = null;
          if (rawLo !== '' || rawHi !== '') {
            if (rawLo === '' || rawHi === '') {
              showToast('USD band: fill both numbers or leave both blank.', true);
              return;
            }
            manual_money_low = parseInt(rawLo, 10);
            manual_money_high = parseInt(rawHi, 10);
            if (!Number.isFinite(manual_money_low) || !Number.isFinite(manual_money_high)) {
              showToast('Invalid USD band.', true);
              return;
            }
          }
          try {
            await api('POST', 'api/project/' + encodeURIComponent(name), {
              importance, status, manual_value_override, manual_notes, monetization_notes, hidden,
              manual_money_low, manual_money_high
            });
            showToast('Saved ' + name);
            await load();
          } catch (e) {
            showToast(e.message, true);
          }
        });
      });
    }
    function renderFromCache() {
      render(reposCache, lastRoot);
    }
    async function refreshDriveBanner() {
      const el = $('driveStatusLine');
      if (!el) return;
      try {
        const st = await api('GET', 'api/drive/status');
        if (!st.libraries_available) {
          el.textContent = 'Drive: install google libs — see CLI hint below.';
          return;
        }
        if (st.email_gate_message) {
          el.textContent = 'Drive: ' + st.email_gate_message;
          return;
        }
        const bits = [];
        if (!st.has_client_secrets) bits.push('no client JSON');
        if (!st.authenticated) {
          if (st.token_account_email && (st.email_allowlist || []).length)
            bits.push('account ' + st.token_account_email + ' not allowed');
          else bits.push('run drive-auth');
        }
        el.textContent = bits.length ? 'Drive: ' + bits.join(' · ') : ('Drive: signed in as ' + (st.token_account_email || '?'));
      } catch (e) {
        el.textContent = 'Drive status: —';
      }
    }
    async function load() {
      $('msg').textContent = 'Loading…';
      try {
        const [data, fx] = await Promise.all([
          api('GET', 'api/projects'),
          api('GET', 'api/fxrates').catch(() => null),
          refreshDriveBanner(),
        ]);
        fxPayload = fx;
        reposCache = data.repos || [];
        refreshCurrencySelect();
        const sortSel = $('sortBy');
        const savedSort = localStorage.getItem(LS_SORT);
        if (savedSort) sortSel.value = savedSort;
        $('msg').textContent = '';
        render(reposCache, data.projects_root);
      } catch (e) {
        $('msg').textContent = e.message;
      }
    }
    $('btnScan').addEventListener('click', async () => {
      const b = $('btnScan');
      b.disabled = true;
      try {
        await api('POST', 'api/scan', {});
        showToast('Scan complete');
        await load();
      } catch (e) {
        showToast(e.message, true);
      } finally {
        b.disabled = false;
      }
    });
    $('showHidden').addEventListener('change', () => renderFromCache());
    $('sortBy').addEventListener('change', () => {
      localStorage.setItem(LS_SORT, $('sortBy').value);
      renderFromCache();
    });
    $('fxCurrency').addEventListener('change', () => {
      localStorage.setItem(LS_CCY, $('fxCurrency').value);
      renderFromCache();
    });
    function stripQuotes(s) {
      const x = (s || '').trim();
      if ((x[0] === '"' && x[x.length - 1] === '"') || (x[0] === "'" && x[x.length - 1] === "'"))
        return x.slice(1, -1);
      return x;
    }
    function parseFilenameFromDisposition(cd) {
      if (!cd) return null;
      const lk = cd.toLowerCase();
      const u8tag = "filename*=utf-8''";
      const pTag = lk.indexOf(u8tag);
      if (pTag >= 0) {
        let v = cd.slice(pTag + u8tag.length).trim();
        const semi = v.indexOf(';');
        if (semi >= 0) v = v.slice(0, semi).trim();
        try { return decodeURIComponent(v); } catch (_) { return v; }
      }
      let m = cd.match(/filename="([^"]+)"/i);
      if (m) return m[1];
      m = cd.match(/filename=([^;\\s]+)/);
      return m ? stripQuotes(m[1]) : null;
    }
    async function downloadPortfolioReport() {
      const fmt = $('reportFormat').value;
      const subset = $('reportSubset').value;
      const qs = 'format=' + encodeURIComponent(fmt) + '&subset=' + encodeURIComponent(subset);
      const r = await fetch('api/report/download?' + qs);
      if (!r.ok) {
        let msg = await r.text();
        try {
          const j = JSON.parse(msg);
          if (j && j.error) msg = j.error;
        } catch (_) {}
        throw new Error(msg || r.statusText);
      }
      const blob = await r.blob();
      let name = parseFilenameFromDisposition(r.headers.get('Content-Disposition')) ||
        ('projectscan_report.' + ({ txt: 'txt', md: 'md', csv: 'csv', json: 'json' }[fmt] || 'txt'));
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 6000);
      showToast('Saved ' + name);
    }
    $('btnDownloadReport').addEventListener('click', async () => {
      const b = $('btnDownloadReport');
      b.disabled = true;
      try {
        await downloadPortfolioReport();
      } catch (e) {
        showToast(e.message, true);
      } finally {
        b.disabled = false;
      }
    });
    $('btnDriveUpload').addEventListener('click', async () => {
      const b = $('btnDriveUpload');
      b.disabled = true;
      try {
        const payload = await api('POST', 'api/drive/upload', {
          format: $('reportFormat').value,
          subset: $('reportSubset').value,
        });
        const link = payload.webViewLink || payload.webContentLink || '';
        showToast(link ? 'Uploaded · open in Drive' : 'Uploaded to Drive (' + (payload.name || payload.id || 'ok') + ')');
        if (link && window.confirm('Open the file in Google Drive?')) window.open(link, '_blank');
        refreshDriveBanner();
      } catch (e) {
        showToast(e.message, true);
      } finally {
        b.disabled = false;
      }
    });
    const bellsPanel = $('bellsPanel');
    if (bellsPanel && localStorage.getItem(LS_BELLS) === '1') bellsPanel.open = true;
    if (bellsPanel) {
      bellsPanel.addEventListener('toggle', () => {
        localStorage.setItem(LS_BELLS, bellsPanel.open ? '1' : '0');
      });
    }
    load();
  </script>
</body>
</html>
"""


def drive_setup_guide_href() -> str:
    """Absolute guide URL when ``PROJECTSCAN_PUBLIC_ORIGIN`` is set; else path on same host."""
    origin = (os.environ.get("PROJECTSCAN_PUBLIC_ORIGIN") or "").strip().rstrip("/")
    if origin:
        return f"{origin}/moneymakers-drive-guide.html"
    return "/moneymakers-drive-guide.html"


def dashboard_html_document() -> str:
    href = html.escape(drive_setup_guide_href(), quote=True)
    hint = ""
    origin = (os.environ.get("PROJECTSCAN_PUBLIC_ORIGIN") or "").strip().rstrip("/")
    if origin:
        hint = (
            ' <span class="guide-origin">(<code>'
            + html.escape(origin, quote=False)
            + "</code>)</span>"
        )
    return DASHBOARD_HTML.replace("@@DRIVE_GUIDE_HREF@@", href).replace("@@DRIVE_GUIDE_HINT@@", hint)


def normalize_loaded_repo(r: dict) -> None:
    """Backfill computed money / monetisation / ROI pathway for legacy JSON."""
    scores = r.get("scores")
    if not isinstance(scores, dict):
        return
    roi = r.get("roi_distribution")
    need = (
        r.get("money_usd_low") is None
        or r.get("money_usd_high") is None
        or not isinstance(r.get("monetization"), dict)
        or not r.get("monetization", {}).get("paths")
        or not isinstance(roi, dict)
        or not roi.get("pathway_title")
    )
    if need:
        refresh_monetization_from_repo(r)
        refresh_money_usd(r)


def normalize_loaded_repos(repos: list[dict]) -> None:
    for r in repos:
        normalize_loaded_repo(r)


# --- Google Drive (optional; pip install -r requirements-google.txt) -----------------

DRIVE_SCOPE = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/userinfo.email",
]

REPORT_SUBSETS = ("all", "past_metaai")


def drive_allowlist_normalized() -> frozenset[str]:
    """Nonempty lowercased emails from ``PROJECTSCAN_DRIVE_ALLOWED_EMAILS`` (comma-separated)."""
    raw = (os.environ.get("PROJECTSCAN_DRIVE_ALLOWED_EMAILS") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def drive_google_account_email(creds) -> str | None:
    """Return lowercased email for credentials, or None if unreadable."""
    try:
        if not creds or not getattr(creds, "valid", False):
            return None
        tok = getattr(creds, "token", None)
        if not tok:
            return None
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tok}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
        data = json.loads(body)
        email = data.get("email")
        return email.strip().lower() if isinstance(email, str) else None
    except (AttributeError, OSError, TypeError, ValueError, urllib.error.HTTPError, urllib.error.URLError):
        return None


def drive_stored_credentials_or_none():
    """Load OAuth token from disk and refresh if needed. Does not enforce email allowlist."""
    Credentials, _, _, _, Request, _ = import_google_clients()
    tok = drive_token_path()
    if not tok.is_file():
        return None
    creds = Credentials.from_authorized_user_file(str(tok), DRIVE_SCOPE)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            tok.write_text(creds.to_json(), encoding="utf-8")
        else:
            return None
    return creds


def drive_client_secrets_path() -> Path:
    env = os.environ.get("PROJECTSCAN_DRIVE_CLIENT_SECRETS")
    if env:
        return Path(env).expanduser()
    return index_dir() / "client_secrets.json"


def drive_token_path() -> Path:
    env = os.environ.get("PROJECTSCAN_DRIVE_TOKEN_PATH")
    if env:
        return Path(env).expanduser()
    return index_dir() / "google_drive_token.json"


def drive_resolve_oauth_loopback_port(explicit_cli: int | None) -> int:
    """0 = random ephemeral port; >0 = fixed (for SSH -L forwarding)."""
    if explicit_cli is not None:
        return max(0, min(65535, explicit_cli))
    raw = (os.environ.get("PROJECTSCAN_DRIVE_OAUTH_PORT") or "").strip()
    if not raw:
        return 0
    try:
        return max(0, min(65535, int(raw)))
    except ValueError:
        print("Ignoring invalid PROJECTSCAN_DRIVE_OAUTH_PORT (expected integer).", file=sys.stderr)
        return 0


def import_google_clients():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build as google_build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseUpload
    except ImportError as exc:
        raise RuntimeError(
            "Google API libraries missing. Install with: pip install -r requirements-google.txt"
        ) from exc
    return Credentials, HttpError, InstalledAppFlow, MediaIoBaseUpload, Request, google_build


def drive_credentials_or_none():
    """Load stored OAuth token; refresh if needed. None if missing, invalid, or not on allowlist."""
    creds = drive_stored_credentials_or_none()
    if not creds:
        return None
    allow = drive_allowlist_normalized()
    if not allow:
        return creds
    em = drive_google_account_email(creds)
    if em is None or em not in allow:
        return None
    return creds


def drive_run_oauth_setup(*, oauth_loopback_port: int | None = None) -> None:
    """Interactive one-time consent; writes ``google_drive_token.json``.

    ``oauth_loopback_port`` — 0 or None after resolution means ephemeral port (default).
    A fixed port (e.g. 9876) allows ``ssh -L 9876:127.0.0.1:9876 host`` so your local
    browser completes the localhost redirect against the SSH session host.
    """
    Credentials, _, InstalledAppFlow, _, _, _ = import_google_clients()
    sec = drive_client_secrets_path()
    if not sec.is_file():
        print(
            "Missing OAuth client file.\n"
            f"  Expected: {sec}\n"
            "  Download a Google Cloud “Desktop app” OAuth client JSON and save it there, or set\n"
            "  PROJECTSCAN_DRIVE_CLIENT_SECRETS to its path.\n"
            "  Enable Drive API for the project.",
            file=sys.stderr,
        )
        sys.exit(1)
    port = drive_resolve_oauth_loopback_port(oauth_loopback_port)
    bind_port = port if port > 0 else 0
    if bind_port > 0:
        print(
            "\nFixed OAuth loopback port for SSH (run on your laptop in another terminal, then\n"
            "authorise in the browser **on that laptop**):\n\n"
            f"  ssh -L {bind_port}:127.0.0.1:{bind_port} -N USER@THIS_HOST\n\n"
            "Google will redirect to http://127.0.0.1:%d/ — the tunnel forwards that to this machine.\n"
            % bind_port,
            file=sys.stderr,
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(sec), DRIVE_SCOPE)
    creds = flow.run_local_server(port=bind_port, prompt="consent")
    allow = drive_allowlist_normalized()
    if allow:
        em = drive_google_account_email(creds)
        if em is None or em not in allow:
            print(
                "That Google account is not on the allow list.\n"
                f"  Signed in as: {em or '(could not read email — try drive-auth again)'}\n"
                f"  Allowed: {', '.join(sorted(allow))}\n"
                "  Set PROJECTSCAN_DRIVE_ALLOWED_EMAILS (comma-separated) or sign in with an allowed address.",
                file=sys.stderr,
            )
            sys.exit(1)
    tpath = drive_token_path()
    tpath.parent.mkdir(parents=True, exist_ok=True)
    tpath.write_text(creds.to_json(), encoding="utf-8")
    try:
        tpath.chmod(0o600)
    except OSError:
        pass
    print(f"Saved OAuth token to {tpath}")


def filter_repos_subset(repos: list[dict], subset: str) -> list[dict]:
    if subset == "past_metaai":
        return [r for r in repos if isinstance(r, dict) and r.get("name", "").lower() > "metaai"]
    return [r for r in repos if isinstance(r, dict)]


def report_repo_plain_block(r: dict) -> list[str]:
    s = r.get("scores") or {}
    roi = r.get("roi_distribution") or {}
    mon = r.get("monetization") or {}
    lines = [
        "=" * 78,
        f"PROJECT: {r.get('name', '')}",
        f"PATH: {r.get('path', '')}",
        (
            "GIT:"
            f" last ~ {r.get('last_commit', '?')} | commits={r.get('commit_count')}"
            f" | files={r.get('file_count')}"
        ),
        (
            "SIGNALS:"
            f" readme={r.get('has_readme')} licence={r.get('has_license')}"
            f" package={r.get('has_package')} api_layout={r.get('has_api')}"
            f" docker={r.get('has_docker')}"
        ),
        f"TAXONOMY: market_tag={r.get('market_tag', '?')} | segment={r.get('market_segment', 'na')}",
        f"  audience_label: {(r.get('market_segment_label') or '').strip() or '—'}",
        f"DEMAND: evidence_pts={r.get('demand_evidence', '?')} · CONFIDENCE: {r.get('scoring_confidence', '?')}",
        (r.get("demand_hint") or "").strip(),
        "",
        "SCORES (0-100 heuristic):",
        f"  final_value (upside)    {s.get('value')}",
        f"  progress                {s.get('progress')}",
        f"  feature_potential       {s.get('feature_potential')}",
        f"  ship_monetise_ease      {s.get('effort_to_monetize')}",
        f"  gtm_readiness           {r.get('gtm_readiness', r.get('total_score'))}",
    ]
    dg = (r.get("data_gap") or "").strip()
    if dg:
        lines.append(f"  {dg}")
    rrk = r.get("risk_adjusted_rank")
    if rrk is not None:
        lines.append(f"  risk_adjusted_rank      {rrk}")
    sb = r.get("score_breakdown")
    if isinstance(sb, dict) and sb.get("schema") == "1.1":
        dem = sb.get("demand") or {}
        infra = sb.get("infra") or {}
        lines.extend(
            [
                "",
                "SCORE_BREAKDOWN (audit — Meta Cursor v1.1):",
                f"  demand_pts: {sb.get('demand_pts')}",
                f"    sources: {dem.get('sources')}",
                f"    demand_source_used: {dem.get('demand_source_used')}",
                f"    demand_combined_100: {dem.get('demand_combined_100')}",
                f"    formula: {dem.get('formula')}",
                f"  infra_pts: {sb.get('infra_pts')}",
                f"    breakdown: {infra.get('breakdown')}",
                f"    formula: {infra.get('formula')}",
            ]
        )
        if sb.get("problem_evidence_pts"):
            pe = sb.get("problem_evidence") or {}
            lines.append(f"  problem_evidence_pts: {sb.get('problem_evidence_pts')}")
            lines.append(f"    sources: {pe.get('sources')}")
        lines.extend(
            [
                f"  base_value: {sb.get('base_value')}",
                f"  recency_multiplier: {sb.get('recency_multiplier')} ({(sb.get('recency') or {}).get('reason')})",
                f"  final_value: {sb.get('final_value')}",
                f"  risk_flags: {', '.join(sb.get('risk_flags') or []) or '—'}",
                f"  confidence: {sb.get('confidence')}",
            ]
        )
    elif isinstance(sb, dict) and sb:
        lines.extend(["", "SCORE_BREAKDOWN (legacy snapshot):", f"  {sb}"])
    lines.extend(
        [
            "",
            "REVENUE_BAND (USD/year · P10 / P50 / P90):",
            (
                f"  P10 {int(r.get('money_usd_low')):,} · P50 {int(r.get('money_usd_mid') or r.get('money_usd_low') or 0):,} · P90 {int(r.get('money_usd_high')):,}"
                if r.get("money_usd_low") is not None and r.get("money_usd_high") is not None
                else "  (not computed — rescan or reload index)"
            ),
            "",
            "MONETISATION:",
            f"  {mon.get('headline', '')}",
        ]
    )
    for p in mon.get("paths") or []:
        lines.append(f"  · [{p.get('model', '?')}] {p.get('title')}: {p.get('detail')}")
    lines.extend(
        [
            "",
            "ROI DISTRIBUTION (playbook):",
            f"  {roi.get('pathway_title', '')}",
            f"  GTM: {roi.get('distribution_method', '')}",
            f"  Why: {roi.get('why_roi', '')}",
            f"  playbook={roi.get('playbook_id')} alt={roi.get('alternative_ids', '')}",
            "",
            "NOTES:",
            f"  monetization_notes: {(r.get('monetization_notes') or '').strip() or '(empty)'}",
            f"  manual_notes: {(r.get('manual_notes') or '').strip() or '(empty)'}",
            f"  importance={r.get('importance')} status={r.get('status')} hidden={r.get('hidden')}",
            "",
        ]
    )
    return lines


def build_report_bytes(fmt: str, subset: str, repos_in: list[dict]) -> tuple[bytes, str, str]:
    sel = sorted(filter_repos_subset(repos_in, subset), key=sort_key)
    subset_note = {"all": "All projects.", "past_metaai": "Lexical subset: repo name sorts after MetaAI."}.get(
        subset, subset
    )
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    if fmt == "txt":
        head = [
            "=" * 78,
            "projectscan — heuristic portfolio report",
            f"Subset: {subset_note}",
            f"Repositories: {len(sel)}",
            f"Projects root: {projects_dir()}",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            "=" * 78,
            "",
        ]
        chunks: list[str] = head
        if not sel:
            chunks.append("(No rows in subset.)\n")
        else:
            for r in sel:
                chunks.extend(report_repo_plain_block(r))
            chunks.append("")
        data = ("\n".join(chunks)).encode("utf-8")
        fname = f"projectscan_report_{stamp}.txt"
        return data, fname, "text/plain; charset=utf-8"

    if fmt == "md":
        parts = [
            "# projectscan heuristic report",
            "",
            f"- **Subset:** {subset_note}",
            f"- **Repos:** {len(sel)}",
            f"- **Root:** `{projects_dir()}`",
            f"- **Generated:** {datetime.now().isoformat(timespec='seconds')}",
            "",
        ]
        for r in sel:
            s = r.get("scores") or {}
            roi = r.get("roi_distribution") or {}
            mon = r.get("monetization") or {}
            parts.extend(
                [
                    f"## {r.get('name', '?')}",
                    "",
                    f"- Path: `{r.get('path')}`",
                    f"- Market tag: `{r.get('market_tag', '—')}` · segment: **`{r.get('market_segment', 'na')}`** — {(r.get('market_segment_label') or '').strip() or '—'}",
                    f"- Demand evidence (auto): **{r.get('demand_evidence', '—')}**/50 · Confidence: **{r.get('scoring_confidence', '—')}**",
                    f"- Signals: readme={r.get('has_readme')} lic={r.get('has_license')}"
                    f" pkg={r.get('has_package')} api={r.get('has_api')} docker={r.get('has_docker')}",
                    "",
                    "| metric | score |",
                    "| --- | ---: |",
                    f"| demand_evidence | {r.get('demand_evidence', '')} |",
                    f"| final_value | {s.get('value')} |",
                    f"| progress | {s.get('progress')} |",
                    f"| feature_potential | {s.get('feature_potential')} |",
                    f"| ship_ease | {s.get('effort_to_monetize')} |",
                    f"| **gtm_readiness** | **{r.get('total_score')}** |",
                    "",
                    "### Revenue band (USD/year — P10 / P50 / P90)",
                    "",
                    (
                        f"P10 **{int(r.get('money_usd_low')):,}** · P50 **{int(r.get('money_usd_mid') or r.get('money_usd_low') or 0):,}** · P90 **{int(r.get('money_usd_high')):,}** / year"
                        if r.get("money_usd_low") is not None and r.get("money_usd_high") is not None
                        else "_(not computed — rescan or reload index)_"
                    ),
                    "",
                    "### Monetisation",
                    "",
                    mon.get("headline", ""),
                    "",
                ]
            )
            for p in mon.get("paths") or []:
                parts.append(f"- **{p.get('title')}** ({p.get('model')}): {p.get('detail')}")
            parts.extend(
                [
                    "",
                    "### ROI distribution",
                    "",
                    f"- **{roi.get('pathway_title', '')}**",
                    f"- GTM: {roi.get('distribution_method', '')}",
                    f"- Why ROI: {roi.get('why_roi', '')}",
                    f"- playbook `{roi.get('playbook_id')}` · `{roi.get('alternative_ids')}`",
                    "",
                ]
            )
        data_str = "\n".join(parts) + "\n"
        return data_str.encode("utf-8"), f"projectscan_report_{stamp}.md", "text/markdown; charset=utf-8"

    if fmt == "json":
        raw = {"subset": subset, "subset_note": subset_note, "count": len(sel), "repos": sel}
        return (
            json.dumps(raw, indent=2).encode("utf-8"),
            f"projectscan_report_{stamp}.json",
            "application/json; charset=utf-8",
        )

    if fmt == "csv":
        buf = io.StringIO(newline="")
        w = csv.writer(buf)
        w.writerow(
            [
                "name",
                "market_tag",
                "demand_evidence",
                "market_segment",
                "market_segment_label",
                "scoring_confidence",
                "total_score",
                "value",
                "progress",
                "feature_potential",
                "effort_to_monetize",
                "money_usd_low",
                "money_usd_high",
                "monetization_headline",
                "roi_pathway_title",
                "roi_playbook_id",
                "importance",
                "status",
                "hidden",
                "commit_count",
                "path",
            ]
        )
        for r in sel:
            s = r.get("scores") or {}
            mon = r.get("monetization") or {}
            roi = r.get("roi_distribution") or {}
            w.writerow(
                [
                    r.get("name"),
                    r.get("market_tag", ""),
                    r.get("demand_evidence", ""),
                    r.get("market_segment", ""),
                    r.get("market_segment_label", ""),
                    r.get("scoring_confidence", ""),
                    r.get("total_score"),
                    s.get("value"),
                    s.get("progress"),
                    s.get("feature_potential"),
                    s.get("effort_to_monetize"),
                    r.get("money_usd_low"),
                    r.get("money_usd_high"),
                    mon.get("headline"),
                    roi.get("pathway_title"),
                    roi.get("playbook_id"),
                    r.get("importance"),
                    r.get("status"),
                    r.get("hidden"),
                    r.get("commit_count"),
                    r.get("path"),
                ]
            )
        return buf.getvalue().encode("utf-8"), f"projectscan_report_{stamp}.csv", "text/csv; charset=utf-8"

    raise ValueError(f"Unsupported format: {fmt!r}")


def drive_upload_payload(fmt: str, subset: str) -> tuple[dict, int]:
    """
    Build payload and upload. Returns ({ok,name,id,webViewLink or None, parents}, HTTP-like code).

    Raises RuntimeError / ValueError / HttpError depending on caller handling.
    """
    if subset not in REPORT_SUBSETS:
        raise ValueError(f"subset must be one of {REPORT_SUBSETS}")
    if fmt not in ("txt", "md", "json", "csv"):
        raise ValueError("format must be txt, md, json, or csv")

    Credentials, HttpError, _, MediaIoBaseUpload, _, google_build = import_google_clients()
    creds = drive_credentials_or_none()
    if not creds:
        raw = drive_stored_credentials_or_none()
        allow = drive_allowlist_normalized()
        if raw and allow:
            em = drive_google_account_email(raw)
            if em is not None and em not in allow:
                raise RuntimeError(
                    f"Google account {em} is not allowed. Allowed: {', '.join(sorted(allow))}. "
                    "Delete the token file and run drive-auth with an allowed account, or adjust "
                    "PROJECTSCAN_DRIVE_ALLOWED_EMAILS."
                )
        raise RuntimeError("Not signed in. Run: python projectscan.py drive-auth")

    repos = load_repos()
    normalize_loaded_repos(repos)
    blob, fname, mime = build_report_bytes(fmt, subset, repos)

    folder_env = os.environ.get("PROJECTSCAN_DRIVE_FOLDER_ID", "").strip()
    body_base: dict = {"name": fname}
    if folder_env:
        body_base["parents"] = [folder_env]

    service = google_build("drive", "v3", credentials=creds, cache_discovery=False)
    media = MediaIoBaseUpload(io.BytesIO(blob), mimetype=mime.split(";")[0].strip(), resumable=False)
    try:
        created = (
            service.files()
            .create(body=body_base, media_body=media, fields="id,name,mimeType,webViewLink,webContentLink")
            .execute()
        )
    except HttpError as exc:
        msg = getattr(exc, "error_details", None) or str(exc)
        raise RuntimeError(f"Drive API error: {msg}") from exc

    return {"ok": True, "format": fmt, "subset": subset, **created}, 200


def drive_status_summary() -> dict:
    secrets = drive_client_secrets_path()
    token = drive_token_path()
    out: dict = {
        "client_secrets_path": str(secrets.resolve()),
        "has_client_secrets": secrets.is_file(),
        "token_path": str(token.resolve()),
        "has_token": token.is_file(),
        "formats": ["txt", "md", "csv", "json"],
        "subsets": list(REPORT_SUBSETS),
        "hint": "pip install -r requirements-google.txt — place Desktop OAuth JSON as client_secrets.json — run: python projectscan.py drive-auth",
    }
    try:
        import_google_clients()
    except RuntimeError as e:
        out["libraries_available"] = False
        out["libraries_error"] = str(e)
        out["authenticated"] = False
        out["email_allowlist"] = []
        out["token_account_email"] = None
        return out
    out["libraries_available"] = True
    allow = drive_allowlist_normalized()
    out["email_allowlist"] = sorted(allow) if allow else []
    raw = drive_stored_credentials_or_none()
    token_email = drive_google_account_email(raw) if raw else None
    out["token_account_email"] = token_email
    email_ok = bool(raw) and (not allow or (token_email is not None and token_email in allow))
    out["email_allowlist_satisfied"] = email_ok
    out["authenticated"] = drive_credentials_or_none() is not None
    if raw and allow and token_email and token_email not in allow:
        out["email_gate_message"] = (
            f"Stored token is for {token_email}, not in PROJECTSCAN_DRIVE_ALLOWED_EMAILS. "
            "Remove google_drive_token.json or sign in again."
        )
    return out


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        # Quieter default; errors still print
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, obj: object) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self._send(status, raw, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, dashboard_html_document().encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/projects":
            repos = load_repos()
            normalize_loaded_repos(repos)
            self._json(
                HTTPStatus.OK,
                {"projects_root": str(projects_dir().resolve()), "repos": repos},
            )
            return
        if parsed.path == "/api/fxrates":
            self._json(HTTPStatus.OK, get_fx_payload())
            return
        if parsed.path == "/api/drive/status":
            self._json(HTTPStatus.OK, drive_status_summary())
            return
        if parsed.path == "/api/report/download":
            qs = parse_qs(parsed.query or "")
            fmt = (qs.get("format") or ["txt"])[0].lower().strip()
            subset = (qs.get("subset") or ["all"])[0].lower().strip()
            try:
                repos = load_repos()
                normalize_loaded_repos(repos)
                blob, fname, mime = build_report_bytes(fmt, subset, repos)
            except ValueError as e:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                return
            safe_name = fname.replace('"', "").replace("\\", "") or "projectscan_report"
            cd = f'attachment; filename="{safe_name}"'
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Content-Disposition", cd)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(blob)
            return
        self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
            return

        if parsed.path == "/api/scan":
            try:
                repos = scan_projects()
            except Exception as e:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
                return
            self._json(HTTPStatus.OK, {"ok": True, "count": len(repos)})
            return

        if parsed.path == "/api/drive/upload":
            fmt = str(body.get("format") or "txt").lower()
            subset = str(body.get("subset") or "all").lower()
            try:
                result, _ = drive_upload_payload(fmt, subset)
                self._json(HTTPStatus.OK, result)
            except ValueError as e:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except RuntimeError as e:
                msg = str(e)
                if "Not signed in" in msg or "drive-auth" in msg:
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": msg})
                elif "not allowed" in msg.lower():
                    self._json(HTTPStatus.FORBIDDEN, {"error": msg})
                else:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": msg})
            except Exception as e:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
            return

        if parsed.path.startswith("/api/project/"):
            name = unquote(parsed.path[len("/api/project/") :])
            repos = load_repos()
            by_name = {r["name"]: r for r in repos}
            if name not in by_name:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown project"})
                return
            r = by_name[name]
            if "importance" in body:
                try:
                    imp = max(1, min(5, int(body["importance"])))
                    r["importance"] = imp
                except (TypeError, ValueError):
                    pass
            if "status" in body and body["status"] in ("idea", "active", "paused", "shipped"):
                r["status"] = body["status"]
            if "manual_notes" in body and isinstance(body["manual_notes"], str):
                r["manual_notes"] = body["manual_notes"]
            if "manual_value_override" in body:
                mo = body["manual_value_override"]
                if mo is None or mo == "":
                    r["manual_value_override"] = None
                else:
                    try:
                        r["manual_value_override"] = max(0, min(100, int(mo)))
                    except (TypeError, ValueError):
                        pass
            if "hidden" in body:
                r["hidden"] = bool(body["hidden"])
            if "monetization_notes" in body and isinstance(body["monetization_notes"], str):
                r["monetization_notes"] = body["monetization_notes"]
            if "manual_money_low" in body and "manual_money_high" in body:
                lo, hi = body.get("manual_money_low"), body.get("manual_money_high")
                if lo is None and hi is None:
                    r["manual_money_low"] = None
                    r["manual_money_high"] = None
                elif lo is not None and hi is not None:
                    try:
                        rl = max(0, int(lo))
                        rh = max(rl, int(hi))
                        r["manual_money_low"] = rl
                        r["manual_money_high"] = rh
                    except (TypeError, ValueError):
                        r["manual_money_low"] = None
                        r["manual_money_high"] = None
                else:
                    r["manual_money_low"] = None
                    r["manual_money_high"] = None
            save_repos(repos)
            self._json(HTTPStatus.OK, {"ok": True})
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})


def serve(host: str, port: int, scan_first: bool) -> None:
    if scan_first:
        print("Scanning before serve…")
        scan_projects()
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard: http://{host}:{port}")
    print(f"Projects root: {projects_dir().resolve()}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


def main() -> None:
    serve_port_default = int(os.environ.get("PROJECTSCAN_PORT", "8765"))
    parser = argparse.ArgumentParser(description="Scan and prioritise local git projects.")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Run local dashboard (default host 127.0.0.1)")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    p_serve.add_argument(
        "--port",
        type=int,
        default=serve_port_default,
        help="Listen port (default: $PROJECTSCAN_PORT or 8765)",
    )
    p_serve.add_argument(
        "--scan",
        action="store_true",
        help="Run a full scan before opening the server",
    )

    sub.add_parser("scan", help="Scan only (same as running with no subcommand)")

    p_dauth = sub.add_parser("drive-auth", help="OAuth sign-in for Google Drive (writes token next to index)")
    p_dauth.add_argument(
        "--oauth-port",
        type=int,
        default=None,
        metavar="PORT",
        help="Loopback port for OAuth callback (0 = random). Use a fixed port with "
        "ssh -L PORT:127.0.0.1:PORT. Default: $PROJECTSCAN_DRIVE_OAUTH_PORT or random.",
    )

    p_dup = sub.add_parser("drive-upload", help="Upload heuristic portfolio report to Google Drive")
    p_dup.add_argument("--format", choices=["txt", "md", "csv", "json"], default="txt")
    p_dup.add_argument("--subset", choices=list(REPORT_SUBSETS), default="all")
    p_dup.add_argument(
        "--fresh-scan",
        action="store_true",
        help="Run a full filesystem scan before building the upload payload",
    )

    args = parser.parse_args()
    cmd = args.command

    if cmd == "serve":
        serve(args.host, args.port, args.scan)
        return

    if cmd == "drive-auth":
        drive_run_oauth_setup(oauth_loopback_port=getattr(args, "oauth_port", None))
        return

    if cmd == "drive-upload":
        if getattr(args, "fresh_scan", False):
            print("Scanning before upload…", file=sys.stderr)
            scan_projects()
        try:
            result, _ = drive_upload_payload(args.format, args.subset)
        except (ValueError, RuntimeError) as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result, indent=2))
        return

    # default: scan
    root = projects_dir()
    print(f"Scanning {root.resolve()}…")
    repos = scan_projects()
    print(f"\nDone. Found {len(repos)} repos.")
    print("Results:")
    print(f"  JSON: {index_file_json()}")
    print(f"  CSV:  {index_file_csv()}")
    print("\nTop by importance, then score:")
    for r in repos[:8]:
        if r.get("hidden"):
            continue
        print(f"  {r.get('importance', 3)}★  {r['total_score']:>5}  {r['name']}")


if __name__ == "__main__":
    main()
