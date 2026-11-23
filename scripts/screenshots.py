"""Screenshots of the running legacy system for SOP_MANUAL_QUOTING.md -> docs/legacy/img/*.png.

Drives the Microsoft Edge already installed on Windows through Playwright (channel "msedge"), so no
browser is downloaded. Logs in like an estimator: Roundcube as rfq@ (the shared mailbox), Odoo as
`office` (the shared login). Read-only: nothing is saved or sent. Passwords are typed into masked
fields and never appear in an image.

Usage: uv run python -m scripts.screenshots   (needs `make demo-legacy` and `make rfq-burst` first)
"""

import json
import shutil
import subprocess
import uuid

import pymupdf
from playwright.sync_api import Page, sync_playwright

from legacy.mail.client import imap, password
from legacy.odoo.rpc import ODOO_URL, ROOT, Odoo, env

IMG = ROOT / "docs" / "legacy" / "img"
WEBMAIL = "http://127.0.0.1:8081"
EXAMPLE = "D-20031-A"  # Krämer's German RFQ: one of the three the class quotes by hand


def shot(page: Page, name: str) -> None:
    page.wait_for_timeout(1200)  # let the UI finish rendering
    page.screenshot(path=str(IMG / name))
    print(f"  {name}")


def uid_of(subject: str) -> str:
    m = imap("rfq@apex.local")
    try:
        m.select("INBOX", readonly=True)
        _, data = m.uid("search", "SUBJECT", f'"{subject}"')
        uid: bytes = data[0].split()[-1]
        return uid.decode()
    finally:
        m.logout()


def drawing_png() -> None:
    """Page 1 of the RFQ drawing the estimator has to read (rendered from the real PDF)."""
    rfq = next(r["rfq_id"] for r in json.loads((ROOT / "data/generated/rfq_parts.json").read_text(encoding="utf-8"))
               if r["part_code"] == EXAMPLE)  # fmt: skip
    doc = pymupdf.open(ROOT / "data/generated/drawings/rfq" / f"{rfq}.pdf")
    doc[0].get_pixmap(dpi=110).save(str(IMG / "03_drawing.png"))
    print("  03_drawing.png")


def spreadsheet_pngs() -> None:
    """The calculator and Ramesh's notes, as LibreOffice prints them (headless, the L4 `calc` container)."""
    src = sorted((ROOT / "data/generated/share/Quotes/Customers").glob("*/Quote_*.xlsx"))[0]
    run = ROOT / "data" / "generated" / "recalc" / f"s{uuid.uuid4().hex[:6]}"
    (run / "in").mkdir(parents=True)
    shutil.copy(src, run / "in" / "quote.xlsx")
    try:
        subprocess.run(["docker", "compose", "-p", "apex", "-f", str(ROOT / "docker-compose.legacy.yml"), "run", "--rm",
                        "calc", "--convert-to", "pdf", "--outdir", f"/work/{run.name}/out", f"/work/{run.name}/in/quote.xlsx"],
                       check=True, capture_output=True, timeout=300)  # fmt: skip
        doc = pymupdf.open(run / "out" / "quote.pdf")
        for name, marker in (("05_calculator.png", "UNIT PRICE"), ("06_ramesh_notes.png", "Titanium")):
            page = next(pg for pg in doc if marker in pg.get_text())
            page.get_pixmap(dpi=110).save(str(IMG / name))
            print(f"  {name}")
    finally:
        shutil.rmtree(run, ignore_errors=True)


def roundcube(page: Page, login: str) -> None:
    page.goto(WEBMAIL)
    page.fill("#rcmloginuser", login)
    page.fill("#rcmloginpwd", password(login))
    page.click("#rcmloginsubmit")
    page.wait_for_url("**_task=mail**")


def odoo(page: Page) -> None:
    page.goto(f"{ODOO_URL}/web/login")
    page.fill("#login", "office")
    page.fill("#password", env("ODOO_OFFICE_PASSWORD"))
    page.click("button[type=submit]")
    page.wait_for_selector(".o_main_navbar")  # not the URL: "/web/login" itself matches "**/web**"


def main() -> None:
    IMG.mkdir(parents=True, exist_ok=True)
    o = Odoo("office", env("ODOO_OFFICE_PASSWORD"))
    quote = o.call("sale.order", "search_read", [("client_order_ref", "=like", "HIST-%"), ("state", "=", "sale")],
                   fields=["id"], order="client_order_ref", limit=1)[0]["id"]  # fmt: skip
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1366, "height": 800})
        roundcube(page, "rfq@apex.local")
        shot(page, "01_rfq_inbox.png")
        page.goto(f"{WEBMAIL}/?_task=mail&_mbox=INBOX&_uid={uid_of('Anfrage ' + EXAMPLE)}&_action=show")
        shot(page, "02_rfq_email.png")
        drawing_png()
        spreadsheet_pngs()
        odoo(page)
        products, quotes = "stock.product_template_action_product", "sale.action_quotations"  # the web client takes XML ids
        page.goto(f"{ODOO_URL}/web#action={products}&view_type=list")
        page.wait_for_selector(".o_searchview_input")
        page.fill(".o_searchview_input", "D-100")
        page.keyboard.press("Enter")
        shot(page, "04_odoo_similar_parts.png")
        page.goto(f"{ODOO_URL}/web#action={quotes}&model=sale.order&id={quote}&view_type=form")
        page.wait_for_selector(".o_form_view")
        shot(page, "07_odoo_quote.png")
        page.goto(f"{ODOO_URL}/web#action={quotes}&view_type=list")
        page.wait_for_selector(".o_list_view")
        shot(page, "09_odoo_quotes_list.png")
        page.goto(f"{WEBMAIL}/?_task=mail&_action=compose")
        page.wait_for_selector("#compose-subject")
        page.fill("#compose-subject", f"Angebot {EXAMPLE}")
        page.fill("#composebody", "Sehr geehrte Damen und Herren,\n\nanbei unser Angebot … (typed by hand, PDF attached)\n")
        shot(page, "08_roundcube_send_quote.png")
        browser.close()
    print(f"screenshots: {len(list(IMG.glob('*.png')))} images in {IMG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
