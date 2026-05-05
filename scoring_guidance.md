TASK: Refactor projectscan value analysis heuristics for accuracy and auditability

CONTEXT: Current scoring underweights demand signals, overweights raw progress, and lacks transparent weighting. Revenue bands are not evidence-based. Output is not actionable for capital allocation.

REQUIREMENTS:

1. **Rewrite scoring model with explicit weights + sub-scores**
   Replace opaque `value` with composite:
   - market_score = 0.4 * TAM_estimate_norm + 0.3 * demand_evidence_norm + 0.3 * growth_trend_norm
   - product_score = 0.4 * progress_norm + 0.3 * feature_completeness_norm + 0.3 * tech_moat_norm  
   - gtm_score = 0.5 * ship_monetise_ease + 0.3 * channel_fit_norm + 0.2 * sales_cycle_norm
   - value = 0.5 * market_score + 0.3 * product_score + 0.2 * gtm_score
   - All sub-scores 0-100. Log formula + inputs in NOTES for auditability.

2. **Calibrate demand_evidence_pts properly**
   Current: GitHub stars only. New: 
   - stars_log = min(100, 20 * log10(stars + 1))
   - external_signals: reddit_posts, discord_members, search_trend, inbound_issues
   - demand_evidence_pts = 0.6 * stars_log + 0.4 * external_signals
   - If demand_evidence_pts < 5, cap market_score at 40 regardless of TAM.

3. **Replace revenue bands with Monte Carlo ranges**
   Inputs: TAM, ACV estimate, conversion_rate, sales_cycle_months, churn.
   Output: P10/P50/P90 ARR with assumptions listed. No more "22,200 — 87,300" without method.
   Use `manual_notes` to override ACV or conversion if known.

4. **Add negative signals**
   - maintenance_burden: lines_of_code / contributors, dependency_count
   - abandonment_risk: days_since_commit > 90 = -15 to value
   - competition_density: scrape market_tag competitors, if >10 direct = -10 to market_score

5. **Use manual_notes and monetization_notes**
   If `manual_notes` contains "waitlist=500" or "LOI=$20k", parse and boost demand_evidence_pts.
   If `monetization_notes` empty, flag in report: "NO GTM HYPOTHESIS - LOW CONFIDENCE".

6. **Output changes**
   - Add `SCORE_BREAKDOWN` table per project showing all sub-scores.
   - Add `CONFIDENCE: Low/Med/High` based on data completeness.
   - Sort final report by `risk_adjusted_value = value * (1 - abandonment_risk/100)`.

7. **Testing**
   Write unit tests: BoreDOOM with 0 stars should not exceed value=30. 
   Brickwise with 60 stars + package=True should exceed value=45.
   Fail CI if weights change without updating NOTES.md changelog.

DELIVERABLE: Commit diff + updated sample report for BoreDOOM and Brickwise showing before/after scores.