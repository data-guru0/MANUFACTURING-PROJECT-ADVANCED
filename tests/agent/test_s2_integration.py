"""S2 Done-when, against the live stacks (make demo-agent, make knowledge):
- the shared login is no longer used by the agent;
- erp_reader is refused when it tries to write;
- the sync is idempotent;
- the data-quality report lists the seeded duplicates and name variants;
- the top-5 analogs contain a same-family, same-material part for most held-out parts (report the %).
Plus: the scoped mail and share accounts, OPA on every MCP tool, the write tool's idempotency, and the
human-approval step for pricing rules."""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from legacy.odoo.rpc import Odoo, OdooError, env
from legacy.seed.history import DUPLICATES

REPO = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(REPO / "docker-compose.legacy.yml"), "-f",
           str(REPO / "docker-compose.agent.yml")]  # fmt: skip


SHARE_PROBE = r"""
import os, uuid, smbclient
smbclient.register_session("fileserver", username="agent-ro", password=os.environ["APEX_SHARE_PASSWORD"])
print("LIST", "Customers" in smbclient.listdir(r"\\fileserver\Quotes"))
try:
    with smbclient.open_file(rf"\\fileserver\Quotes\zz_{uuid.uuid4().hex[:6]}.txt", mode="w") as f:
        f.write("x")
    print("WRITE allowed")
except OSError as e:
    print("WRITE refused", type(e).__name__)
"""


def run(*args: str, timeout: int = 600) -> str:
    r = subprocess.run([*COMPOSE, "run", "--rm", "knowledge", *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)  # fmt: skip
    assert r.returncode == 0, (r.stdout + r.stderr)[-2000:]
    return r.stdout


def last_json(out: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads([ln for ln in out.splitlines() if ln.startswith("{")][-1])
    return data


def tool(server: str, name: str, args: dict[str, Any], node: str) -> dict[str, Any]:
    return last_json(run("python", "-m", "apex_agent.tools", server, name, json.dumps(args), "--node", node))


def sql(query: str) -> list[str]:
    r = subprocess.run([*COMPOSE, "exec", "-T", "app-postgres", "psql", "-U", "postgres", "-d", "apex", "-At", "-c", query],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)  # fmt: skip
    assert r.returncode == 0, r.stderr
    return r.stdout.split("\n")[:-1]


def test_the_agent_never_uses_the_shared_logins() -> None:
    compose_agent = yaml.safe_load((REPO / "docker-compose.agent.yml").read_text(encoding="utf-8"))["services"]
    env_text = json.dumps({s: c.get("environment") for s, c in compose_agent.items()})
    assert (
        "ODOO_OFFICE_PASSWORD" not in env_text and "FILESHARE_PASSWORD}" not in env_text and "MAIL_RFQ_PASSWORD" not in env_text
    )
    code = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "agent" / "src").rglob("*.py"))
    assert "ODOO_OFFICE_PASSWORD" not in code and '"office"' not in code
    o = Odoo("admin", env("ODOO_ADMIN_PASSWORD"))
    users = {u["login"]: u for u in o.call("res.users", "search_read", [("login", "in", ["erp_reader", "erp_integration"])],
                                           fields=["login", "login_date"])}  # fmt: skip
    assert users["erp_reader"]["login_date"], "the agent should have logged in with its own read-only account"


def test_erp_reader_reads_but_is_refused_when_it_writes() -> None:
    r = Odoo("erp_reader", env("ODOO_READER_PASSWORD"))
    assert r.call("sale.order", "search_count", []) >= 400
    so = r.call("sale.order", "search", [], limit=1)
    for model, method, args in (("sale.order", "write", [so, {"note": "x"}]), ("product.template", "create", [{"name": "x"}]),
                                ("res.partner", "create", [{"name": "x"}])):  # fmt: skip
        with pytest.raises(OdooError, match="not allowed"):
            r.call(model, method, *args)


def test_the_agent_mailbox_and_share_accounts_are_scoped() -> None:
    import imaplib
    import ssl

    ctx = ssl.create_default_context(cafile=str(REPO / "data" / "generated" / "certs" / "ca.crt"))
    m = imaplib.IMAP4_SSL("127.0.0.1", 993, ssl_context=ctx)
    m.login("rfq@apex.local*agent", env("MAIL_AGENT_PASSWORD"))  # its own password, logged as master user "agent"
    assert m.select("INBOX", readonly=True)[0] == "OK"
    m.logout()
    # The share check runs where the agent runs: in its Linux container, over the real network to the file server.
    probe = SHARE_PROBE
    out = run("python", "-c", probe)
    assert "LIST True" in out and "WRITE refused" in out, out[-500:]


def test_sync_is_idempotent() -> None:
    first = last_json(run("python", "-m", "apex_agent.knowledge.sync"))
    second = last_json(run("python", "-m", "apex_agent.knowledge.sync"))
    assert second["odoo_changed"] == 0, second  # nothing new in Odoo -> nothing rewritten
    assert int(sql("SELECT count(*) FROM odoo_records")[0]) >= 4000
    for key in ("parts", "duplicate_customers", "routing_estimated"):
        assert first[key] == second[key], key
    assert int(sql("SELECT count(*) FROM parts")[0]) == second["parts"]
    assert int(sql("SELECT count(*) FROM odoo_records WHERE model = 'sale.order'")[0]) >= 400


def test_data_quality_report_lists_the_seeded_problems() -> None:
    run("python", "-m", "apex_agent.knowledge.report")
    report = (REPO / "docs" / "fde" / "DATA_QUALITY.md").read_text(encoding="utf-8")
    for original, duplicate in DUPLICATES.items():
        assert duplicate in report and original in report, duplicate  # all 5 seeded duplicate customers
    for spelling in ("AL6061", "6061-T6", "Alu 6061", "1.4301", "Delrin"):  # seeded material variants
        assert spelling in report, spelling
    assert re.search(r"(\d+) of (\d+) quoted products have \*\*no routing\*\*", report)
    assert "Excel only" in report or "exist only in Excel" in report
    assert int(sql("SELECT count(*) FROM price_conflicts WHERE winner = 'odoo'")[0]) >= 8  # W5: Odoo wins


def test_routings_are_estimated_from_analogs_and_marked() -> None:
    rows = sql("SELECT code, routing_source FROM parts WHERE source = 'odoo' AND routing_source LIKE 'estimated:%'")
    assert len(rows) >= 40
    for row in rows[:10]:
        code, src = row.split("|")
        analog = src.removeprefix("estimated:")
        fam = sql(f"SELECT family FROM parts WHERE code IN ('{code}', '{analog}')")
        assert len(set(fam)) == 1, (code, analog, fam)  # same family


def test_top5_analogs_share_family_and_material_for_most_held_out_parts() -> None:
    res = last_json(run("python", "-m", "apex_agent.knowledge.evaluate"))
    print(f"\nretrieval: {res}")
    assert res["held_out"] == 80
    assert res["top5_same_family_material_text"] >= 0.8  # "most": measured 0.975 when built


def test_every_mcp_tool_call_is_decided_by_opa() -> None:
    ok = tool("erp-mcp-read", "get_product", {"code": "D-10001-A"}, "retrieve")
    assert ok["ok"] and ok["result"]["found"] and ok["result"]["materials"]
    denied = tool("share-mcp", "ramesh_notes", {}, "retrieve")
    assert not denied["ok"] and "OPA denied" in denied["result"]
    unknown = tool("erp-mcp-read", "get_product", {"code": "D-10001-A"}, "nobody")
    assert not unknown["ok"]


def test_write_tool_only_from_act_and_only_with_a_signed_approval() -> None:
    """S2: only the `act` node may call a write tool. S8: even `act` needs a person's signed approval of the content
    (idempotency with a real approval is proven in test_s8_actions.py)."""
    args = {
        "email_id": 1,
        "approval": "not-a-token",
        "customer": {"ref": "CUST-001"},
        "lines": [{"code": "D-10001-A", "breaks": [{"qty": 10, "unit_price": 12.5}]}],
        "validity_date": "2030-01-01",
        "note_html": "",
    }
    denied = tool("erp-mcp-write", "create_draft_quotation", args, "retrieve")
    assert not denied["ok"] and "OPA denied" in denied["result"]
    unsigned = tool("erp-mcp-write", "create_draft_quotation", args, "act")
    assert not unsigned["ok"] and "no valid approval token" in unsigned["result"], unsigned
    o = Odoo("admin", env("ODOO_ADMIN_PASSWORD"))
    assert not o.call("sale.order", "search", [("client_order_ref", "=", "AGENT-RFQ-1")])  # nothing was written


def test_pricing_rules_need_a_named_human_approval() -> None:
    policy = yaml.safe_load((REPO / "agent" / "config" / "policy.yaml").read_text(encoding="utf-8"))
    rules = policy.get("pricing_rules", [])
    assert rules, "run the DEMO's approve step first (python -m apex_agent.knowledge.rules approve P-001 --by Ramesh)"
    for r in rules:
        assert (
            r["id"].startswith("R-")
            and r["approved_by"]
            and r["approved_at"]
            and r["source"].startswith(("Ramesh notes #", "mined from"))
        )
    proposals = yaml.safe_load((REPO / "agent" / "config" / "rule_proposals.yaml").read_text(encoding="utf-8"))["proposals"]
    approved = {p["id"] for p in proposals if str(p["status"]).startswith("approved")}
    assert approved == {r["proposal"] for r in rules}, "only approved proposals may be in the policy"
