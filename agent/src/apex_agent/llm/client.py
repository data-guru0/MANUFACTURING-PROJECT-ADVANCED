"""The agent's only way to call an LLM: through the LiteLLM proxy, with this agent's virtual key and a
model ALIAS (never a provider model name). Before every call it asks OPA whether AI is enabled (the kill
switch); after LiteLLM's own retries and fallbacks, any failure becomes NeedsHuman, never a made-up answer."""

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from apex_agent import audit, opa
from apex_agent.settings import Settings


class NeedsHuman(Exception):
    """The AI step can't be done now; hand the job to a person (manual mode)."""


class AiUnavailable(NeedsHuman):
    """S19: LiteLLM or the model provider can't be reached (network, 5xx): the work waits, it doesn't fail."""


class AiDisabled(NeedsHuman):
    """OPA said no (kill switch on, or OPA unreachable: fail closed)."""


class QuotaExhausted(NeedsHuman):
    """The provider's DAILY quota for this model is used up: the work should wait, not fail.
    `retry_after_s`: when the provider says it frees up (Groq's quota is a rolling 24 h window: "try again in 3m30s")."""

    def __init__(self, msg: str, retry_after_s: float | None = None) -> None:
        super().__init__(msg)
        self.retry_after_s = retry_after_s


def try_again_in(text: str) -> float | None:
    """Seconds from the provider's "Please try again in 1h2m3.5s" (None if it doesn't say)."""
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", text)
    if not m or not any(m.groups()):
        return None
    h, mi, sec = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)


class NotAllowed(Exception):
    """LiteLLM refused this key for this alias or budget: a configuration/governance problem, not a retry."""


@dataclass
class Completion:
    text: str
    alias: str
    cost_usd: float  # as computed by LiteLLM (x-litellm-response-cost)
    call_id: str  # x-litellm-call-id, to find the call in LiteLLM's logs
    trace_id: str  # the Langfuse trace id (32 lowercase hex, W3C)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # S16: the model asked for these tools (OpenAI format)


class LLMClient:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.spent_usd = 0.0  # running total of LiteLLM's x-litellm-response-cost (the graph's per-job budget, S6)
        self.email_id: int | None = None  # S9: the job this client is working on (set by the worker); tags + audit
        self.http = httpx.Client(base_url=s.litellm_url, timeout=s.llm_timeout_s, verify=s.tls(),
                                 headers={"Authorization": f"Bearer {s.llm_key.get_secret_value()}"})  # fmt: skip

    def chat(self, alias: str, messages: list[dict[str, Any]], *, max_tokens: int = 512,
             trace_id: str | None = None, tags: list[str] | None = None,
             response_format: dict[str, Any] | None = None, redacted: str | None = None,
             reasoning_effort: str | None = None, cache: bool = False,
             tools: list[dict[str, Any]] | None = None) -> Completion:  # fmt: skip
        """`redacted`: for untrusted, personal content (email). LiteLLM then keeps the messages OUT of its logs and
        the Langfuse trace, and the trace shows this Presidio-redacted version instead (S3).
        A rate limit (429) is waited out (Retry-After, else 10/20/40/60 s), then NeedsHuman: the job is retried later."""
        if not opa.allowed(self.s.opa_url, "apex/system/ai_enabled", {"agent": self.s.agent, "alias": alias}, self.s.tls()):
            raise AiDisabled("OPA denied the AI step (kill switch or OPA unreachable)")
        trace_id = trace_id or uuid.uuid4().hex
        body = {"model": alias, "messages": messages, "max_tokens": max_tokens,
                "metadata": {"trace_id": trace_id, "generation_name": f"{self.s.agent}:{alias}",
                             "tags": [f"agent:{self.s.agent}", f"job:{self.email_id}", *(tags or [])]}}  # fmt: skip
        if response_format:
            body["response_format"] = response_format
        if tools:  # S16: tool calling (the what-if assistant's read-only tools)
            body["tools"] = tools
        if cache:  # S9: an identical deterministic call is answered from LiteLLM's Redis cache (no tokens, no cost)
            body["cache"] = {"use-cache": True}
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort  # gpt-oss: fewer thinking tokens for simple tasks
        headers = {}
        if redacted is not None:
            body["metadata"]["redacted_input"] = redacted  # type: ignore[index]
            headers["x-litellm-enable-message-redaction"] = "true"
        for wait in (10, 20, 40, 60, None):
            try:
                r = self.http.post("/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as e:
                raise AiUnavailable(f"LiteLLM unreachable: {type(e).__name__}") from e
            if r.status_code != 429 or wait is None or "per day" in r.text:  # a daily quota won't free up in a minute
                break
            time.sleep(min(float(r.headers.get("retry-after") or wait), 90))
        if r.status_code in (401, 403) or (r.status_code == 400 and "not allowed" in r.text.lower()):
            raise NotAllowed(f"LiteLLM refused {self.s.agent} for {alias}: {r.status_code} {r.text[:200]}")
        if r.status_code == 429 and "per day" in r.text:
            raise QuotaExhausted(f"daily quota reached for {alias}", try_again_in(r.text))
        if r.status_code >= 400:
            if r.status_code >= 500:  # S19: the provider (Groq) or LiteLLM is down: an outage, not the job's fault
                raise AiUnavailable(f"LiteLLM error {r.status_code}: {r.text[:200]}")
            raise NeedsHuman(f"LiteLLM error {r.status_code}: {r.text[:200]}")
        data = r.json()
        self.spent_usd += float(r.headers.get("x-litellm-response-cost") or 0.0)
        audit.record("llm_call", self.s.agent, {
            "alias": alias, "model": data.get("model"), "cost_usd": float(r.headers.get("x-litellm-response-cost") or 0.0),
            "usage": data.get("usage"), "call_id": r.headers.get("x-litellm-call-id", ""), "trace_id": trace_id, "tags": tags or [],
            "cache_hit": r.headers.get("x-litellm-cache-key") is not None,
            "input_preview": redacted[:300] if redacted is not None else "[not kept here: see the Langfuse trace]",
        }, self.email_id)  # fmt: skip
        msg = data["choices"][0]["message"]
        return Completion(text=msg.get("content") or "", alias=alias,
                          cost_usd=float(r.headers.get("x-litellm-response-cost") or 0.0),
                          call_id=r.headers.get("x-litellm-call-id", ""), trace_id=trace_id,
                          tool_calls=list(msg.get("tool_calls") or []))  # fmt: skip
