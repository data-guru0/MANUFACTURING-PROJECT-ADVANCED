# Security recommendations for Apex (S9)

**For:** Vikram (IT), with Meera (owner). **From:** the FDE team. **Date:** 2026-11-22.
**Scope:** the parts of Apex's own systems we found weak while integrating (evidence: `docs/legacy/PAIN_POINTS.md`). What
the agent itself does is already fixed on our side (below); the rest needs Apex's decision and Vikram's hands. We have
not changed Apex's legacy systems ourselves.

## Already done on the agent's side
| Weakness | What we did |
|---|---|
| W3 shared logins | The agent never uses the shared `office` Odoo login or the shared rfq@ password: it has its own accounts (`erp_reader` read-only, `erp_integration` drafts only; the Dovecot master user `agent`, logged as such). Every person who decides on a quote logs in as themselves (Keycloak), and every action is in a hash-chained audit log. |
| W16 share writable by all | The agent reads the share as `agent-ro` (read-only); it can't change or delete a quote sheet. |
| W18 drawings to suppliers | The agent sends nothing to suppliers (S17 adds supplier RFQs with confidentiality checks). |

## Recommended for Apex (in order of risk reduction per hour of work)
| # | Recommendation | Why (evidence) | Effort | Weakness |
|---|---|---|---|---|
| 1 | **Back up Odoo's database and the file share daily, off this machine, and test a restore** | No backup service exists; one disk failure loses 3 years of quotes and orders | ½ day (S19 delivers a scripted backup + restore drill) | W16 |
| 2 | **Give each estimator their own Odoo login** (Arjun, Ramesh…) and retire `office` | All 400 historical quotes show salesperson *Office (shared)*: nobody can tell who priced what | 1 hour + habit change | W3 |
| 3 | **Stop sharing the rfq@ password**: read it through personal accounts (shared folder / delegated access) | 403 quote emails left from one shared mailbox | 2 hours | W3 |
| 4 | **Make the share read-only for most people**, with named write accounts for the two who maintain the calculator, and turn the recycle bin on | `valid users = fileshare` (one shared read-write account), recycle bin off | 2 hours | W16 |
| 5 | **Never forward a customer's whole drawing to a supplier**; send only the needed view, title block removed, for NDA customers only after Sana approves | 3 forwards of "CONFIDENTIAL" drawings found in purchasing@'s Sent; 8 customers are NDA-restricted | a rule + S17's tool | W18 |
| 6 | **Rotate every shared password once** (Odoo `office`, rfq@, the share) after 2-4 are done | They have been known by everyone, including people who left | 30 min | W3, W16 |
| 7 | **Turn on multi-factor authentication** for Keycloak (the review screen) and Odoo admin | One password protects quote approval | 1 hour (Keycloak OTP is built in) | W3 |
| 8 | **Patch on a schedule**: the agent's image is scanned on every `make check` (trivy found and we fixed a zlib CVE); do the same monthly for Odoo and the mail server images | Unpatched images | 1 hour/month | W16 |

**What we need from Apex:** a decision on 1-7 (Meera), a maintenance window for 2-4 (Vikram), and the NDA list kept current in Odoo (Sana).
