"""Who really sent this? (S3 security gate)

- DKIM: the signature is verified cryptographically (dkimpy) with the public key the sender's domain
  publishes. The lab has no real DNS, so the TXT records come from a lab zone file
  (agent/ops/lab_dns.py exports the mail server's DKIM keys plus each domain's DMARC policy).
- DMARC: passes when DKIM passes AND the signing domain is the From domain (alignment).
- SPF: reported as "none". ponytail: lab mail never crosses an outside SMTP hop and the mail server strips
  submission Received headers, so there is no connecting IP to check. Upgrade: ENABLE_POLICYD_SPF on the
  mail server with real DNS, then read its Authentication-Results header here.
- Odoo: is the address a known customer? Does the DISPLAY NAME claim a customer whose address is different
  (display-name spoofing)? Asked through erp-mcp-read as node `security_gate` (OPA decides).
"""

import asyncio
import json
import re
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import dkim

from apex_agent.tools import call_tool


def dns_lookup(zone_file: str) -> Any:
    records: dict[str, str] = json.loads(Path(zone_file).read_text(encoding="utf-8"))

    def lookup(name: bytes, timeout: int = 5) -> bytes:
        return records.get(name.decode().rstrip(".").lower(), "").encode()

    return lookup


def _tag(record: str, tag: str) -> str | None:
    m = re.search(rf"(?:^|;)\s*{tag}\s*=\s*([^;]+)", record)
    return m.group(1).strip() if m else None


def authenticate(raw: bytes, from_header: str, zone_file: str) -> dict[str, Any]:
    lookup = dns_lookup(zone_file)
    domain = parseaddr(from_header)[1].rpartition("@")[2].lower()
    sig = re.search(rb"^DKIM-Signature:(.*?)(?=^\S)", raw, re.M | re.S)
    d = _tag(re.sub(r"\s+", "", sig.group(1).decode(errors="replace")), "d") if sig else None
    try:
        dkim_ok = bool(sig) and bool(dkim.verify(raw, dnsfunc=lookup))
    except dkim.DKIMException:
        dkim_ok = False
    policy = _tag(lookup(f"_dmarc.{domain}".encode()).decode(), "p")
    aligned = dkim_ok and (d or "").lower() == domain
    return {"from_domain": domain, "dkim": "pass" if dkim_ok else ("fail" if sig else "none"), "dkim_domain": d,
            "dmarc": "pass" if aligned else ("fail" if policy else "none"), "dmarc_policy": policy, "spf": "none"}  # fmt: skip


def _company(display: str) -> str:
    return re.sub(r"\(.*?\)|\b(?:sales|purchasing|procurement)\b", "", display, flags=re.I).strip(" -,")


def customer_check(from_header: str) -> dict[str, Any]:
    name, addr = parseaddr(from_header)

    async def ask() -> tuple[Any, Any]:
        by_email = await call_tool("erp-mcp-read", "customer_by_email", {"email": addr}, "security_gate")
        company = _company(name)
        by_name = await call_tool("erp-mcp-read", "find_customer", {"name": company}, "security_gate") if len(company) >= 4 \
            else (True, [])  # fmt: skip
        return by_email, by_name

    (ok1, by_email), (ok2, by_name) = asyncio.run(ask())
    if not (ok1 and ok2):  # OPA said no or the tool failed: unknown, and say why (fail closed -> human looks)
        return {"known_customer": None, "spoof": None, "error": str(by_email if not ok1 else by_name)[:200]}
    known = by_email[0] if by_email else None
    claimed = [c for c in by_name if c["name"].lower() == _company(name).lower()] or by_name
    spoof = bool(claimed) and (known is None or all(c["ref"] != known["ref"] for c in claimed))
    return {"known_customer": known["ref"] if known else None, "known_name": known["name"] if known else None,
            "display_name_claims": claimed[0]["name"] if spoof else None, "spoof": spoof}  # fmt: skip
