# Costing, delivery date and CO2 evaluation (S5)

_Generated 2026-11-23 17:15 by `python -m apex_agent.costing.evaluate`. Held-out = parts the cycle-time model never saw (a fixed 20 % of parts). Model 20261123-105118._

## 1. Price vs what Apex quoted (held-out history)

Median absolute % error: **15.5%** over 80 quotes (won quotes only: 20.8%). Target ≈ 12 %.

| family | quotes | median error |
|---|---|---|
| flanged_housing | 7 | 20.6% |
| keyed_shaft | 11 | 7.9% |
| l_bracket | 9 | 15.1% |
| mounting_plate | 10 | 20.5% |
| sheet_panel | 13 | 10.2% |
| spacer | 10 | 10.7% |
| spur_gear | 14 | 39.7% |
| u_bracket | 6 | 20.7% |

Known causes of the big gaps: **spur gears**: the teeth are cut on a secondary mill, but a drawing shows no tooth count (S4 can't read it), so the engine misses that time; **flanged housings**: the lab's STEP models are simplified solid blocks, which makes the calibrated fill factor wrong (0.99 instead of ≈0.4).

The history's prices are Ramesh's spreadsheet plus his gut (±10 %), on the material price of that day; the engine uses the same day's supplier price, the drawing as read by S4, and the learned cycle times. A gap here is not automatically the engine's error: the history itself was under-priced on some families (W14).

## 2. Machine cost vs what the job really took

Median absolute % error of setup + run cost per part: **24.7%** over 17 finished held-out jobs (actual work-order minutes x the Odoo rate).

Cycle-time model on held-out work orders: P50 error **12.9%** vs standard times 11.4%; P10-P90 band holds the actual **59.4%** of the time (64 work orders, 16 parts).

## 3. Delivery date backtest

Committed date (P90 + 2 days buffer, against the other orders' work open at confirmation) met: **44.2%** of 86 delivered orders. The flat '3 weeks' Apex promises today: 74.4%. **The committed dates are shorter** (median 15.0 days after confirmation vs 21) and are kept LESS often: the plan is optimistic against the history's real delivery times. S14's expected load pushes dates out; the planner's buffer is a policy knob (`lead_buffer_days`).

Caveat: the history's real delivery dates were not produced by a capacity schedule, so this measures how often the committed date would have been kept, not a perfect replay of the shop floor.

| order | confirmed | committed | delivered | met | bottleneck |
|---|---|---|---|---|---|
| S00330 | 2026-06-03 | 2026-06-17 | 2026-07-10 | **no** | SAW |
| S00335 | 2026-06-26 | 2026-07-11 | 2026-07-12 | **no** | LASER |
| S00339 | 2026-06-29 | 2026-07-17 | 2026-07-14 | yes | LATHE |
| S00336 | 2026-07-10 | 2026-07-23 | 2026-07-22 | yes | SAW |
| S00348 | 2026-07-30 | 2026-08-22 | 2026-08-15 | yes | MILL3 |
| S00346 | 2026-08-01 | 2026-08-14 | 2026-08-10 | yes | SAW |
| S00349 | 2026-08-07 | 2026-08-20 | 2026-08-23 | **no** | SAW |
| S00350 | 2026-08-08 | 2026-08-21 | 2026-09-20 | **no** | SAW |
| S00347 | 2026-08-13 | 2026-08-27 | 2026-08-31 | **no** | SAW |
| S00356 | 2026-08-21 | 2026-08-30 | 2026-08-31 | **no** | SAW |
| S00359 | 2026-09-03 | 2026-10-08 | 2026-09-16 | yes | MILL3 |
| S00357 | 2026-09-09 | 2026-10-09 | 2026-10-03 | yes | MILL3 |
| S00367 | 2026-10-08 | 2026-11-12 | 2026-11-01 | yes | LATHE |
| S00370 | 2026-10-09 | 2026-10-24 | 2026-11-05 | **no** | LATHE |
| S00382 | 2026-10-16 | 2026-10-31 | 2026-11-06 | **no** | LASER |

## 4. CO2 for one part, by hand

Part **D-10042-A**, lot of 25: **6.3852 kg CO2e per part**, 159.63 kg per lot.

| contribution | calculation | kg CO2e | source |
|---|---|---|---|
| material MAT-POM | 0.431 kg bought (stock 0.399 kg x scrap 1.08) x 7.3 kg CO2e/kg | 3.1434 | Carbonfact material database, polyoxymethylene average 7.3 kg CO2e/kg (boundaries not verified) (proxy) |
| machine energy | 2.17 min x 2.2 kW (SAW) + 12.10 min x 11.0 kW (LATHE) + 11.43 min x 11.0 kW (MILL3) + 1.63 min x 1.5 kW (DEBURR) + 0.70 min x 2.0 kW (CMM) = 4.459 kWh x 0.727 kg CO2e/kWh (IN grid) | 3.2418 | Ember, India grid emission factor 727 g CO2e/kWh (lifecycle), 'From fossil to flexible' analysis (verified) |

Excluded (no citable factor): none

_Estimate following the GHG Protocol Product Standard approach: purchased material (stock weight incl. scrap) x cradle-to-gate factor + machine energy (machine hours x rated power) x grid factor + outside processes where a factor is known. Not a certified product carbon footprint (no third-party verification, industry-average factors)._

## Worst price gaps (first 15)

| part | qty | quoted | engine | error |
|---|---|---|---|---|
| D-10042-A | 500 | 293.82 | 41.17 | 86% |
| D-10012-A | 1 | 227.30 | 419.51 | 85% |
| D-10240-A | 10 | 137.34 | 236.25 | 72% |
| D-10042-A | 10 | 288.67 | 82.14 | 72% |
| D-10194-A | 1 | 1166.16 | 1994.39 | 71% |
| D-10105-B | 50 | 147.83 | 56.67 | 62% |
| D-10014-B | 25 | 197.95 | 80.64 | 59% |
| D-10293-A | 25 | 232.76 | 96.59 | 59% |
| D-10018-A | 10 | 156.69 | 241.43 | 54% |
| D-10305-A | 50 | 197.06 | 299.20 | 52% |
| D-10088-B | 250 | 31.84 | 47.07 | 48% |
| D-10234-A | 50 | 112.06 | 60.29 | 46% |
| D-10291-A | 10 | 163.32 | 96.41 | 41% |
| D-10105-A | 10 | 169.56 | 104.42 | 38% |
| D-10078-A | 100 | 137.85 | 85.67 | 38% |
