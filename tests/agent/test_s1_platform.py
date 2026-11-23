"""S1 Done-when, against the live agent stack (make demo-agent):
- a real Groq completion through LiteLLM shows up in Langfuse with a $ cost;
- the triage key is refused when it asks for the reasoner model;
- the agent container cannot reach the internet directly (and the egress proxy only allows its list);
- gitleaks is clean.
Plus: every agent service healthy, only the ingress publishes ports, OPA fails closed, settings fail fast."""

import json
import ssl
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.ops.litellm.keys import registry
from apex_agent.llm.client import AiDisabled, LLMClient
from apex_agent.settings import ConfigError, Settings, load
from legacy.odoo.rpc import env
from scripts.gen_secrets import new_value

REPO = Path(__file__).resolve().parents[2]
CA = str(REPO / "data" / "generated" / "certs" / "ca.crt")
TLS = ssl.create_default_context(cafile=CA)
COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(REPO / "docker-compose.legacy.yml"), "-f",
           str(REPO / "docker-compose.agent.yml")]  # fmt: skip
LITELLM, LANGFUSE = "https://localhost:8446", "https://localhost:8443"
AGENT_SERVICES = {"app-postgres", "minio", "redis", "clickhouse", "opa", "litellm", "langfuse-worker", "langfuse-web",
                  "keycloak", "envoy-egress", "envoy-ingress", "prometheus", "grafana", "clamav", "erp-mcp-read",
                  "erp-mcp-write", "share-mcp"}  # fmt: skip


def compose(*args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*COMPOSE, *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout)


def host_settings(agent: str, opa_url: str = "http://127.0.0.1:1") -> Settings:
    return Settings(agent=agent, litellm_url=LITELLM, llm_key=env(f"LITELLM_KEY_{agent.upper()}"),  # type: ignore[arg-type]
                    opa_url=opa_url, ca_file=CA)  # fmt: skip


def test_every_agent_service_is_healthy() -> None:
    rows = [json.loads(line) for line in compose("ps", "--format", "json").stdout.splitlines() if line.strip()]
    health = {r["Service"]: r.get("Health") or r["State"] for r in rows}
    missing = AGENT_SERVICES - set(health)
    assert not missing, f"not running: {missing}"
    assert all(health[s] == "healthy" for s in AGENT_SERVICES), {s: health[s] for s in AGENT_SERVICES}


def test_only_the_ingress_publishes_ports_and_only_on_localhost() -> None:
    rows = [json.loads(line) for line in compose("ps", "--format", "json").stdout.splitlines() if line.strip()]
    published = {r["Service"]: [p for p in r.get("Publishers") or [] if p.get("PublishedPort")] for r in rows}
    agent_ports = {s: p for s, p in published.items() if s in AGENT_SERVICES and p}
    assert set(agent_ports) == {"envoy-ingress"}, agent_ports
    assert all(p["URL"] == "127.0.0.1" for p in agent_ports["envoy-ingress"])


@pytest.fixture(scope="module")
def selfcheck() -> dict[str, Any]:
    r = compose("run", "--rm", "--build", "agent", "python", "-m", "apex_agent.selfcheck", timeout=900)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    out: dict[str, Any] = json.loads(r.stdout.strip().splitlines()[-1])
    return out


@pytest.mark.llm
def test_agent_container_gets_a_real_completion_and_has_no_internet(selfcheck: dict[str, Any]) -> None:
    assert selfcheck["opa_ai_enabled"] is True
    assert "READY" in selfcheck["completion"]["text"].upper()
    assert selfcheck["completion"]["cost_usd"] > 0, "LiteLLM should price the call"
    assert selfcheck["direct_internet"] == "blocked"


@pytest.mark.llm
def test_the_call_appears_in_langfuse_with_a_cost(selfcheck: dict[str, Any]) -> None:
    """Langfuse v4 serves traces through the v2 observations API (the old /traces API is off in v4)."""
    trace_id = selfcheck["completion"]["trace_id"]
    auth = (env("LANGFUSE_PUBLIC_KEY"), env("LANGFUSE_SECRET_KEY"))
    url = f"{LANGFUSE}/api/public/v2/observations?traceId={trace_id}&fields=core,basic,usage,model"
    end, gen = time.time() + 120, dict[str, Any]()
    while time.time() < end and not gen:  # ingestion is asynchronous (worker -> ClickHouse)
        data = httpx.get(url, auth=auth, verify=TLS, timeout=30).json().get("data") or []
        gen = next((o for o in data if o["type"] == "GENERATION" and (o.get("totalCost") or 0) > 0), {})
        time.sleep(0 if gen else 3)
    assert gen, f"trace {trace_id} never reached Langfuse with a cost"
    assert gen["model"].startswith("groq/") and gen["name"] == "triage:quote-triage"
    assert gen["totalUsage"] > 0 and gen["totalCost"] > 0


@pytest.mark.llm
def test_triage_key_is_refused_for_the_reasoner_model() -> None:
    r = httpx.post(f"{LITELLM}/chat/completions", verify=TLS, timeout=60,
                   headers={"Authorization": f"Bearer {env('LITELLM_KEY_TRIAGE')}"},
                   json={"model": "quote-reasoner", "max_tokens": 5,
                         "messages": [{"role": "user", "content": "hi"}]})  # fmt: skip
    assert r.status_code in (400, 401, 403), r.text
    assert "quote-reasoner" in r.text or "not allowed" in r.text.lower()


@pytest.mark.llm
def test_inside_the_container_our_client_turns_that_refusal_into_not_allowed(selfcheck: dict[str, Any]) -> None:
    """Same refusal through the real client, real OPA and real LiteLLM: NotAllowed, not a retry, not an answer."""
    assert selfcheck["reasoner_with_triage_key"] == "NotAllowed"


def test_opa_unreachable_fails_closed_before_any_llm_call() -> None:
    c = LLMClient(host_settings("triage", opa_url="http://127.0.0.1:1"))
    with pytest.raises(AiDisabled):
        c.chat("quote-triage", [{"role": "user", "content": "hi"}])


def test_each_agent_key_is_registered_with_only_its_models() -> None:
    for name, a in registry().items():
        r = httpx.get(
            f"{LITELLM}/key/info",
            verify=TLS,
            timeout=30,
            headers={"Authorization": f"Bearer {env(f'LITELLM_KEY_{name.upper()}')}"},
        )
        assert r.status_code == 200, (name, r.text[:200])
        info = r.json()["info"]
        assert sorted(info["models"]) == sorted(a["models"]), name
        assert info["max_budget"] == a["budget_usd"] and info["rpm_limit"] == a["rpm"], name


def test_egress_allows_groq_and_denies_everything_else() -> None:
    """Ask Envoy (from the LiteLLM container) to tunnel to Groq and to example.com; judge by Envoy's own decision."""
    probe = (
        "import urllib.request\n"
        "for u in ('https://api.groq.com/openai/v1/models', 'https://example.com'):\n"
        "    try: urllib.request.urlopen(u, timeout=10)\n"
        "    except Exception: pass\n"
    )
    compose("exec", "-T", "litellm", "python3", "-c", probe)
    connects: list[str] = []
    for _ in range(15):  # Envoy flushes its access log every few seconds
        log = compose("logs", "--since", "1m", "envoy-egress").stdout
        connects = [ln for ln in log.splitlines() if '"CONNECT' in ln]
        if any('"example.com:443"' in ln for ln in connects):
            break
        time.sleep(2)
    assert any('"api.groq.com:443"' in ln and '" 200 ' in ln for ln in connects), connects[-5:]
    assert any('"example.com:443"' in ln and '" 403 ' in ln for ln in connects), connects[-5:]


def test_settings_fail_fast_with_the_missing_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("APEX_AGENT", "APEX_LITELLM_URL", "APEX_LLM_KEY", "APEX_OPA_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError, match="APEX_LLM_KEY"):
        load()


def test_generated_secrets_have_the_formats_services_require() -> None:
    assert new_value("LITELLM_KEY_TRIAGE").startswith("sk-") and new_value("LITELLM_MASTER_KEY").startswith("sk-")
    assert new_value("LANGFUSE_PUBLIC_KEY").startswith("pk-lf-") and new_value("LANGFUSE_SECRET_KEY").startswith("sk-lf-")
    key = new_value("LANGFUSE_ENCRYPTION_KEY")
    assert len(key) == 64 and int(key, 16) >= 0


def test_gitleaks_finds_no_secrets() -> None:
    r = subprocess.run(["docker", "run", "--rm", "-v", f"{REPO}:/repo:ro", "zricethezav/gitleaks:v8.30.0", "dir", "/repo",
                        "--config", "/repo/.gitleaks.toml", "--redact", "--no-banner"],
                       capture_output=True, text=True, encoding="utf-8", timeout=600)  # fmt: skip
    assert r.returncode == 0 and "no leaks found" in (r.stdout + r.stderr), (r.stdout + r.stderr)[-1500:]
