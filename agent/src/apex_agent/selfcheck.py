"""Platform self-check, run INSIDE the agent container (S1 Done-when):
1. configuration loads (fail fast);
2. OPA answers the kill-switch question;
3. a real Groq completion through LiteLLM with this agent's key (cost + trace id for Langfuse);
4. the triage key is refused for the reasoner model (NotAllowed);
5. the container can NOT reach the internet directly (no route; only LiteLLM may go out, via Envoy).
Prints one JSON line. Usage: docker compose ... run --rm agent python -m apex_agent.selfcheck
"""

import json
import sys

import httpx

from apex_agent import opa
from apex_agent.llm.client import LLMClient, NeedsHuman, NotAllowed
from apex_agent.settings import load


def internet_reachable() -> str | None:
    """None if blocked; otherwise what answered (a failure of the platform's egress design)."""
    for url in ("https://api.groq.com/openai/v1/models", "https://example.com"):
        try:
            r = httpx.get(url, timeout=5)
            return f"{url} answered {r.status_code}"
        except httpx.HTTPError:
            continue
    return None


def main() -> None:
    s = load()
    out: dict[str, object] = {"agent": s.agent, "opa_ai_enabled": opa.allowed(s.opa_url, "apex/system/ai_enabled", None, s.tls())}
    try:
        c = LLMClient(s).chat("quote-triage", [{"role": "user", "content": "Reply with exactly the word: READY"}],
                              max_tokens=200, tags=["selfcheck"])  # fmt: skip
    except NeedsHuman as e:  # kill switch, OPA down, LiteLLM/Groq down: manual mode, never a made-up answer
        out["completion"] = f"{type(e).__name__}: {e} -> manual mode"
        print(json.dumps(out))
        sys.exit(2)
    out["completion"] = {"text": c.text.strip(), "cost_usd": c.cost_usd, "trace_id": c.trace_id, "call_id": c.call_id}
    try:  # this agent's key must not be able to use the big model (registry: triage -> quote-triage only)
        LLMClient(s).chat("quote-reasoner", [{"role": "user", "content": "hi"}], max_tokens=5)
        out["reasoner_with_triage_key"] = "ANSWERED (governance failure)"
    except NotAllowed:
        out["reasoner_with_triage_key"] = "NotAllowed"
    leak = internet_reachable()
    out["direct_internet"] = leak or "blocked"
    print(json.dumps(out))
    sys.exit(1 if leak else 0)


if __name__ == "__main__":
    main()
