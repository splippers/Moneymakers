# Projectscan scoring notes

Update this file when **`gtm_readiness`** weights, demand aggregation, infra breakdown rules, recency handling, or heuristic revenue (**P10/P50/P90**) defaults change — same commit as the code (Meta Cursor **v1.1** §8).

The legacy weighted composite over **value / progress / feature_potential / effort_to_monetize** no longer drives **`total_score`**.

## 2026-05-05 — Schema v1.1 (`Meta-Cursor-heuristic-algorythm-guidance.md`)

- **Demand (0–50)**: single **best** resolved traction signal — `max` over per-channel `min(100, 20*log10(count+1))` for `stars`, `npm`, `pypi`, `waitlist` (missing legs are ignored, not averaged in as zero).
- **`final_value`**: `min(100, round((demand + infra + problem_evidence) × recency_multiplier))` — **no legacy hard caps** (`game_cap29`, etc.). Risk is **`risk_flags`** (e.g. `HIT_DRIVEN_VOLATILITY`, `NO_MARKET_PULL`, `ABANDONMENT_RISK`).
- **Infra**: auditable breakdown `{package, api_layout, docker, commits_gt_50, readme_licence}` summed and **capped at 35**.
- **Problem evidence**: `manual_notes` regex (+ GitHub labelled issues **stub**, external mentions **stub**).
- **`total_score` ≡ `gtm_readiness`**: **0.7 × ship_monetise_ease + 0.3 × progress** — separate from upside **`scores.value`**.
- **Revenue**: **`money_usd_low` / `money_usd_mid` / `money_usd_high`** = **P10 / P50 / P90** with **`revenue_assumptions`** JSON for audit.
- **`repos.csv`** (index + dashboard save) and **Download report → CSV**: columns **`gtm_readiness`**, **`risk_adjusted_rank`**, **`money_usd_mid`** (P50 between **`money_usd_low`** / **`money_usd_high`** P10/P90). Report CSV assigns **`risk_adjusted_rank`** within the exported subset.
- Still TODO vs broader `scoring_guidance.md`: deeper Monte Carlo parameterisation, live GitHub issue labels, competitor density, full market/product/GTM composite.

## Earlier (v1.0 snapshot)

- Prior demand used **0.6/0.4** stars/npm blend when both resolved; v1.1 replaces that with **best-signal** semantics per Meta Cursor file pulled 2026-05-05.
