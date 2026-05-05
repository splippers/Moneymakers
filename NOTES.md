# Projectscan scoring notes

Update this file whenever **`WEIGHTS`** in `projectscan.py` or the demand / value caps in `_demand_evidence_points` / `analyze_repo` change materially (see `scoring_guidance.md` §7).

## 2026-05-05

- Demand blend when **both** GitHub stars and npm weekly resolve: **0.6 × stars_log + 0.4 × npm_log** (`scoring_guidance.md` §2); single-signal paths unchanged (stars-only or npm-only).
- **`market_segment` / `market_segment_label`**: consumer Quest/home AR README signals map GAME repos to **mass_market_computer_game** (“Computer game for the masses”).
- **`scoring_confidence`**, **`score_breakdown`** (demand audit + caps): `scoring_guidance.md` §6 / §6-style audit output.
- Not implemented yet from `scoring_guidance.md`: full market/product/GTM composite, Monte Carlo revenue bands, negative signals, manual_notes parsers, risk-adjusted sort.
