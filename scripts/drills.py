"""S19 resilience drills, run against the live LOCAL stack, each recorded in docs/runbooks/DRILLS.md (+ drills.json).

  groq-down <job>     cut the egress proxy (LiteLLM can't reach Groq), re-run one job: it must WAIT (queued, attempts
                      not burnt), the review screen must show "Manual mode"; restore: the job finishes
  odoo-down <job>     stop Odoo, re-run one job: it waits for Odoo (no price made up), the review screen still works;
                      restore: the job finishes
  worker-kill <job>   re-run one job and kill the worker mid-job: on restart it resumes from its checkpoint and no
                      finished node runs twice
  burst <n>           AI off (kill switch), n emails at once: every one is ingested and queued, none lost, the screen
                      says manual mode; then the backlog is handed to people (the outage runbook) and the AI goes back on

Usage: uv run python -m scripts.drills groq-down 193 | odoo-down 64 | worker-kill 54 | burst 100
"""

import json
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from legacy.odoo.rpc import env

REPO = Path(__file__).resolve().parents[1]
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "apex",
    "-f",
    str(REPO / "docker-compose.legacy.yml"),
    "-f",
    str(REPO / "docker-compose.agent.yml"),
]
RESULTS = REPO / "docs" / "runbooks" / "drills.json"


def compose(*args: str, timeout: int = 600) -> str:
    r = subprocess.run([*COMPOSE, *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"{' '.join(args[:3])}: {r.stderr[-600:]}")
    return r.stdout


def sql(q: str) -> str:
    return compose("exec", "-T", "app-postgres", "psql", "-U", "apex", "-d", "apex", "-Atc", q).strip()


def job(email_id: int) -> dict[str, Any]:
    s, a, e = (
        sql(f"SELECT status || '|' || attempts || '|' || coalesce(error, '') FROM jobs WHERE email_id = {int(email_id)}") + "||"
    ).split("|")[:3]
    return {"status": s, "attempts": int(a or 0), "error": e[:160]}


def wait(cond: Callable[[], bool], timeout: float, every: float = 5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(every)
    return False


def screen(path: str = "/") -> tuple[int, str]:
    """GET a review-screen page as arjun (Keycloak login through Envoy): status and visible text."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        page = b.new_context(ignore_https_errors=True).new_page()
        page.goto("https://localhost:8448/")
        page.fill("#username", "arjun")
        page.fill("#password", env("KEYCLOAK_STAFF_PASSWORD"))
        page.click("#kc-login")
        page.wait_for_url("https://localhost:8448/")
        r = page.goto(f"https://localhost:8448{path}")
        out = (r.status if r else 0, page.inner_text("main"))
        b.close()
    return out


def rerun(email_id: int) -> None:
    compose("exec", "-T", "worker", "python", "-m", "apex_agent.worker", "--rerun", str(email_id))


def outage_drill(name: str, email_id: int, stop: list[str], start: list[str], healthy: str | None) -> dict[str, Any]:
    t0 = time.time()
    compose(*stop)
    try:
        rerun(email_id)
        waited = wait(lambda: job(email_id)["status"] == "queued" and bool(job(email_id)["error"]), 300)
        first = job(email_id)
        status, text = screen("/")
        page_ok = screen(f"/jobs/{email_id}")[0] == 200
        time.sleep(75)  # longer than a retry: the job must still be waiting, its attempts untouched
        later = job(email_id)
    finally:
        compose(*start)
        if healthy:
            wait(lambda: "healthy" in compose("ps", healthy, "--format", "{{.Status}}"), 300)
    done = wait(lambda: job(email_id)["status"] == "awaiting_review", 900, 10)
    return {"drill": name, "job": email_id, "at": datetime.now().isoformat(timespec="minutes"),
            "waiting_while_down": waited and later["status"] == "queued" and later["attempts"] == first["attempts"],
            "error_seen": first["error"], "attempts": [first["attempts"], later["attempts"]],
            "manual_mode_banner": "Manual mode" in text, "job_page_served": page_ok,
            "finished_after_restore": done, "minutes": round((time.time() - t0) / 60, 1),
            "passed": waited and later["status"] == "queued" and later["attempts"] == first["attempts"] and "Manual mode" in text and done}  # fmt: skip


def worker_kill(email_id: int) -> dict[str, Any]:
    t0 = datetime.now().astimezone().isoformat()
    rerun(email_id)
    killed_after = None
    if wait(
        lambda: (
            sql(f"SELECT count(*) FROM graph_events WHERE email_id = {email_id} AND at > '{t0}' AND node = 'suppliers'") != "0"
        ),
        300,
        0.5,
    ):
        compose("kill", "worker")
        killed_after = sql(
            f"SELECT string_agg(node, ',' ORDER BY id) FROM graph_events WHERE email_id = {email_id} AND at > '{t0}'"
        )
        compose("start", "worker")
    done = wait(lambda: job(email_id)["status"] == "awaiting_review", 900, 10)
    nodes = sql(
        f"SELECT node || ':' || count(*) FROM graph_events WHERE email_id = {email_id} AND at > '{t0}' GROUP BY node ORDER BY min(id)"
    ).split()
    twice = [n for n in nodes if not n.endswith(":1") and not n.startswith("cost_part")]  # cost_part: one per part (fan-out)
    return {"drill": "worker-kill", "job": email_id, "at": datetime.now().isoformat(timespec="minutes"), "killed_after_nodes": killed_after,
            "nodes_run": nodes, "finished_nodes_run_twice": twice, "finished_after_restart": done,
            "passed": bool(killed_after) and done and not twice}  # fmt: skip


def burst(n: int) -> dict[str, Any]:
    t0 = datetime.now().astimezone().isoformat()
    before = int(sql("SELECT count(*) FROM emails"))
    subprocess.run(["uv", "run", "python", "-m", "agent.ops.killswitch", "off", "--by", "drill", "--reason", f"S19 drill: burst of {n}"], check=True,
                   capture_output=True, cwd=REPO)  # fmt: skip
    try:
        sent_at = time.time()
        subprocess.run(
            ["uv", "run", "python", "-m", "legacy.traffic.rfq_traffic", "--burst", str(n)],
            check=True,
            capture_output=True,
            cwd=REPO,
            timeout=1800,
        )
        ingested = wait(lambda: int(sql("SELECT count(*) FROM emails")) - before >= n, 1800, 10)
        ingest_min = round((time.time() - sent_at) / 60, 1)
        got = int(sql("SELECT count(*) FROM emails")) - before
        jobs = sql(
            f"SELECT coalesce(string_agg(status || ':' || c, ','), '') FROM (SELECT j.status, count(*) c FROM jobs j JOIN emails e ON e.id = j.email_id WHERE e.ingested_at >= '{t0}' GROUP BY 1) x"
        )
        failed = int(
            sql(
                f"SELECT count(*) FROM jobs j JOIN emails e ON e.id = j.email_id WHERE e.ingested_at >= '{t0}' AND j.status = 'failed'"
            )
        )
        text = screen("/")[1]
        # hand the backlog over until none of it is left: a job the worker holds at that moment (retrying "AI off")
        # goes back to the queue a moment later, and must not be picked up by the AI once it is switched on again
        left_sql = (f"SELECT count(*) FROM jobs j JOIN emails e ON e.id = j.email_id WHERE e.ingested_at >= '{t0}' "
                    "AND j.status IN ('queued', 'running')")  # fmt: skip
        handed_n, left = 0, n
        for _ in range(30):
            out = compose("exec", "-T", "worker", "python", "-m", "apex_agent.worker", "--to-people-since", t0, "--why",
                          f"S19 drill: AI off during a burst of {n} emails, quoted by hand").strip().splitlines()[-1]  # fmt: skip
            handed_n += json.loads(out)["handed_to_people"]
            left = int(sql(left_sql))
            if not left:
                break
            time.sleep(5)
        handed = json.dumps({"handed_to_people": handed_n, "left_in_queue": left})
    finally:
        subprocess.run(["uv", "run", "python", "-m", "agent.ops.killswitch", "on", "--by", "drill", "--reason", "S19 drill over"], check=True,
                       capture_output=True, cwd=REPO)  # fmt: skip
    return {"drill": "burst", "at": datetime.now().isoformat(timespec="minutes"), "sent": n, "ingested": got, "all_ingested": ingested,
            "ingest_minutes": ingest_min, "jobs_while_ai_off": jobs, "failed": failed, "manual_mode_banner": "AI is switched off" in text,
            "handed_to_people": json.loads(handed), "passed": ingested and got == n and failed == 0 and not left and "AI is switched off" in text}  # fmt: skip


def record(r: dict[str, Any]) -> None:
    allr = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    allr[r["drill"]] = r
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(allr, indent=1), encoding="utf-8")
    lines = [
        "# Resilience drills (S19)",
        "",
        "_Run by `uv run python -m scripts.drills …` against the live local stack. Raw results: `drills.json`._",
        "",
    ]
    for k in ("groq-down", "odoo-down", "worker-kill", "burst"):
        if k in allr:
            x = allr[k]
            lines += [
                f"## {k}: {'PASSED' if x['passed'] else 'FAILED'} ({x['at']})",
                "",
                *[f"- {a}: `{json.dumps(b)}`" for a, b in x.items() if a not in ("drill", "at", "passed")],
                "",
            ]
    (RESULTS.parent / "DRILLS.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    cmd, arg = sys.argv[1], int(sys.argv[2])
    if cmd == "groq-down":
        r = outage_drill("groq-down", arg, ["stop", "envoy-egress"], ["start", "envoy-egress"], "envoy-egress")
    elif cmd == "odoo-down":
        r = outage_drill("odoo-down", arg, ["stop", "odoo"], ["start", "odoo"], "odoo")
    elif cmd == "worker-kill":
        r = worker_kill(arg)
    elif cmd == "burst":
        r = burst(arg)
    else:
        raise SystemExit(__doc__)
    record(r)
    print(json.dumps(r))


if __name__ == "__main__":
    main()
