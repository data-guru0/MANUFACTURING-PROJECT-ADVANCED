"""L3: attach each history part's drawing files (PDF, DXF, STEP if any) to its Odoo product.

Idempotent: an attachment is identified by (product, file name). Uses the same JSON-RPC as everything else.
RFQ drawings are NOT attached: those parts aren't in Odoo until someone quotes them.

Run: uv run python -m legacy.seed.attach_drawings   (after legacy.seed.drawings and the L2 load)
"""

import base64
import json
import time

from legacy.odoo.rpc import Odoo, env
from legacy.seed.drawings import OUT, ROOT

MIME = {".pdf": "application/pdf", ".dxf": "image/vnd.dxf", ".step": "model/step"}


def main() -> None:
    t0 = time.monotonic()
    o = Odoo("admin", env("ODOO_ADMIN_PASSWORD"))
    tmpl = {r["default_code"]: r["id"] for r in o.call("product.template", "search_read",
            [("x_seed_batch", "=", "L2-history")], fields=["default_code"])}  # fmt: skip
    have = {(a["res_id"], a["name"]) for a in o.call("ir.attachment", "search_read", [
        ("res_model", "=", "product.template"), ("res_id", "in", list(tmpl.values()))], fields=["res_id", "name"])}  # fmt: skip
    batch: list[dict[str, object]] = []
    created = 0
    for code, tid in sorted(tmpl.items()):
        gt_path = OUT / "ground_truth" / f"{code}.json"
        if not gt_path.exists():
            raise FileNotFoundError(f"{gt_path} missing: run `python -m legacy.seed.drawings` first")
        for rel in json.loads(gt_path.read_text(encoding="utf-8"))["files"].values():
            f = ROOT / rel
            if (tid, f.name) in have:
                continue
            batch.append({"name": f.name, "res_model": "product.template", "res_id": tid, "mimetype": MIME[f.suffix],
                          "datas": base64.b64encode(f.read_bytes()).decode()})  # fmt: skip
            if len(batch) == 40:
                o.call("ir.attachment", "create", batch, timeout=300)
                created += len(batch)
                batch = []
    if batch:
        o.call("ir.attachment", "create", batch, timeout=300)
        created += len(batch)
    total = o.call("ir.attachment", "search_count", [("res_model", "=", "product.template"),
                                                     ("res_id", "in", list(tmpl.values()))])  # fmt: skip
    print(f"drawing attachments: {total} on {len(tmpl)} products ({created} created) in {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    main()
