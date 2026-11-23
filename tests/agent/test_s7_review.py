"""S7 Done-when: a Playwright test with a REAL Keycloak login (Microsoft Edge) shows an estimator approving a small quote,
being blocked on a large one, and a senior approving the large one. Plus: no login -> Keycloak (PKCE), a forged header
gets nowhere, a field edit re-costs the job and is stored as a diff, and planner/admin can look but not decide (Envoy ->
OPA blocks them before the app). Two fresh RFQs go through the real graph (gate, triage, extraction, costing, drafting)."""

import json
import subprocess
import time
from collections.abc import Iterator
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect, sync_playwright

from legacy.mail.client import send
from legacy.odoo.rpc import env

REPO = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(REPO / "docker-compose.legacy.yml"), "-f",
           str(REPO / "docker-compose.agent.yml")]  # fmt: skip
UI = "https://localhost:8448"
LIMIT = 5000.0  # policy.yaml graph.senior_above_usd


def sql(query: str) -> list[list[str]]:
    r = subprocess.run([*COMPOSE, "exec", "-T", "app-postgres", "psql", "-U", "postgres", "-d", "apex", "-At", "-F", "\t", "-c", query],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)  # fmt: skip
    assert r.returncode == 0, r.stderr
    return [line.split("\t") for line in r.stdout.splitlines()]


def wait(query: str, timeout: float, what: str) -> list[list[str]]:
    end = time.time() + timeout
    while time.time() < end:
        if rows := sql(query):
            return rows
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what}")


def rfq(sender: str, name: str, qty: str) -> int:
    """RFQ-003's real drawing (DXF + vector PDF) from a known customer; returns the email id once it waits for review."""
    gt = json.loads((REPO / "data/generated/ground_truth/RFQ-003.json").read_text(encoding="utf-8"))
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = f"{name} <{sender}>", "rfq@apex.local", f"RFQ {gt['part_code']} x {qty}"
    msg["Message-ID"] = make_msgid(domain="customers.local")
    msg.set_content(f"Hello Apex team,\n\nplease quote {gt['part_code']}: {qty}.\n\nRegards,\n{name}")
    for kind, mime in (("dxf", ("image", "vnd.dxf")), ("pdf", ("application", "pdf"))):
        msg.add_attachment(
            (REPO / gt["files"][kind]).read_bytes(), maintype=mime[0], subtype=mime[1], filename=f"{gt['part_code']}.{kind}"
        )
    send(sender, msg)
    (email_id,), *_ = wait(f"SELECT id FROM emails WHERE message_id = '{msg['Message-ID']}'", 600, "ingestion")
    wait(f"SELECT 1 FROM jobs j JOIN review_items r USING (email_id) WHERE j.email_id = {email_id} AND j.status = 'awaiting_review'",
         1500, "the review interrupt")  # fmt: skip
    return int(email_id)


def item(email_id: int) -> tuple[float, bool]:
    v, senior = sql(f"SELECT value_usd, needs_senior FROM review_items WHERE email_id = {email_id}")[0]
    return float(v), senior == "t"


@pytest.fixture(scope="module")
def jobs() -> dict[str, int]:
    small = rfq("kestrelautomation@customers.local", "Kestrel Automation", "10 pcs")
    large = rfq("delmontconveyorsyste@customers.local", "Delmont Conveyor Systems", "5000 pcs")
    assert item(small)[0] < LIMIT and not item(small)[1], item(small)
    assert item(large)[0] > LIMIT and item(large)[1], item(large)
    return {"small": small, "large": large}


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        yield b
        b.close()


def login(browser: Browser, username: str) -> Page:
    """The real flow: the screen -> Keycloak's login page -> back with Envoy's login cookies."""
    page = browser.new_context(ignore_https_errors=True).new_page()
    page.goto(UI + "/")
    page.wait_for_url("https://localhost:8445/realms/apex/**")
    page.fill("#username", username)
    page.fill("#password", env("KEYCLOAK_STAFF_PASSWORD"))
    page.click("#kc-login")
    page.wait_for_url(UI + "/")
    expect(page.locator("header .me")).to_contain_text(f"({username})")
    return page


def decide(page: Page, email_id: int, button: str, reason: str, goto: bool = True) -> Any:
    if goto:
        page.goto(f"{UI}/jobs/{email_id}")
    page.fill("#reason", reason)
    page.get_by_role("button", name=button, exact=True).click()
    return page.locator("#result")


def test_without_a_login_the_screen_sends_you_to_keycloak() -> None:
    r = httpx.get(UI + "/", verify=False, follow_redirects=False)  # noqa: S501 (lab CA; we only read the redirect)
    assert r.status_code == 302 and r.headers["location"].startswith(
        "https://localhost:8445/realms/apex/protocol/openid-connect/auth"
    )
    assert "code_challenge_method=S256" in r.headers["location"]  # PKCE
    # a hand-made bearer token doesn't skip the login (Envoy only trusts its own signed login cookies)
    forged = httpx.get(UI + "/jobs/1", headers={"Authorization": "Bearer e30.e30.x"}, verify=False, follow_redirects=False)  # noqa: S501
    assert forged.status_code == 302


def test_a_field_edit_re_costs_the_job_and_is_stored_as_a_diff(jobs: dict[str, int], browser: Browser) -> None:
    e = jobs["small"]
    before, _ = item(e)
    page = login(browser, "arjun")
    page.goto(f"{UI}/jobs/{e}")
    field = page.locator('input[name$="|holes_count"]').first
    old = int(field.input_value())
    field.fill(str(old + 4))
    expect(decide(page, e, "Re-cost with my field changes", "the drawing shows four more holes", goto=False)).to_contain_text(
        "Re-costed", timeout=300_000
    )
    name, was, now, by, why = sql(
        f"SELECT field, old_value, new_value, by_user, reason FROM feedback WHERE email_id = {e} ORDER BY id DESC LIMIT 1"
    )[0]
    assert (name.endswith(".holes_count"), int(was), int(now), by, why) == (
        True,
        old,
        old + 4,
        "arjun",
        "the drawing shows four more holes",
    )
    after, _ = item(e)
    assert after > before, (before, after)  # more holes -> more machine minutes -> a higher price, computed by code


def test_an_estimator_approves_a_small_quote(jobs: dict[str, int], browser: Browser) -> None:
    e = jobs["small"]
    page = login(browser, "arjun")
    expect(decide(page, e, "Approve", "drawing and price checked")).to_contain_text("Done", timeout=120_000)
    assert sql(f"SELECT action, decided_by, reason FROM decisions WHERE email_id = {e}")[0] == [
        "approve",
        "arjun",
        "drawing and price checked",
    ]
    assert sql(f"SELECT status, decision IS NULL FROM jobs WHERE email_id = {e}")[0] == ["sent", "t"]  # S8: approved = sent


def test_an_estimator_is_blocked_on_a_large_quote(jobs: dict[str, int], browser: Browser) -> None:
    e = jobs["large"]
    page = login(browser, "arjun")
    expect(decide(page, e, "Approve", "looks fine")).to_contain_text("a senior estimator must approve this quote")
    assert sql(f"SELECT status FROM jobs WHERE email_id = {e}")[0][0] == "awaiting_review"
    assert sql(f"SELECT count(*) FROM decisions WHERE email_id = {e}")[0][0] == "0"


def test_a_senior_approves_the_large_quote(jobs: dict[str, int], browser: Browser) -> None:
    e = jobs["large"]
    page = login(browser, "ramesh")
    expect(decide(page, e, "Approve", "5000 pcs: checked capacity with Kavita")).to_contain_text("Done", timeout=120_000)
    assert sql(f"SELECT action, decided_by FROM decisions WHERE email_id = {e}")[0] == ["approve", "ramesh"]


def test_planner_auditor_and_admin_can_look_but_not_decide(jobs: dict[str, int], browser: Browser) -> None:
    e = jobs["large"]
    for user in ("kavita", "meera", "vikram"):
        page = login(browser, user)
        page.goto(f"{UI}/jobs/{e}")
        expect(page.locator("h1")).to_contain_text(f"#{e}")
        assert page.get_by_role("button", name="Approve", exact=True).count() == 0
        # even a hand-made POST with this person's real login is stopped at the door (Envoy ext_authz -> OPA): 403
        r = page.request.post(f"{UI}/jobs/{e}/decide", form={"action": "approve"}, headers={"HX-Request": "true"})
        assert r.status == 403, (user, r.status)
