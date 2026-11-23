"""L1: bring a fresh Odoo 17 to Apex's as-is configuration, entirely through the JSON-RPC API.

Idempotent: safe to run any number of times (`make seed` calls it every time).
Steps: create DB -> install modules + apex_legacy -> company -> shift calendar ->
work centers -> materials -> outside processes -> the shared "office" login (W3).

Run: uv run python -m legacy.odoo.setup_odoo
"""

import time
import urllib.error
import urllib.request

from legacy.odoo.rpc import ODOO_DB, ODOO_URL, Odoo, env, jsonrpc

SEED = "L1-master"
MODULES = ["sale_management", "mrp", "stock", "purchase", "contacts", "apex_legacy"]

# ponytail: realistic synthetic assumptions (typical US job-shop rates, common machine classes),
# not market quotes. Rates are from the spec; setup minutes, envelopes, tolerances, Ra and
# average power draw are typical values for the named machine class.
# (name, code, $/h, setup min, cleanup min, x, y, z mm, max kg, best tol mm, best Ra µm, kW)
WORKCENTERS = [
    ("CNC Mill 3-Axis (VF-2 class)", "MILL3", 85, 45, 10, 762, 406, 508, 1360, 0.010, 0.8, 11.0),
    ("CNC Mill 5-Axis (UMC-750 class)", "MILL5", 140, 60, 15, 762, 508, 508, 300, 0.005, 0.4, 15.0),
    ("CNC Lathe (ST-20 class)", "LATHE", 75, 30, 10, 533, 533, 533, 180, 0.008, 0.8, 11.0),
    ("Fiber Laser 6 kW (3015 bed)", "LASER", 110, 10, 5, 3000, 1500, 25, 900, 0.100, 6.3, 25.0),
    ("Press Brake 100 t", "BRAKE", 70, 20, 5, 3100, 0, 0, 200, 0.200, 6.3, 7.5),
    ("Deburr Bench", "DEBURR", 40, 5, 0, 600, 600, 300, 25, 0.100, 3.2, 1.5),
    ("CMM Inspection", "CMM", 90, 15, 0, 1000, 700, 600, 500, 0.002, 0.0, 2.0),
    ("Band Saw", "SAW", 45, 5, 5, 330, 330, 0, 500, 0.500, 12.5, 2.2),
]

# ponytail: typical 2026 bar/plate stock prices for small-lot buyers; L2's history and L4's
# stale price list are derived from these. (code, name, grade as Odoo knows it, g/cm³, $/kg)
MATERIALS = [
    ("MAT-AL6061", "Aluminium 6061-T6 bar/plate", "Al 6061-T6", 2.70, 6.50),
    ("MAT-AL7075", "Aluminium 7075-T6 bar/plate", "Al 7075-T6", 2.81, 12.00),
    ("MAT-SS304", "Stainless steel 304 bar/plate", "SS304", 8.00, 5.50),
    ("MAT-SS316", "Stainless steel 316 bar/plate", "SS316", 8.00, 8.00),
    ("MAT-1018", "Carbon steel 1018 bar", "Steel 1018", 7.87, 2.20),
    ("MAT-S235", "Structural steel S235 plate/sheet", "Steel S235", 7.85, 1.60),
    ("MAT-C360", "Brass C360 bar", "Brass C360", 8.50, 11.00),
    ("MAT-POM", "POM (acetal) rod/plate", "POM", 1.41, 7.00),
    ("MAT-TI64", "Titanium Ti-6Al-4V bar", "Ti-6Al-4V", 4.43, 45.00),
]

# ponytail: $ per part at typical small-lot pricing; lot minimums arrive with costing in L2.
OUTSIDE_PROCESSES = [
    ("OSP-ANODIZE", "Anodize (Type II, clear/black)", 1.20),
    ("OSP-PLATING", "Zinc plating (clear trivalent)", 0.90),
    ("OSP-POWDER", "Powder coat (single colour)", 2.50),
    ("OSP-PASSIVATE", "Passivation (ASTM A967)", 1.00),
    ("OSP-HEATTREAT", "Heat treat (harden & temper)", 1.80),
]

OFFICE_GROUPS = [
    "base.group_user",
    "sales_team.group_sale_manager",
    "mrp.group_mrp_user",
    "purchase.group_purchase_user",
    "stock.group_stock_user",
]


def wait_for_odoo(timeout_s: int = 300) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with urllib.request.urlopen(f"{ODOO_URL}/web/health", timeout=5) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError):
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"Odoo not healthy at {ODOO_URL} after {timeout_s}s (make demo-legacy?)")
        time.sleep(3)


def ensure_database() -> None:
    if jsonrpc("db", "db_exist", ODOO_DB):
        print(f"database {ODOO_DB!r}: exists")
        return
    print(f"database {ODOO_DB!r}: creating (takes ~1 min)")
    jsonrpc(
        "db", "create_database", env("ODOO_MASTER_PASSWORD"), ODOO_DB,
        False, "en_US", env("ODOO_ADMIN_PASSWORD"), "admin", "US",
        timeout=600,
    )  # fmt: skip


def install_modules(odoo: Odoo) -> None:
    odoo.call("ir.module.module", "update_list")
    todo = odoo.call(
        "ir.module.module", "search_read",
        [("name", "in", MODULES), ("state", "!=", "installed")], fields=["name"],
    )  # fmt: skip
    if todo:
        print("modules: installing", ", ".join(m["name"] for m in todo), "(takes a few min)")
        odoo.call("ir.module.module", "button_immediate_install", [m["id"] for m in todo], timeout=1200)
    else:
        print("modules: all installed")
    # Our own add-on gains fields over the phases: upgrade it when its manifest version moved on.
    mod = odoo.call("ir.module.module", "search_read", [("name", "=", "apex_legacy")],
                    fields=["latest_version", "installed_version"])[0]  # fmt: skip
    if mod["latest_version"] != mod["installed_version"]:
        print(f"apex_legacy: upgrading {mod['latest_version']} -> {mod['installed_version']}")
        odoo.call("ir.module.module", "button_immediate_upgrade", [mod["id"]], timeout=1200)


def configure_company(odoo: Odoo) -> None:
    usd = odoo.xmlid("base.USD")
    odoo.call("res.company", "write", [1], {"name": "Apex Precision Components", "currency_id": usd})
    # Work orders/routings must be switched on for BOM operations (used from L2).
    settings = odoo.call("res.config.settings", "create", {"group_mrp_routings": True})
    odoo.call("res.config.settings", "execute", [settings])


def ensure_calendar(odoo: Odoo) -> int:
    name = "Apex 2 shifts (Mon-Fri 06-22)"
    attendances = [
        (
            0,
            0,
            {
                "name": f"{day} {label}",
                "dayofweek": str(i),
                "hour_from": h0,
                "hour_to": h1,
                "day_period": period,
            },
        )
        for i, day in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri"])
        for label, h0, h1, period in [("shift 1", 6, 14, "morning"), ("shift 2", 14, 22, "afternoon")]
    ]
    found = odoo.call("resource.calendar", "search", [("name", "=", name)], limit=1)
    if found:
        return int(found[0])
    cal_id: int = odoo.call(
        "resource.calendar", "create",
        {"name": name, "tz": "Asia/Kolkata", "hours_per_day": 16, "attendance_ids": attendances},
    )  # fmt: skip
    return cal_id


def ensure_workcenters(odoo: Odoo, calendar_id: int) -> None:
    for name, code, rate, setup, cleanup, x, y, z, kg, tol, ra, kw in WORKCENTERS:
        odoo.ensure("mrp.workcenter", [("code", "=", code)], {
            "name": name, "code": code, "costs_hour": rate, "time_start": setup, "time_stop": cleanup,
            "resource_calendar_id": calendar_id, "default_capacity": 1, "time_efficiency": 100,
            "x_max_x_mm": x, "x_max_y_mm": y, "x_max_z_mm": z, "x_max_weight_kg": kg,
            "x_best_tolerance_mm": tol, "x_best_ra": ra, "x_power_kw": kw, "x_seed_batch": SEED,
        })  # fmt: skip
    print(f"work centers: {len(WORKCENTERS)}")


def ensure_category(odoo: Odoo, name: str) -> int:
    return odoo.ensure("product.category", [("name", "=", name)], {"name": name})


def ensure_materials(odoo: Odoo) -> None:
    categ = ensure_category(odoo, "Raw Material")
    kg = odoo.xmlid("uom.product_uom_kgm")
    for code, name, grade, density, price in MATERIALS:
        odoo.ensure("product.template", [("default_code", "=", code)], {
            "name": name, "default_code": code, "detailed_type": "product", "categ_id": categ,
            "uom_id": kg, "uom_po_id": kg, "standard_price": price, "sale_ok": False,
            "purchase_ok": True, "x_material_grade": grade, "x_density_g_cm3": density,
            "x_seed_batch": SEED,
        })  # fmt: skip
    print(f"materials: {len(MATERIALS)}")


def ensure_outside_processes(odoo: Odoo) -> None:
    categ = ensure_category(odoo, "Outside Processes")
    for code, name, price in OUTSIDE_PROCESSES:
        odoo.ensure("product.template", [("default_code", "=", code)], {
            "name": name, "default_code": code, "detailed_type": "service", "categ_id": categ,
            "standard_price": price, "sale_ok": False, "purchase_ok": True, "x_seed_batch": SEED,
        })  # fmt: skip
    print(f"outside processes: {len(OUTSIDE_PROCESSES)}")


def ensure_office_user(odoo: Odoo) -> None:
    """W3: ONE login shared by every estimator. No per-person accounts, so no record of who did what."""
    groups = [odoo.xmlid(g) for g in OFFICE_GROUPS]
    vals = {
        "name": "Office (shared)",
        "login": "office",
        "password": env("ODOO_OFFICE_PASSWORD"),
        "groups_id": [(6, 0, groups)],
    }
    odoo.ensure("res.users", [("login", "=", "office")], vals)
    print("user: office (shared)")


def main() -> None:
    wait_for_odoo()
    ensure_database()
    admin = Odoo("admin", env("ODOO_ADMIN_PASSWORD"))
    install_modules(admin)
    configure_company(admin)
    calendar_id = ensure_calendar(admin)
    ensure_workcenters(admin, calendar_id)
    ensure_materials(admin)
    ensure_outside_processes(admin)
    ensure_office_user(admin)
    print("L1 setup done")


if __name__ == "__main__":
    main()
