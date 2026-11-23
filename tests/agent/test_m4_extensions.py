"""M4 extensions (S11-S18) Done-when, against the live stack and the labelled RFQs (no LLM).
- S11: every out-of-capability RFQ part is caught, and no capable part gets a no-bid;
- S12: the labelled repeats and revisions are detected (the right earlier part), with no false matches, and the same
  files sent by ANOTHER customer never match;
- S13: every labelled DFM issue is found, with no false ones; every saving is reproduced by the engine to the cent;
  no suggestion changes any other field of the customer's spec;
- S14: the win model is trained on a TIME split and used only if it beats the average win rate (the gate; the result
  itself is reported in WIN_MODEL_CARD.md, not asserted); the heatmap shows the seeded load; more open quotes push
  committed dates out;
- S15: replies to agent-sent quotes are routed as the simulated customers decided; lost reasons are in Odoo; a late
  acceptance was re-priced; the weekly digest passed the faithfulness check (stored results: no LLM here);
- S16: the what-if assistant has only read-only tools; its scripted answers are tool-sourced and faithful; the
  injection attempts changed nothing; an applied option is a person's audited click (stored results: no LLM here);
- S17: supplier replies extracted as the suppliers wrote them; nothing about the customer reached a supplier's mailbox;
  NDA-restricted / export-controlled RFQs are blocked and never sent (stored results + the supplier mailboxes);
- S18: quoted vs actual finds the loss-making jobs and the families whose estimates run short; a mined override
  pattern was approved and changed the next quote (stored results + the variance re-computed: no LLM)."""

import json
import subprocess
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(REPO / "docker-compose.legacy.yml"), "-f",
           str(REPO / "docker-compose.agent.yml")]  # fmt: skip


def knowledge(module: str) -> Any:
    r = subprocess.run([*COMPOSE, "run", "--rm", "knowledge", "python", "-m", module, "eval"],
                       capture_output=True, text=True, encoding="utf-8", timeout=900)  # fmt: skip
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_s11_capability() -> None:
    r = knowledge("apex_agent.costing.capability")
    # every labelled impossible part among the stored RFQs is caught (8 in the full 68-email mix, 4 in the class set)
    assert r["out_of_capability_caught"] > 0 and not r["out_of_capability_missed"], r
    assert not r["capable_refused"], r["capable_refused"]


def test_s12_repeats_and_revisions() -> None:
    r = knowledge("apex_agent.costing.repeat")
    assert r["repeat"] == [10, 10] and r["revision"] == [12, 12], r["wrong"]
    assert not r["false_matches"], r["false_matches"]
    assert not r["cross_customer_matches"], r["cross_customer_matches"]


def test_s13_dfm() -> None:
    r = knowledge("apex_agent.costing.dfm")
    assert r["found"] == r["labelled_issues"] > 0 and not r["missed"], r["missed"]
    assert not r["false_issues"], r["false_issues"]
    assert r["reproduced"] == r["priced"] > 0 and not r["not_reproduced"], r["not_reproduced"]
    assert not r["spec_changed_elsewhere"], r["spec_changed_elsewhere"]


def in_container(service: str, *cmd: str, run: bool = False) -> Any:
    r = subprocess.run([*COMPOSE, *(["run", "--rm"] if run else ["exec", "-T"]), service, *cmd],
                       capture_output=True, text=True, encoding="utf-8", timeout=900)  # fmt: skip
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_s14_win_model_gate() -> None:
    card = in_container("knowledge", "python", "-m", "apex_agent.costing.win_model", "train", run=True)
    assert card["train"]["to"] < card["test"]["from"] and card["test"]["n"] >= 50  # split by time, a real test set
    b = card["brier"]
    assert card["beats_baseline"] == (b["model"] < b["baseline_average_win_rate"])
    code = "import json; from apex_agent.costing import win_model as w; m = w.load(); print(json.dumps(m and m['validated']))"
    used = in_container("worker", "python", "-c", code)
    assert used == card["beats_baseline"]  # a model that doesn't beat the baseline is never marked validated


def test_s14_capacity_heatmap_and_flood() -> None:
    r = in_container("worker", "python", "-m", "apex_agent.costing.capacity", "--flood", "5")
    assert r["open_quotes"] > 0 and r["expected_minutes"]
    assert r["flood"]["committed_after"] > r["flood"]["committed_before"], r["flood"]
    code = (
        "import json; from apex_agent.knowledge import db; c = db.connect(); "
        "print(json.dumps(c.execute('SELECT data FROM capacity_snapshots ORDER BY id DESC LIMIT 1').fetchone()[0]))"
    )
    snap = in_container("worker", "python", "-c", code)
    assert any(c["confirmed"] > 0 for c in snap["rows"]["MILL3"])  # the seeded open work orders are on the heatmap


def test_s15_replies_lost_reasons_late_acceptance() -> None:
    from scripts.replies_eval import evaluate

    r = evaluate()
    assert r["replies_with_truth"] >= 1 and not r["routed_wrong"], r["routed_wrong"]  # how many depends on the customers
    assert r["lost_reasons"] and all(x["odoo_state"] == "cancel" and x["odoo_lost_reason"] for x in r["lost_reasons"])
    assert any(x["written_by_agent"] for x in r["lost_reasons"])
    assert r["late_acceptances_repriced"], "run scripts.s15_late_acceptance first (DEMO.md S15)"


def test_s15_digest_is_faithful() -> None:
    code = (
        "import json; from apex_agent.knowledge import db; c = db.connect(); "
        "print(json.dumps(c.execute('SELECT faithful, unfaithful FROM digests ORDER BY id DESC LIMIT 1').fetchone()))"
    )
    faithful, bad = in_container("worker", "python", "-c", code)
    assert faithful and not bad, bad


def test_s16_assistant_has_no_write_tool() -> None:
    src = (REPO / "agent" / "src" / "apex_agent" / "whatif.py").read_text(encoding="utf-8")
    names = {t["function"]["name"] for t in __import__("apex_agent.whatif", fromlist=["TOOLS"]).TOOLS}
    assert names == {"get_quote", "recompute_cost", "recompute_leadtime", "recompute_co2", "explain_line_item", "get_analogs",
                     "get_policy_rule", "win_curve"}  # fmt: skip
    for forbidden in ("erp-mcp-write", "mail-mcp", "send_reply", "record_lost", "create_draft_quotation", "jobs SET"):
        assert forbidden not in src, forbidden


def test_s16_answers_sourced_and_injections_harmless() -> None:
    code = (
        "import json; from apex_agent.knowledge import db; c = db.connect(); print(json.dumps({"
        "'log': c.execute(\"SELECT question, answer, faithful, tools FROM whatif_log WHERE email_id = (SELECT email_id FROM whatif_options ORDER BY id DESC LIMIT 1) ORDER BY id\").fetchall(), "
        "'side_effects': c.execute(\"SELECT (SELECT count(*) FROM decisions WHERE email_id = (SELECT email_id FROM whatif_options ORDER BY id DESC LIMIT 1)) + (SELECT count(*) FROM actions WHERE email_id = (SELECT email_id FROM whatif_options ORDER BY id DESC LIMIT 1))\").fetchone()[0], "
        "'options': c.execute(\"SELECT by_user, unit_price FROM whatif_options WHERE email_id = (SELECT email_id FROM whatif_options ORDER BY id DESC LIMIT 1)\").fetchall(), "
        "'audited': c.execute(\"SELECT count(*) FROM audit_log WHERE email_id = (SELECT email_id FROM whatif_options ORDER BY id DESC LIMIT 1) AND data->>'event' = 'what-if option applied'\").fetchone()[0]}, default=str))"
    )
    r = in_container("worker", "python", "-c", code)
    tol = [x for x in r["log"] if "0.1 mm" in x[0]][-1]  # the corrected answer: the NEW price, from recompute_cost, faithful
    new_price = next(c["result"]["new_unit_price"] for c in tol[3] if c["tool"] == "recompute_cost")
    assert tol[2] and f"{new_price:.2f}" in tol[1], tol[1]
    attacks = [x for x in r["log"] if "approve" in x[0].lower() or "record_lost" in x[0]]
    assert attacks and all(not x[3] for x in attacks)  # no tool was even called for them
    assert r["side_effects"] == 0
    assert r["options"] and r["audited"] >= 1


def test_s17_suppliers_extracted_redacted_blocked() -> None:
    from scripts.suppliers_eval import evaluate

    r = evaluate()
    assert r["replies"] >= 1 and r["lines_extracted_right"] == r["lines_true"] > 0, r["wrong"]
    assert r["sent"] >= 1 and r["sent_with_drawing"] >= 1 and not r["leaks"], r["leaks"]
    assert r["blocked"] >= 1 and not r["blocked_but_sent"], r["blocked_but_sent"]
    assert r["settled"], "a settled best offer (Odoo price + draft PO)"


def test_s18_variance_finds_losses_and_biased_families() -> None:
    r = in_container("knowledge", "python", "-m", "apex_agent.costing.variance", run=True)
    assert r["finished_jobs"] > 50 and r["lost_money"] > 0 and r["total_loss_usd"] > 0
    fam = r["minutes_actual_vs_planned_by_family"]
    assert fam["flanged_housing"]["median_ratio"] > 1.15 and fam["sheet_panel"]["median_ratio"] < 1.0  # seeded W14 bias found


def test_s18_mined_rule_approved_and_changes_the_next_quote() -> None:
    import yaml

    policy = yaml.safe_load((REPO / "agent" / "config" / "policy.yaml").read_text(encoding="utf-8"))
    mined = [r for r in policy.get("pricing_rules", []) if str(r.get("source", "")).startswith("mined from")]
    assert mined and mined[-1]["approved_by"], "an approved rule mined from reviewer overrides"
    rid = mined[-1]["id"]
    code = (
        "import json; from apex_agent.knowledge import db; c = db.connect(); print(json.dumps([r[0] for r in c.execute("
        "\"SELECT result FROM estimates WHERE result->'breaks'->0->'rules' @> to_jsonb(ARRAY[]::text[]) ORDER BY created_at DESC LIMIT 400\").fetchall()]))"
    )
    est = in_container("worker", "python", "-c", code)
    assert any(any(str(x).startswith(rid) for x in e["breaks"][0]["rules"]) for e in est), f"no estimate applies {rid} yet"
