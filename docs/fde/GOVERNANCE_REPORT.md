# Governance report: AI cost, ROI and human oversight

_Generated 2026-11-23 12:41 UTC by `python -m apex_agent.governance.report` from the agent's database. Regenerate any time._

## Cost per quote
LLM spend per job is LiteLLM's own cost figure (`x-litellm-response-cost`), summed by the graph (S6). Canary copies are excluded.

| Metric | Value |
|---|---|
| AI spend, all jobs through the graph | $0.0209 over 23 jobs |
| AI $ per job that reached review | $0.0011 (18 jobs) |
| AI $ per quote sent | $0.0031 (2 sent) |
| AI $ per won order | $0.0061 (1 won, 1 lost) |

## ROI (assumptions from `policy.yaml -> governance.roi`)
- Estimator time per quote before: **150 min** (SOLUTION_DESIGN.md W1, from the L6 SOP walkthrough and Ramesh's estimate).
- Review time per quote now: **30 min** (the W1 target in SOLUTION_DESIGN.md, an assumption until S10 measures it).
- Estimator cost: **$25.0/h** (an assumption for the lab (loaded cost of an estimator in India, where the grid factor puts Apex); replace with Apex's figure).
- For the 2 quotes sent: **4.0 estimator hours saved ≈ $100.00**, against **$0.0061** of AI spend (≈ 16,367×).

## Human oversight (review screen decisions; canaries excluded)
Waiting time = from the moment the job reached review to the decision (queue time, not effort).

| Reviewer | Decisions | Edited or re-costed | Jobs re-costed | Median wait (min) | Canaries caught / missed |
|---|---|---|---|---|---|
| arjun | 1 | 0 (0 %) | 1 | 0.0 | 0 / 1 |
| ramesh | 1 | 0 (0 %) | 0 | 0.0 | 0 / 0 |

A reviewer who approves every canary is rubber-stamping (see `docs/governance/HUMAN_OVERSIGHT.md`).

## Budget alerts (50 / 80 / 100 % of an agent's 30-day budget)

| Agent | Threshold | Spend | Budget | When |
|---|---|---|---|---|
| (none: every agent is below 50 % of its budget) | | | | |
