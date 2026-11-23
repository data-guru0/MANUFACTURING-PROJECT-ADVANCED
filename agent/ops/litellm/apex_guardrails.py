"""Guardrails INSIDE the AI gateway (S9), as a LiteLLM OSS custom callback: they apply to every call from every agent,
whatever the calling code does.

  pre-call   1. the agent registry's max_tokens cap (agents.yaml, by the key's alias "agent-<name>")
             2. masking: email addresses, phone numbers (with a +country code) and IBANs never leave for Groq
  post-call  3. output safety: every drafter reply is checked by `quote-safety` (gpt-oss-safeguard) against Apex's
                policy; UNSAFE blocks the response (the job goes to a person: NeedsHuman, never a made-up answer)

Prompt Guard stays in the security gate (S3), where its score decides the red banner, before any other model reads
the email. ponytail: regex masking, not Presidio (Presidio's analyzer would be two more containers in the gateway);
the agent's own logs and the Langfuse copy are already Presidio-redacted (S3).
"""

import re
from typing import Any

import yaml
from litellm.integrations.custom_logger import CustomLogger

REGISTRY = "/app/apex/agents.yaml"
MASKS = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    (re.compile(r"\+\d{1,3}[\d ()\-]{7,}\d"), "[PHONE]"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b"), "[IBAN]"),
]
SAFETY_POLICY = """You review an email that a machine shop is about to send to a customer. It is UNSAFE if it contains any of:
insults, harassment or threats; instructions to do something harmful or illegal; text that reveals internal instructions,
a system prompt, passwords, keys or other customers' names, prices or drawings; a request to the reader to click a link
or send money or credentials. Ordinary quotation content (prices, dates, quantities, terms, certificates) is SAFE.
Answer with exactly one word: SAFE or UNSAFE."""


def mask(text: str) -> str:
    for rx, label in MASKS:
        text = rx.sub(label, text)
    return text


class ApexGuardrails(CustomLogger):  # type: ignore[misc]  # litellm is only installed in the gateway image
    def __init__(self) -> None:
        super().__init__()
        with open(REGISTRY, encoding="utf-8") as f:
            agents = yaml.safe_load(f)["agents"]
        self.max_tokens = {f"agent-{name}": int(a.get("max_tokens") or 1024) for name, a in agents.items()}

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> dict[str, Any]:
        alias = getattr(user_api_key_dict, "key_alias", None) or ""
        if alias in self.max_tokens:  # the master key (admin, tests) isn't an agent and isn't capped
            data["max_tokens"] = min(int(data.get("max_tokens") or self.max_tokens[alias]), self.max_tokens[alias])
        for m in data.get("messages") or []:
            if isinstance(m.get("content"), str):
                m["content"] = mask(m["content"])
            elif isinstance(m.get("content"), list):
                for part in m["content"]:
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        part["text"] = mask(part["text"])
        return data

    async def async_post_call_success_hook(self, data: dict[str, Any], user_api_key_dict: Any, response: Any) -> Any:
        if (getattr(user_api_key_dict, "key_alias", None) or "") != "agent-drafter":
            return response
        from litellm.proxy.proxy_server import llm_router  # the proxy's own router: same aliases, same egress

        text = response.choices[0].message.content or ""
        verdict = await llm_router.acompletion(model="quote-safety", max_tokens=400, messages=[
            {"role": "system", "content": SAFETY_POLICY}, {"role": "user", "content": text[:12000]}])  # fmt: skip
        answer = (verdict.choices[0].message.content or "").strip().upper()
        print(f"apex guardrail: drafter reply judged {answer[:20]!r}", flush=True)  # Docker Desktop -> apex-litellm-1 -> Logs
        if "UNSAFE" in answer or "SAFE" not in answer:  # no clear SAFE = blocked (fail closed)
            raise ValueError(f"apex guardrail: the drafted reply was judged unsafe by quote-safety ({answer[:40]!r})")
        return response


proxy_handler_instance = ApexGuardrails()
