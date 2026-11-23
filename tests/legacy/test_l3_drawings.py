"""L3 Done-when: drawings are attached to their Odoo products, and a random sample of the DXFs and PDFs
parses. Runs against the real generated files and the live Odoo; nothing is mocked."""

import hashlib
import json
import random
from pathlib import Path
from typing import Any

import ezdxf
import pymupdf
import pytest

from legacy.odoo.rpc import Odoo, env

ROOT = Path(__file__).resolve().parents[2]
GT = ROOT / "data" / "generated" / "ground_truth"
A3 = (1190.55, 841.89)  # points


def truth() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(GT.glob("*.json"))]


ALL = truth()
HISTORY = [g for g in ALL if g["kind"] == "history"]
RFQ = [g for g in ALL if g["kind"] != "history"]
SAMPLE = random.Random(3).sample(HISTORY, 15) + random.Random(4).sample([g for g in RFQ if g["kind"] != "repeat"], 15)


def pdf_text(g: dict[str, Any]) -> tuple[pymupdf.Document, str]:
    doc = pymupdf.open(ROOT / g["files"]["pdf"])
    return doc, "".join(page.get_text() for page in doc)


def test_every_part_has_a_drawing_and_ground_truth() -> None:
    assert len(HISTORY) == 353
    assert len(RFQ) == 80
    for g in ALL:
        assert {"dxf", "pdf"} <= set(g["files"]), g["drawing_id"]
        for rel in g["files"].values():
            assert (ROOT / rel).stat().st_size > 1000


@pytest.mark.parametrize("g", SAMPLE, ids=lambda g: g["drawing_id"])
def test_dxf_parses_with_dimensions_callouts_and_title_block(g: dict[str, Any]) -> None:
    doc = ezdxf.readfile(ROOT / g["files"]["dxf"])
    assert not doc.audit().has_errors
    msp = doc.modelspace()
    dims = msp.query("DIMENSION")
    assert len(dims) >= 3, "expected real DIMENSION entities"
    texts = " | ".join(t.plain_text() for t in msp.query("TEXT"))
    assert g["drawing_no"] in texts and "DRAWING NO" in texts and "MATERIAL" in texts
    if g["holes"]["count"] > 1 or (g["holes"]["count"] == 1 and g["family"] not in ("spacer", "spur_gear")):
        assert f"{g['holes']['count']}X Ø" in texts
    if g["threads"]["count"]:
        assert f"{g['threads']['count']}X " in texts
    if g["gdt"]:
        assert "POSITION" in texts
    assert ("INCH" in texts) == (g["units"] == "inch")
    assert ("SEE PURCHASE ORDER" in texts) == g["material_in_email"]


@pytest.mark.parametrize("g", SAMPLE, ids=lambda g: g["drawing_id"])
def test_pdf_opens_and_has_text_unless_scanned(g: dict[str, Any]) -> None:
    doc, text = pdf_text(g)
    assert len(doc) == g["sheets"]
    assert (doc[0].rect.width, doc[0].rect.height) == pytest.approx(A3, abs=1.0)  # A3 landscape
    if g["scan"]:
        assert text.strip() == "", "a scan must be image-only"
        assert doc[0].get_images(), "a scan must contain the page image"
    else:
        assert g["drawing_no"] in text
        assert ("ISO 2768-m" in text) == (g["units"] == "mm")
        if g["material_text_on_drawing"]:
            assert g["material_text_on_drawing"] in text


def test_variety_in_rfq_drawings() -> None:
    n = len(RFQ)
    assert 0.2 <= sum(g["scan"] for g in RFQ) / n <= 0.3
    assert 0.1 <= sum(g["units"] == "inch" for g in RFQ) / n <= 0.2
    assert 0.07 <= sum(g["material_in_email"] for g in RFQ) / n <= 0.13
    assert sum(g["sheets"] == 2 for g in RFQ) >= 6
    kinds = {k: sum(g["kind"] == k for g in RFQ) for k in ("new", "repeat", "revision", "out_of_capability")}
    assert kinds == {"new": 50, "repeat": 10, "revision": 12, "out_of_capability": 8}


def test_repeat_rfqs_resend_the_archived_drawing() -> None:
    by_id = {g["drawing_id"]: g for g in ALL}
    identical = 0
    for g in (g for g in RFQ if g["kind"] == "repeat"):
        src = by_id[g["source_part"]]
        same = hashlib.sha256((ROOT / g["files"]["pdf"]).read_bytes()).digest() == hashlib.sha256(
            (ROOT / src["files"]["pdf"]).read_bytes()).digest()  # fmt: skip
        assert same != g["scan"], g["drawing_id"]
        identical += same
    assert identical == 7


def test_step_models_match_the_drawing_envelope() -> None:
    import cadquery as cq

    with_step = [g for g in ALL if "step" in g["files"]]
    assert 0.25 <= sum(1 for g in HISTORY if "step" in g["files"]) / len(HISTORY) <= 0.4
    for g in random.Random(5).sample(with_step, 6):
        bb = cq.importers.importStep(str(ROOT / g["files"]["step"])).val().BoundingBox()
        dims = sorted([bb.xlen, bb.ylen, bb.zlen])
        assert dims == pytest.approx(sorted(g["envelope_mm"]), abs=0.01), g["drawing_id"]


def test_drawings_are_attached_to_their_odoo_products() -> None:
    o = Odoo("office", env("ODOO_OFFICE_PASSWORD"))
    tmpl = {r["default_code"]: r["id"] for r in o.call("product.template", "search_read",
            [("x_seed_batch", "=", "L2-history")], fields=["default_code"])}  # fmt: skip
    atts = o.call("ir.attachment", "search_read", [("res_model", "=", "product.template"),
                  ("res_id", "in", list(tmpl.values()))], fields=["res_id", "name", "mimetype", "file_size"])  # fmt: skip
    names = {(a["res_id"], a["name"]) for a in atts}
    for g in HISTORY:
        for rel in g["files"].values():
            assert (tmpl[g["part_code"]], Path(rel).name) in names, rel
    assert all(a["file_size"] > 1000 for a in atts)
    assert {a["mimetype"] for a in atts if a["name"].endswith(".pdf")} == {"application/pdf"}


def test_nist_reference_drawings_are_present_and_open() -> None:
    nist = sorted((ROOT / "data" / "reference" / "nist").glob("nist_*.pdf"))
    assert len(nist) == 11, "run: make nist"
    for p in nist:
        doc = pymupdf.open(p)
        assert len(doc) >= 1 and doc[0].get_text().strip()
    assert "without any restrictions" in (ROOT / "data" / "reference" / "nist" / "LICENSE_NOTES.md").read_text(encoding="utf-8")
