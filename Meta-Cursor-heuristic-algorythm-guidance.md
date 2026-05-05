# Project Scoring Heuristics v2 - Training Doc for Cursor

You are helping Jonathan evaluate ~/Projects for monetization. Use these rules before assigning value, market, or effort scores. 

## 1. Core Principle: Demand > Code

Never score based on code artifacts alone. Code is cost. Revenue comes from painful problems + distribution. 
If there's no evidence of demand, cap `value` at 40/100 max.

## 2. Market Fit Taxonomy - Use this before scoring

Tag every repo with ONE primary market. If you pick wrong, the rest of the score is garbage.

| Market Tag | Definition | Red Flags for Misclassification |
| --- | --- | --- |
| `B2B_SAAS` | Companies pay to save time/money. Needs auth, billing, multi-tenant. | No org/team concept. Solo utility. |
| `B2C_SUBSCRIPTION` | Individuals pay for convenience/status/skill. Mobile-first often. | Requires enterprise sales. |
| `DEVTOOL` | Developers are the user. CLI, SDK, lib, plugin. | End users are non-technical. |
| `GAME` | Entertainment. Monetize via IAP, ads, cosmetics, UGC. | Pitching to "corporate training" unless explicitly designed. |
| `CONTENT` | Blog, docs, course. Monetize via ads, sponsorship, info products. | Has Dockerfile + API. |
| `INTERNAL_TOOL` | Built for you. Monetize only by productizing. | No docs, no onboarding. |

**Hard rule**: If repo name/description contains "game", "AR", "chore", "play", "fun" -> default to `GAME`. 
Never assign `B2B_SAAS` unless repo has `/enterprise`, `sso`, `saml`, or explicit B2B pricing.

Example failure: "AR chore game" -> `GAME`. Not `B2B_SAAS`. Corporate user base is ludicrous without evidence.

## 3. Value Scoring Rubric - Replace commit-based scoring

Start at 0. Add points only for evidence. Cap at 100.

**Demand Evidence: 0-50 pts**
+20: GitHub stars > 50 OR npm downloads > 500/week OR clear Reddit/HN threads asking for it
+15: You have >10 real users you didn't force to use it
+10: Existing competitors charge money for this
+5:  You use it weekly to solve your own problem
0:   No external evidence anyone wants this

**Monetization Infrastructure: 0-30 pts**
+15: Stripe/LemonSqueezy/Paddle code actually present and tested
+10: Auth + user accounts exist. Not just `admin/admin`
+5:  Pricing page or `/pricing` route exists
0:   No way to take money today

**Painkiller vs Vitamin: 0-20 pts**
+20: Solves "hair on fire" problem: data loss, legal, money, compliance
+10: Saves >2hrs/week for target user. You can name the user.
+5:  "Nice to have" quality-of-life improvement
0:   Fun/interesting but no clear ROI

**Auto-cap**: If `Demand Evidence = 0`, then `value = min(value, 40)`. Don't let polished code fool you.

## 4. Effort to Monetize - Replace `100 - progress`

Estimate days to first £1 of revenue if you worked full-time. Not "done", but "charging".

| Days | Score | What it means |
| --- | --- | --- |
| 0-2 | 100 | Add Stripe link to README, ship |
| 3-7 | 80 | Needs pricing page + basic auth |
| 8-30 | 60 | Needs payments, auth, landing page, 1 core feature |
| 31-90 | 40 | Needs rebuild for multi-tenant OR find first users |
| 90+ | 20 | Research project. No path without pivot |
| Unknown | 10 | You can't describe the customer |

## 5. Feature Potential - Kill this or redefine it

Old: "Small repo = high potential" is nonsense. 
New: `feature_potential` = # of validated user requests you have logged.

0 = no requests. 100 = 20+ people asked for specific paid features.
If you don't talk to users, set to 20 and stop guessing.

## 6. Project-Specific Overrides

**AR/Games**: 
1. Market is `GAME` unless proven otherwise.
2. Value depends on retention D1/D7/D30, not code. If no playtest data, value < 30.
3. Monetization = IAP, ads, UGC marketplace. If none planned, effort_to_monetize = 90+.
4. "Corporate training" angle requires: a) Existing corporate LOIs, b) SCORM/xAPI support, c) No fun.

**Devtools**: 
Value requires usage data. Check `npm`, `crates.io`, `pip` downloads. <100/week = hobby.

## 7. When Cursor runs project_scorer.py

Before outputting scores, run this checklist mentally:

1. Did I assign a `market_tag`? If not, STOP.
2. Is `value > 40` but `demand_evidence = 0`? Reduce value to 40.
3. For `GAME` projects, did I mention B2B? If yes, delete and apologize.
4. Can I name 3 people who would pay for this today? If no, flag `effort_to_monetize < 40`.

## 8. Output Format

When asked to score, return:
```
Repo: <name>
Market Tag: <tag> | Confidence: High/Med/Low
Value: X/100 | Why: <1 sentence citing demand evidence>
Effort to £1: X days | Blocker: <biggest missing piece>
Verdict: Kill / Explore / Double-down
```

If Verdict = Kill, say why in 1 line. No sunk-cost cope.

## 9. Code Integration Notes

Add this comment to the top of `analyze_repo()` in project_scorer.py:
```
# Before scoring, read HEURISTICS.md and apply Market Fit Taxonomy.
# Default GAME projects to market_tag='GAME'. Never suggest B2B without evidence.
# Cap value at 40 if demand_evidence = 0.
```

Example heuristics to add in code:
```
has_stripe = run_git_command(repo_path, ["grep", "-ri", "stripe"]) != ""
has_auth = any((repo_path / f).exists() for f in ["auth.ts", "clerk", "auth0"])
has_billing_file = (repo_path / "billing").exists() or (repo_path / "pricing.md").exists()
```

Example market tag detection:
```
name_lower = repo_path.name.lower()
if any(k in name_lower for k in ["game", "ar", "chore", "play"]):
    market_tag = "GAME"
elif (repo_path / "sso").exists() or (repo_path / "saml").exists():
    market_tag = "B2B_SAAS"
else:
    market_tag = "INTERNAL_TOOL"
```
