"""docs/fde/DATA_QUALITY.md: what the read model found wrong in Apex's data (S2, W4/W5), for the client.
Nothing is fixed in Odoo by the agent; each finding has a recommendation for Apex to decide on.

Usage: python -m apex_agent.knowledge.report
"""

import os
from datetime import datetime
from pathlib import Path

from apex_agent.knowledge.db import connect

OUT = Path(os.environ.get("APEX_DOCS_DIR", "/app/docs-fde")) / "DATA_QUALITY.md"


def main() -> None:
    with connect() as conn, conn.cursor() as cur:

        def q(sql: str) -> list[tuple[object, ...]]:
            cur.execute(sql)
            return list(cur.fetchall())

        dups = q("SELECT ref, name, duplicate_of, duplicate_name, similarity, reason FROM duplicate_customers ORDER BY ref")
        aliases = q("SELECT canonical, raw, method, reviewed FROM material_aliases ORDER BY canonical, raw")
        conflicts = q("SELECT raw, canonical FROM material_aliases WHERE method = 'conflict'")
        unknown = q("SELECT raw, method, score FROM material_aliases WHERE canonical IS NULL OR NOT reviewed ORDER BY raw")
        n_parts, n_none, n_est = (int(str(x)) for x in q("""SELECT count(*), count(*) FILTER (WHERE routing_source = 'none'),
                                         count(*) FILTER (WHERE routing_source LIKE 'estimated:%') FROM parts
                                         WHERE source = 'odoo'""")[0])  # fmt: skip
        est = q("SELECT code, routing_source FROM parts WHERE routing_source LIKE 'estimated:%' ORDER BY code LIMIT 10")
        prices = q("SELECT material_code, share_price, odoo_price, diff_pct FROM price_conflicts ORDER BY diff_pct")
        excel = q("SELECT customer, drawing, quoted_unit_price FROM share_quotes WHERE excel_only ORDER BY customer, drawing")
        sync = q("SELECT source, last_run, seen_last, changed_last FROM sync_state ORDER BY source")
    variants: dict[str, list[str]] = {}
    for canonical, raw, _m, _r in aliases:
        variants.setdefault(str(canonical), []).append(str(raw))
    L = [
        "# Data quality report for Apex (S2)", "",
        f"_Generated {datetime.now():%Y-%m-%d %H:%M} by `apex_agent.knowledge.report` from the agent's read model. "
        "**Nothing here was changed in Odoo**: Odoo stays the system of record; each finding is a recommendation._", "",
        "## 1. Duplicate customers (W4)",
        f"{len(dups)} customer records look like a second copy of another one. Quotes are split between them, so history per customer is wrong.", "",
        "| Duplicate | Name | Same as | Name | Evidence |", "|---|---|---|---|---|",
        *[f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[5]} |" for r in dups], "",
        "**Recommendation:** merge each pair in Odoo (Contacts → select both → Action → Merge), keeping the older record.", "",
        "## 2. Material written many ways (W4)",
        f"The free-text material field on products holds {len(aliases)} different spellings. The read model maps each to one code "
        "(from the part's BOM first, else the reviewed lookup in `agent/config/material_map.yaml`).", "",
        "| Material | Spellings found |", "|---|---|",
        *[f"| {k} | {', '.join(sorted(v))} |" for k, v in sorted(variants.items())], "",
        f"Spellings that contradict the BOM: **{len(conflicts)}**. Spellings not yet in the reviewed list: **{len(unknown)}**"
        + ("" if not unknown else " (" + ", ".join(f"`{r[0]}` ({r[1]}, {r[2]})" for r in unknown) + ")") + ".", "",
        "**Recommendation:** replace the free-text field with a fixed list (or the BOM material) so it can't drift again.", "",
        "## 3. Products without a routing (W4)",
        f"{n_none + n_est} of {n_parts} quoted products have **no routing** in Odoo. For quoting, the read model estimates one from the "
        f"closest analog (same family and material) and marks it `estimated:` everywhere it is shown ({n_est} estimated).", "",
        "| Product | Routing used |", "|---|---|", *[f"| {r[0]} | {r[1]} |" for r in est],
        f"| … | ({max(0, n_est - len(est))} more) |", "",
        "**Recommendation:** Kavita adds the real routing to these BOMs; estimates disappear as routings appear.", "",
        "## 4. Stale price list on the share (W5)",
        "The calculator's material prices disagree with Odoo. **The agent always uses Odoo**; each conflict is logged.", "",
        "| Material | Share $/kg | Odoo $/kg | Difference |", "|---|---|---|---|",
        *[f"| {r[0]} | {float(str(r[1])):.2f} | {float(str(r[2])):.2f} | {float(str(r[3])):+.1f} % |" for r in prices], "",
        "**Recommendation:** retire `Material_Prices_2023.xlsx` and the calculator's price table; Tom maintains prices in Odoo only.", "",
        "## 5. Quotes that exist only in Excel (W5)",
        f"{len(excel)} quote files on the share have no Odoo quotation, so nobody searching Odoo can find them. "
        "The read model indexes them as analogs (marked `excel_only`).", "",
        "| Customer folder | Drawing | Quoted unit price |", "|---|---|---|",
        *[f"| {r[0]} | {r[1]} | {float(str(r[2])):.2f} |" for r in excel], "",
        "## 6. Sync status", "", "| Source | Last run | Read | Changed |", "|---|---|---|---|",
        *[f"| {r[0]} | {r[1]:%Y-%m-%d %H:%M} | {r[2]} | {r[3] if r[3] is not None else '-'} |" for r in sync],
    ]  # fmt: skip
    OUT.write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")
    print(f"data quality: {len(dups)} duplicates, {len(aliases)} material spellings, {n_none + n_est} products without routing, "
          f"{len(prices)} price conflicts, {len(excel)} Excel-only quotes -> {OUT.name}")  # fmt: skip


if __name__ == "__main__":
    main()
