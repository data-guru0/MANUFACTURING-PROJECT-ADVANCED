"""Clean up the read model WITHOUT touching Odoo (S2: W4, W5). Findings go to docs/fde/DATA_QUALITY.md.

- material names -> one canonical code: the BOM's material when the part has one, else the human-reviewed
  lookup (agent/config/material_map.yaml), else a fuzzy match marked "needs review";
- duplicate customers flagged (normalized names / same email);
- one `parts` row per quoted part (Odoo products + Excel-only quotes), with won/lost prices, actual
  minutes per unit from finished MOs, and a routing; a missing routing is ESTIMATED from the closest
  analog (same family and material) and visibly marked `estimated:<analog>`;
- a local embedding per part (fastembed BAAI/bge-small-en-v1.5; nothing leaves the machine).
"""

import difflib
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg
import yaml

MAP_FILE = Path(os.environ.get("APEX_CONFIG_DIR", "/app/agent-config")) / "material_map.yaml"
FUZZY_MIN = 0.75
DUP_MIN = 0.85
LEGAL = re.compile(r"\b(inc|ltd|llc|gmbh|co|corp|company|sa|srl)\b\.?")


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class MaterialMatcher:
    """Lookup first (reviewed), then fuzzy (needs review). match(text) -> (code | None, method, score)."""

    def __init__(self, map_file: Path = MAP_FILE) -> None:
        data = yaml.safe_load(map_file.read_text(encoding="utf-8"))
        self.lookup = {_key(v): code for code, variants in data["materials"].items() for v in [code, *variants]}

    def match(self, text: str) -> tuple[str | None, str, float]:
        k = _key(text)
        if not k:
            return None, "unknown", 0.0
        if k in self.lookup:
            return self.lookup[k], "lookup", 1.0
        best = max(self.lookup, key=lambda known: difflib.SequenceMatcher(None, k, known).ratio())
        score = difflib.SequenceMatcher(None, k, best).ratio()
        return (self.lookup[best], "fuzzy", round(score, 2)) if score >= FUZZY_MIN else (None, "unknown", round(score, 2))


def company_key(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue"))
    return " ".join(LEGAL.sub(" ", _key(ascii_name.encode("ascii", "ignore").decode())).split())


def records(conn: psycopg.Connection[Any], model: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, data FROM odoo_records WHERE model = %s ORDER BY id", (model,))
        return [{"id": i, **d} for i, d in cur.fetchall()]


def flag_duplicates(conn: psycopg.Connection[Any]) -> int:
    customers = [p for p in records(conn, "res.partner") if (p.get("customer_rank") or 0) > 0 and p.get("ref")]
    found = []
    for i, a in enumerate(customers):
        for b in customers[i + 1 :]:
            ka, kb = company_key(a["name"]), company_key(b["name"])
            sim = difflib.SequenceMatcher(None, ka, kb).ratio()
            same_mail = bool(a.get("email")) and a.get("email") == b.get("email")
            if sim >= DUP_MIN or same_mail:
                reason = f"name similarity {sim:.2f}" + ("; same email" if same_mail else "")
                found.append(
                    (b["ref"], b["name"], a["ref"], a["name"], round(sim, 2), reason)
                )  # the later record is the duplicate
    with conn.cursor() as cur:
        cur.execute("DELETE FROM duplicate_customers")
        cur.executemany("INSERT INTO duplicate_customers VALUES (%s, %s, %s, %s, %s, %s)", found)
    return len(found)


def build_parts(conn: psycopg.Connection[Any], matcher: MaterialMatcher, embed: bool = True) -> dict[str, int]:
    tmpl = {t["id"]: t for t in records(conn, "product.template")}
    prod_tmpl = {p["id"]: p["product_tmpl_id"][0] for p in records(conn, "product.product")}
    code_of = {t["id"]: t["default_code"] for t in tmpl.values()}
    mat_names = {t["default_code"]: t["name"] for t in tmpl.values() if str(t.get("default_code") or "").startswith("MAT-")}
    lines_by_bom: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for ln in records(conn, "mrp.bom.line"):
        lines_by_bom[ln["bom_id"][0]].append(ln)
    bom_by_tmpl = {b["product_tmpl_id"][0]: b for b in records(conn, "mrp.bom")}
    partners = {p["id"]: p for p in records(conn, "res.partner")}
    orders = {o["id"]: o for o in records(conn, "sale.order")}
    sales: dict[int, list[tuple[str, float, str]]] = defaultdict(list)  # tmpl id -> (state, price, customer ref)
    for ln in records(conn, "sale.order.line"):
        o = orders.get(ln["order_id"][0])
        if o and ln.get("product_id"):
            ref = str(partners.get(o["partner_id"][0], {}).get("ref") or "").removesuffix("-DUP")
            sales[prod_tmpl.get(ln["product_id"][0], -1)].append((o["state"], float(ln["price_unit"]), ref))
    minutes: dict[int, list[float]] = defaultdict(list)
    wo_by_mo: dict[int, float] = defaultdict(float)
    for w in records(conn, "mrp.workorder"):
        wo_by_mo[w["production_id"][0]] += float(w["duration"] or 0)
    for mo in records(conn, "mrp.production"):
        if mo["state"] == "done" and mo.get("product_qty"):
            minutes[prod_tmpl.get(mo["product_id"][0], -1)].append(wo_by_mo[mo["id"]] / float(mo["product_qty"]))

    parts: list[dict[str, Any]] = []
    aliases: dict[str, tuple[str | None, str, float, bool]] = {}
    for tid, t in tmpl.items():
        if not t.get("x_drawing_no"):
            continue
        bom = bom_by_tmpl.get(tid)
        mat_codes = [code_of.get(prod_tmpl.get(ln["product_id"][0], -1)) for ln in lines_by_bom.get(bom["id"], [])] if bom else []
        bom_mat = next((c for c in mat_codes if c and c.startswith("MAT-")), None)
        text = str(t.get("x_material_grade") or "")
        lk = matcher.match(text)
        if bom_mat:
            material, msrc = bom_mat, "bom"
            aliases[text] = (bom_mat, "bom" if lk[0] in (None, bom_mat) else "conflict", lk[2], lk[1] == "lookup")
        else:
            material, msrc = lk[0], lk[1]
            aliases[text] = (lk[0], lk[1], lk[2], lk[1] == "lookup")
        s = sales.get(tid, [])
        parts.append({
            "code": t["default_code"], "name": t["name"], "family": t.get("x_part_family"), "drawing": t["x_drawing_no"],
            "customer_ref": s[-1][2] if s else None, "material_code": material, "material_text": text, "material_source": msrc,
            "routing_ops": len(bom["operation_ids"]) if bom else 0, "routing_source": "odoo" if bom and bom["operation_ids"] else "none",
            "quotes": len(s), "won": sum(1 for st, *_ in s if st == "sale"), "last_price": s[-1][1] if s else None,
            "won_prices": [p for st, p, _ in s if st == "sale"], "lost_prices": [p for st, p, _ in s if st == "cancel"],
            "actual_min_per_unit": round(sum(minutes[tid]) / len(minutes[tid]), 2) if minutes.get(tid) else None, "source": "odoo",
        })  # fmt: skip
    # Excel-only quotes: parts Apex quoted that Odoo never heard of (W5)
    with conn.cursor() as cur:
        cur.execute("""SELECT drawing, customer, material_text, material_code, process, quoted_unit_price FROM share_quotes
                       WHERE excel_only ORDER BY path""")  # fmt: skip
        by_code = {p["code"]: p for p in parts}
        for drawing, customer, mtext, mcode, process, price in cur.fetchall():
            m = re.match(r"(D-\d+) Rev ([A-Z])", str(drawing))
            if not m:
                continue
            if (seen := by_code.get(f"{m[1]}-{m[2]}")) is not None:  # quoted again in Excel: one part, one more quote
                seen["quotes"] += 1
                seen["last_price"] = price if seen["source"] == "excel_only" else seen["last_price"]  # Odoo wins (W5)
                continue
            parts.append({"code": f"{m[1]}-{m[2]}", "name": f"{process} part {m[1]} Rev {m[2]} (Excel only)", "family": None,
                          "drawing": m[1], "customer_ref": customer, "material_code": mcode, "material_text": mtext,
                          "material_source": "lookup" if mcode else "unknown", "routing_ops": 0, "routing_source": "none",
                          "quotes": 1, "won": 0, "last_price": price, "won_prices": [], "lost_prices": [],
                          "actual_min_per_unit": None, "source": "excel_only"})  # fmt: skip
            by_code[parts[-1]["code"]] = parts[-1]
    # Estimate missing routings from the closest analog that has one (same family + material, else same family)
    with_routing = [p for p in parts if p["routing_source"] == "odoo"]
    estimated = 0
    for p in parts:
        if p["routing_source"] == "none" and p["family"]:
            pool = [a for a in with_routing if a["family"] == p["family"] and a["material_code"] == p["material_code"]] or \
                   [a for a in with_routing if a["family"] == p["family"]]  # fmt: skip
            if pool:
                analog = max(pool, key=lambda a: (a["quotes"], a["code"]))
                p["routing_ops"], p["routing_source"] = analog["routing_ops"], f"estimated:{analog['code']}"
                estimated += 1
    for p in parts:
        fam = str(p["family"] or "").replace("_", " ")
        p["search_text"] = " ".join(x for x in [p["name"], fam, mat_names.get(p["material_code"] or "", ""), p["material_text"],
                                               p["material_code"] or ""] if x)  # fmt: skip
    if embed:
        from apex_agent.knowledge.embed import embed_passages

        for p, v in zip(parts, embed_passages([p["search_text"] for p in parts]), strict=True):
            p["embedding"] = v
    with conn.cursor() as cur:
        cur.execute("DELETE FROM parts")
        for p in parts:
            cols = [c for c in p if c != "embedding"] + (["embedding"] if "embedding" in p else [])
            vals = [p[c] if c != "embedding" else "[" + ",".join(f"{x:.6f}" for x in p[c]) + "]" for c in cols]
            cur.execute(f"INSERT INTO parts ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})", vals)  # nosec B608 (column names from our own dict; values are parameters)
        cur.execute("DELETE FROM material_aliases")
        cur.executemany("INSERT INTO material_aliases VALUES (%s, %s, %s, %s, %s)",
                        [(raw, *v) for raw, v in aliases.items() if raw])  # fmt: skip
    conn.commit()
    return {"parts": len(parts), "excel_only": sum(1 for p in parts if p["source"] == "excel_only"),
            "routing_estimated": estimated, "material_aliases": len(aliases)}  # fmt: skip
