TASK: Upgrade projectscan evaluation schema to v1.1 - fix scoring opacity, demand math, and revenue logic

CONTEXT: v1.0 added SCORE_BREAKDOWN and caps, but infra_pts and pain_pts are black boxes, demand_combined_100 incorrectly averages None values, and revenue bands ignore traction. Goal: make every score auditable and tie ARR estimates to evidence.

REQUIREMENTS:

1. EXPAND SCORE_BREAKDOWN TO BE FULLY AUDITABLE
   Replace current compact format with explicit sub-scores:

   SCORE_BREAKDOWN (audit — scoring_guidance.md):
     demand_pts: 18
       sources: {stars_log: 35.707, npm_log: None, waitlist: None}
       demand_source_used: stars_log
       demand_combined_100: 35.707
       formula: demand_pts = min(50, round(combined_100/100*50))
     infra_pts: 15
       breakdown: {package: 10, api_layout: 0, docker: 0, commits_gt_50: 0, readme_licence: 5}
       formula: sum(breakdown) capped_at 35
     problem_evidence_pts: 5
       sources: {manual_notes: 0, github_issues_tagged_pain: 5, external_mentions: 0}
     base_value: 38
       formula: demand_pts + infra_pts + problem_evidence_pts
     recency_multiplier: 1.0
       reason: last_commit_days=3, threshold=30
     final_value: 38
       formula: min(100, base_value * recency_multiplier)
     caps_applied: []
     risk_flags: []
     confidence: Med

2. FIX demand_combined_100 CALCULATION
   Current bug: combined = 0.6*stars_log + 0.4*npm_log returns low score when npm_log=None.
   New logic:
   def calc_demand_combined(signals):
       available = {k: v for k, v in signals.items() if v is not None}
       if not available: return 0.0
       normalized = [min(100, 20 * log10(v + 1)) for v in available.values()]
       return max(normalized)  # use best signal, don't average with zeros
   signals = {stars: stars_count, npm: npm_weekly_downloads, pypi: pypi_downloads, waitlist: waitlist_count}
   Log demand_source_used in SCORE_BREAKDOWN.

3. REPLACE HARD CAPS WITH RISK FLAGS
   Remove: game_cap29, demand_zero_cap40, demand_lt5_cap40 from caps_applied.
   Add instead:
   if market_tag == 'GAME' and demand_pts < 20:
       risk_flags.append('HIT_DRIVEN_VOLATILITY')
       confidence = 'Low'
   if demand_pts == 0:
       risk_flags.append('NO_MARKET_PULL')
       confidence = 'Low'
   if days_since_commit > 90:
       risk_flags.append('ABANDONMENT_RISK')
       final_value *= 0.5
   Never cap value. Surface risk, don't hide upside.

4. KILL OR RENAME weighted_total
   Option A: Delete weighted_total from output entirely. Sort by final_value.
   Option B: Rename to gtm_readiness = 0.7*ship_monetise_ease + 0.3*progress. Keep separate from value.
   Do not show two conflicting "total" scores.

5. MAKE REVENUE BANDS EVIDENCE-BASED
   Replace "illustrative" bands with Monte Carlo P10/P50/P90.
   
   REVENUE_ASSUMPTIONS:
     market_tag: DEVTOOL
     base_acv_usd_per_year: 1200  # $99/mo, from manual_notes or defaults
     conversion_factor: 5         # payers per demand_pt, varies by market_tag
     payers_p50: demand_pts * conversion_factor = 18 * 5 = 90
     churn_annual: 0.2
   
   REVENUE_BAND (USD/year):
     P10: 64,800   P50: 108,000   P90: 162,000
   
   If demand_pts == 0: show P10: 0  P50: 2,500  P90: 10,000 (no traction)
   Always log assumptions. If manual_notes has "LOI=$20k", override base_acv.

6. DEFINE problem_evidence_pts
   Remove generic pain_pts=5. New rules:
   problem_evidence_pts = 0
   +15 if manual_notes contains /waitlist|beta users|design partners/i
   +25 if manual_notes contains /LOI|letter of intent|prepaid|pilot \$\\d/i
   +1 per github_issue with labels [pain, bug, user-request], cap 10
   +10 if external_mentions > 5 from search
   If total == 0, omit line from SCORE_BREAKDOWN.

7. UPDATE CONFIDENCE LOGIC
   CONFIDENCE: High if demand_source_used != None AND infra_breakdown.readme_licence == 5
   CONFIDENCE: Med if demand_source_used != None OR infra_breakdown.readme_licence == 5
   CONFIDENCE: Low if both None OR risk_flags contains ABANDONMENT_RISK or HIT_DRIVEN_VOLATILITY

8. ADD UNIT TESTS
   test_demand_combined_uses_best_signal: stars=60, npm=None -> combined=35.7, not 21.4
   test_game_not_capped: market_tag=GAME, demand_pts=40 -> final_value=40+, no cap at 29
   test_zero_demand_arr: demand_pts=0 -> P50_ARR <= 5000
   test_infra_breakdown_sums: sum(infra_breakdown.values()) == infra_pts
   test_recency_penalty: days_since_commit=120 -> final_value *= 0.5
   Fail CI if any test fails or if SCORE_BREAKDOWN missing required keys.

9. OUTPUT FORMAT CHANGES
   - Sort final report DESC by final_value, then DESC by confidence
   - Add RISK_ADJUSTED_RANK = rank after applying 0.5x multiplier for ABANDONMENT_RISK
   - Flag projects with manual_notes or monetization_notes empty: "DATA_GAP: No GTM hypothesis"

DELIVERABLES:
1. Diff of scoring.py implementing all above
2. Regenerated report for BoreDOOM, Brickwise, moneymakers showing before/after SCORE_BREAKDOWN
3. NOTES.md entry documenting v1.0 -> v1.1 schema changes and migration notes

CONSTRAINTS:
- Do not change input file format. Only change scoring logic and output schema.
- Maintain backward compat: old reports without SCORE_BREAKDOWN should still parse.
- All formulas must be logged in SCORE_BREAKDOWN. No hidden math.