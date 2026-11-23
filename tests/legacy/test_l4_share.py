"""L4 Done-when: the share is reachable over SMB, and a headless LibreOffice recalculation of the
calculator matches costing_model.py within 0.5 %. Runs against the live fileserver, LibreOffice and Odoo."""

import io
import random
import shutil
import subprocess
import uuid
from pathlib import Path

import openpyxl
import pytest
import smbclient

from legacy.odoo.rpc import Odoo, env
from legacy.seed import costing_model as cm
from legacy.seed.build_share import CALC_NAME, PORT, ROOT, SERVER, UNIT_PRICE_CELL, spec_from_sheet

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def smb() -> None:
    smbclient.register_session(SERVER, username="fileshare", password=env("FILESHARE_PASSWORD"), port=PORT)


def read(rel: str) -> bytes:
    with smbclient.open_file(ROOT + "\\" + rel, mode="rb", port=PORT) as f:
        data: bytes = f.read()
    return data


def quote_files() -> list[str]:
    out = []
    for cust in smbclient.listdir(ROOT + r"\Customers", port=PORT):
        out += [f"Customers\\{cust}\\{f}" for f in smbclient.listdir(ROOT + rf"\Customers\{cust}", port=PORT)]
    return sorted(out)


def test_share_is_reachable_over_smb_with_the_expected_files() -> None:
    top = set(smbclient.listdir(ROOT, port=PORT))
    assert {CALC_NAME, "Pricing_Calculator_v7_FINAL.xlsx", "Material_Prices_2023.xlsx", "Machine_Rates.xlsx",
            "Customers"} <= top  # fmt: skip
    assert 140 <= len(quote_files()) <= 160


def test_wrong_password_is_refused() -> None:
    from smbprotocol.exceptions import LogonFailure

    with pytest.raises(LogonFailure):  # STATUS_LOGON_FAILURE
        smbclient.register_session(SERVER, username="fileshare", password="not-the-password", port=PORT,
                                   connection_cache={})  # fmt: skip


def test_w16_anyone_with_the_shared_password_can_write_and_delete() -> None:
    name = f"{ROOT}\\zz_test_{uuid.uuid4().hex[:6]}.txt"
    with smbclient.open_file(name, mode="w", port=PORT) as f:
        f.write("anyone can write here")
    smbclient.remove(name, port=PORT)
    assert not smbclient.path.exists(name, port=PORT)  # gone for good: no recycle bin, no versions


def test_w6_ramesh_notes_live_in_the_calculator() -> None:
    wb = openpyxl.load_workbook(io.BytesIO(read(CALC_NAME)))
    notes = [str(r[1].value) for r in wb["Ramesh notes"].iter_rows(min_row=2)]
    assert len(notes) >= 10
    assert any("Titanium" in n for n in notes) and any("anodize" in n.lower() for n in notes)


def test_w5_price_list_is_stale_versus_odoo() -> None:
    o = Odoo("office", env("ODOO_OFFICE_PASSWORD"))
    odoo = {p["default_code"]: p["standard_price"] for p in o.call("product.template", "search_read",
            [("default_code", "=like", "MAT-%")], fields=["default_code", "standard_price"])}  # fmt: skip
    calc = openpyxl.load_workbook(io.BytesIO(read(CALC_NAME)))["Materials"]
    stale = {r[1].value: r[3].value for r in calc.iter_rows(min_row=2)}
    assert all(stale[c] < odoo[c] for c in stale), "the calculator's prices should be ~3 years old"
    assert all(stale[c] < odoo[c] * 0.9 for c in stale if c != "MAT-POM"), "metals rose >10 % since 2023"
    prices = openpyxl.load_workbook(io.BytesIO(read("Material_Prices_2023.xlsx"))).active
    assert prices is not None and "Delrin" in [r[0].value for r in prices.iter_rows(min_row=2)]  # messy names


def test_w5_some_quotes_exist_only_in_excel() -> None:
    o = Odoo("office", env("ODOO_OFFICE_PASSWORD"))
    in_odoo = excel_only = 0
    for rel in quote_files():
        ws = openpyxl.load_workbook(io.BytesIO(read(rel)))["Calculator"]
        odoo_no, drawing = ws["B42"].value, str(ws["B3"].value).split(" Rev")[0]
        if odoo_no:
            in_odoo += 1
        else:
            assert not o.call("product.template", "search_count", [("x_drawing_no", "=", drawing)])
            excel_only += 1
    assert in_odoo >= 100 and 20 <= excel_only <= 40


def test_w4_duplicate_customer_folders() -> None:
    folders = smbclient.listdir(ROOT + r"\Customers", port=PORT)
    assert sum(1 for f in folders if f.lower().startswith("kestrel")) >= 1
    assert len(folders) > 35  # more folders than real customers: duplicates


def test_libreoffice_recalculation_matches_costing_model_within_half_a_percent() -> None:
    run = REPO / "data" / "generated" / "recalc" / f"t{uuid.uuid4().hex[:6]}"
    (run / "in").mkdir(parents=True)
    sample = random.Random(11).sample(quote_files(), 25)
    for i, rel in enumerate(sample):
        (run / "in" / f"q{i:02d}.xlsx").write_bytes(read(rel))
    files = [f"/work/{run.name}/in/q{i:02d}.xlsx" for i in range(len(sample))]
    subprocess.run(["docker", "compose", "-p", "apex", "-f", str(REPO / "docker-compose.legacy.yml"), "run", "--rm",
                    "calc", "--convert-to", "xlsx", "--outdir", f"/work/{run.name}/out", *files],
                   check=True, capture_output=True, timeout=600)  # fmt: skip
    try:
        for i in range(len(sample)):
            inp = openpyxl.load_workbook(run / "in" / f"q{i:02d}.xlsx")
            spec, qty, tier = spec_from_sheet(inp["Calculator"])
            ppk = next(float(str(r[3].value)) for r in inp["Materials"].iter_rows(min_row=2) if r[1].value == spec.material)
            out = openpyxl.load_workbook(run / "out" / f"q{i:02d}.xlsx", data_only=True)["Calculator"]
            excel = out[UNIT_PRICE_CELL].value
            assert isinstance(excel, int | float), f"{sample[i]}: LibreOffice did not compute the price ({excel})"
            python = cm.estimate(spec, qty, tier, price_per_kg=ppk).unit_price
            assert excel == pytest.approx(python, rel=0.005), sample[i]
    finally:
        shutil.rmtree(run, ignore_errors=True)
