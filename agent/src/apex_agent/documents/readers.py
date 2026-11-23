"""The exact readers (S4): DXF (ezdxf), vector PDF (PyMuPDF text layer), STEP (OpenCascade, CadQuery's kernel).
Each returns {field: fact-dict} for one file. No LLM here: these are the most exact sources.
"""

import io
import tempfile
from pathlib import Path
from typing import Any

import pymupdf

from apex_agent.documents.callouts import PLAIN_NUMBER, TextItem, _f, parse
from apex_agent.security.content import MIN_PT

PDF_PAGES = 4


def read_dxf(data: bytes, name: str) -> dict[str, dict[str, Any]]:
    from ezdxf import recover

    doc, _auditor = recover.read(io.BytesIO(data))  # tolerant of messy customer files (and CRLF line ends)
    msp = doc.modelspace()
    items = [TextItem(t.dxf.text, t.dxf.insert.x, t.dxf.insert.y) for t in msp.query("TEXT")]
    items += [TextItem(t.plain_text(split=False).replace("\n", " "), t.dxf.insert.x, t.dxf.insert.y) for t in msp.query("MTEXT")]
    dims: list[TextItem] = []
    for d in msp.query("DIMENSION"):
        ov = d.override()
        shown = float(d.get_measurement()) * float(ov.get("dimlfac") or 1.0)
        mid = d.dxf.text_midpoint
        vertical = abs((float(d.dxf.get("angle", 0.0)) % 180) - 90) < 1
        text = f"{'Ø' if d.dimtype in (3, 35) else ''}{shown:.4f}".rstrip("0").rstrip(".")
        if ov.get("dimtol"):
            text += f"±{float(ov.get('dimtp') or 0):.6f}"
        dims.append(TextItem(text, mid.x, mid.y, vertical))
    units_code = doc.header.get("$INSUNITS", 4)
    facts = parse(items + dims, f"dxf:{name}", 0.97, dims=[d for d in dims if not d.text.startswith("Ø")])
    facts.setdefault("units", _f("mm" if units_code == 4 else "inch", f"dxf:{name}", 0.6, "from $INSUNITS"))
    return facts


def pdf_items(data: bytes) -> tuple[list[TextItem], int]:
    """Readable text spans with positions (y up, points). Text the security gate calls hidden (white, transparent,
    tiny, off-page) is left out: an attacker's instruction never becomes a 'fact'."""
    items: list[TextItem] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pages = doc.page_count
        for n, page in enumerate(doc.pages(0, min(PDF_PAGES, pages))):
            h = page.rect.height
            for s in page.get_texttrace():
                white = tuple(round(x, 2) for x in s.get("color", (0, 0, 0))) == (1.0, 1.0, 1.0)
                if s.get("opacity", 1) == 0 or s["size"] < MIN_PT or white or not page.rect.intersects(pymupdf.Rect(s["bbox"])):
                    continue
                x0, y0, x1, y1 = s["bbox"]
                items.append(TextItem("".join(chr(c[0]) for c in s["chars"]), (x0 + x1) / 2, h - (y0 + y1) / 2,
                                      abs(s["dir"][0]) < 0.5, n))  # fmt: skip
    return items, pages


def read_pdf(data: bytes, name: str) -> dict[str, dict[str, Any]]:
    items, pages = pdf_items(data)
    # No dimension text at all (a scan, or annotations drawn as graphics, like NIST's PMI drawings): the OCR and
    # vision path takes it.
    if not any(PLAIN_NUMBER.match(i.text.split("±")[0]) or "±" in i.text or "Ø" in i.text for i in items):
        return {}
    facts = parse(items, f"pdf:{name}", 0.9)
    facts["sheets"] = _f(pages, f"pdf:{name}", 0.95)
    return facts


def read_step(data: bytes, name: str) -> dict[str, dict[str, Any]]:
    """Bounding box (the size of the blank), volume (the weight, with density) and through-hole count."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.GProp import GProp_GProps
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "part.step"
        path.write_bytes(data)
        r = STEPControl_Reader()
        if r.ReadFile(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
            return {}
        r.TransferRoots()
        shape = r.OneShape()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    x0, y0, z0, x1, y1, z1 = box.Get()
    size = sorted([x1 - x0, y1 - y0, z1 - z0], reverse=True)
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    # Holes: cylindrical faces whose material is OUTSIDE the cylinder (reversed orientation), grouped by axis.
    holes: set[tuple[float, float, float]] = set()
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        surf = BRepAdaptor_Surface(face)
        if surf.GetType() == GeomAbs_Cylinder and face.Orientation() == TopAbs_REVERSED:
            c = surf.Cylinder()
            loc, r_ = c.Location(), c.Radius()
            if 2 * r_ < 0.5 * size[1]:  # a bore as big as the part is the part's own outside, not a hole
                holes.add((round(loc.X(), 1), round(loc.Y(), 1), round(r_, 2)))
        exp.Next()
    src = f"step:{name}"
    return {"step_bbox_mm": _f([round(v, 2) for v in size], src, 0.99, "sorted, largest first"),
            "step_volume_cm3": _f(round(props.Mass() / 1000, 3), src, 0.99),
            "step_holes": _f(len(holes), src, 0.8, "cylindrical holes by axis; threads count as holes")}  # fmt: skip
