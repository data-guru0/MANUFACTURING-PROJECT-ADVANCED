"""L1 Done-when: log in over JSON-RPC (as the shared estimator login) and list
work centers, materials and outside processes. Runs against the live Odoo container."""

import subprocess
import sys

import pytest

from legacy.odoo.rpc import Odoo, OdooError, env
from legacy.odoo.setup_odoo import MATERIALS, OUTSIDE_PROCESSES, WORKCENTERS


@pytest.fixture(scope="module")
def office() -> Odoo:
    return Odoo("office", env("ODOO_OFFICE_PASSWORD"))


def test_office_lists_workcenters_with_rates_and_envelopes(office: Odoo) -> None:
    rows = office.call(
        "mrp.workcenter", "search_read", [],
        fields=["code", "costs_hour", "time_start", "resource_calendar_id", "x_max_x_mm", "x_power_kw"],
    )  # fmt: skip
    by_code = {r["code"]: r for r in rows}
    assert len(rows) == 8
    expected_rates = {"MILL3": 85, "MILL5": 140, "LATHE": 75, "LASER": 110, "BRAKE": 70,
                      "DEBURR": 40, "CMM": 90, "SAW": 45}  # fmt: skip
    assert {c: by_code[c]["costs_hour"] for c in expected_rates} == expected_rates
    for r in rows:
        assert r["resource_calendar_id"], f"{r['code']} has no shift calendar"
        assert r["time_start"] > 0 and r["x_max_x_mm"] > 0 and r["x_power_kw"] > 0


def test_office_lists_materials_with_density_and_price(office: Odoo) -> None:
    rows = office.call(
        "product.template", "search_read", [("default_code", "=like", "MAT-%")],
        fields=["default_code", "x_density_g_cm3", "standard_price", "uom_id", "detailed_type"],
    )  # fmt: skip
    assert sorted(r["default_code"] for r in rows) == sorted(m[0] for m in MATERIALS)
    for r in rows:
        assert r["x_density_g_cm3"] > 1 and r["standard_price"] > 0
        assert r["uom_id"][1] == "kg" and r["detailed_type"] == "product"


def test_office_lists_outside_processes(office: Odoo) -> None:
    rows = office.call(
        "product.template", "search_read", [("default_code", "=like", "OSP-%")],
        fields=["default_code", "detailed_type"],
    )  # fmt: skip
    assert sorted(r["default_code"] for r in rows) == sorted(p[0] for p in OUTSIDE_PROCESSES)
    assert {r["detailed_type"] for r in rows} == {"service"}


def test_custom_fields_exist(office: Odoo) -> None:
    expected = {
        "product.template": ["x_drawing_no", "x_material_grade", "x_part_family", "x_seed_batch"],
        "mrp.workcenter": ["x_max_x_mm", "x_max_y_mm", "x_max_z_mm", "x_max_weight_kg",
                           "x_best_tolerance_mm", "x_best_ra", "x_power_kw"],
        "sale.order": ["x_rfq_received_at", "x_quote_sent_at", "x_lost_reason"],
        "res.partner": ["x_nda_restricted", "x_required_certs", "x_language"],
    }  # fmt: skip
    for model, names in expected.items():
        fields = office.call(model, "fields_get", names, attributes=["type"])
        assert sorted(fields) == sorted(names), model


def test_w3_one_shared_login_no_personal_estimator_accounts(office: Odoo) -> None:
    logins = {u["login"] for u in office.call("res.users", "search_read", [], fields=["login"])}
    # The humans still share `office` (W3 stays true for the legacy system). From S2 on, the FDE agent has
    # its own service accounts (erp_reader, erp_integration); those are not estimators.
    assert logins - {"erp_reader", "erp_integration"} == {"admin", "office"}


def test_wrong_password_is_refused() -> None:
    with pytest.raises(OdooError):
        Odoo("office", "definitely-not-the-password")


def test_setup_is_idempotent(office: Odoo) -> None:
    subprocess.run([sys.executable, "-m", "legacy.odoo.setup_odoo"], check=True, capture_output=True)
    assert office.call("mrp.workcenter", "search_count", []) == len(WORKCENTERS)
    count = office.call("product.template", "search_count", [("x_seed_batch", "=", "L1-master")])
    assert count == len(MATERIALS) + len(OUTSIDE_PROCESSES)
