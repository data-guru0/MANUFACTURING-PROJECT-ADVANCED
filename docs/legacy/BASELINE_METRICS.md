# Baseline metrics: Apex quoting today (as-is)

_Generated 2026-11-23 16:52 by `scripts/baseline_metrics.py` from the running legacy system: IMAP (rfq@ Archive and Sent) and Odoo. Regenerate any time; do not edit the numbers by hand._

## Headline numbers
| Metric | Value | Source | Weakness |
|---|---|---|---|
| Quotes measured | 400 of 400 | IMAP Archive ↔ Sent, matched by customer ref | |
| **Median response time** (RFQ in → quote out) | **4.9 days** | IMAP INTERNALDATE | W1 |
| **p90 response time** | **7.2 days** | IMAP | W1 |
| **Win rate** (won / decided) | **27.2 %** (106 won, 284 lost, 10 open) | Odoo | |
| **On-time delivery** | **71.4 %** (75 of 105 delivered orders) | Odoo delivered vs promised date | W7 |
| **Lost quotes with a known reason** | **20.1 %** (57 of 284) | Odoo `x_lost_reason` | W9 |
| **Loss-making jobs** | **20 of 101 finished jobs** (total loss $62,719) | Odoo POs + work-order time x rate + 15 % | W8, W11, W14 |
| RFQs waiting in the shared inbox now | 20 (oldest 0.0 days) | IMAP INBOX | W1 |
| Mail and ERP agree on the dates | 100.0 % of quotes | IMAP vs Odoo `x_rfq_received_at` / `x_quote_sent_at` (a sanity check) | |

## Win rate by response time
Faster answers win more often. This is the business case for W1.

| Response time | Decided quotes | Won | Win rate |
|---|---|---|---|
| < 1 day | 3 | 3 | 100.0 % |
| 1-3 days | 56 | 19 | 33.9 % |
| 3-5 days | 147 | 51 | 34.7 % |
| 5-10 days | 183 | 33 | 18.0 % |
| > 10 days | 1 | 0 | 0.0 % |

## The 10 worst jobs (actual cost vs price)
| Sales order | Price | Actual cost | Result |
|---|---|---|---|
| S00119 | $131,985.00 | $151,376.79 | −$19,391.79 |
| S00204 | $45,900.00 | $59,648.94 | −$13,748.94 |
| S00121 | $207,167.00 | $218,405.35 | −$11,238.35 |
| S00195 | $69,505.00 | $76,187.73 | −$6,682.73 |
| S00128 | $51,721.50 | $56,952.32 | −$5,230.82 |
| S00299 | $14,770.00 | $17,375.29 | −$2,605.29 |
| S00126 | $9,853.00 | $11,645.50 | −$1,792.50 |
| S00154 | $5,663.00 | $6,162.73 | −$499.73 |
| S00022 | $5,734.00 | $6,149.60 | −$415.60 |
| S00057 | $16,868.00 | $17,064.41 | −$196.41 |

## How each number is computed
- **Response time:** for each historical quote, the INTERNALDATE of the customer's RFQ in `rfq@/Archive` and of Apex's quotation in `rfq@/Sent`, matched by the customer reference (`HIST-xxxx`) in the subject. Calendar days.
- **Win rate:** confirmed sales orders / (confirmed + cancelled) among the historical quotes.
- **On-time:** a won order is on time if its delivery was validated on or before the promised `commitment_date`.
- **Actual job cost:** all confirmed purchase orders for the job (material, subcontracting) + the recorded work-order minutes x the work center's hourly rate, + 15 % overhead. Only finished jobs (manufacturing order done).

<!-- students-log: everything below this line is kept when the report is regenerated -->






































































## Students' hand-quoting log (fill in during class)
Quote these three RFQs from `rfq@` **by hand**, following `SOP_MANUAL_QUOTING.md`, and write the minutes per step.

| Step (SOP) | RFQ D-10305-B (Rookfield, revision) | Anfrage D-20031-A (Krämer, German) | RFQ D-10227-A (Zephyr, repeat) |
|---|---|---|---|
| 1. Find and read the email | | | |
| 2. Read the drawing | | | |
| 3. Search Odoo for similar parts | | | |
| 4. Fill in the calculator | | | |
| 5. Ask Ramesh / apply his notes | | | |
| 6. Decide the delivery date | | | |
| 7. Enter the quote in Odoo | | | |
| 8. Email the quote | | | |
| **Total minutes** | | | |
