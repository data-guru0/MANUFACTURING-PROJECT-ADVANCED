"""The lab's DNS TXT records, as a file (S3). `.local` domains have no public DNS, so this file plays DNS for
the security gate's DKIM/DMARC check: each domain's DKIM public key (read from the mail server, exactly what
it would publish) and a DMARC policy. Run after the mail server has its DKIM keys (setup_mail). Idempotent.

Usage: uv run python -m agent.ops.lab_dns   -> data/generated/mail/lab_dns.json
"""

import json
import re
import subprocess

from legacy.odoo.rpc import ROOT

DOMAINS = ["apex.local", "customers.local", "suppliers.local"]
OUT = ROOT / "data" / "generated" / "mail" / "lab_dns.json"
COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(ROOT / "docker-compose.legacy.yml")]


def main() -> None:
    records = {}
    for d in DOMAINS:
        txt = subprocess.run([*COMPOSE, "exec", "-T", "mail", "cat", f"/tmp/docker-mailserver/opendkim/keys/{d}/mail.txt"],  # nosec B108 (a path inside the mail container, not a temp file)
                             check=True, capture_output=True, text=True).stdout  # fmt: skip
        records[f"mail._domainkey.{d}"] = "".join(re.findall(r'"([^"]*)"', txt))
        records[f"_dmarc.{d}"] = f"v=DMARC1; p=reject; rua=mailto:postmaster@{d}"
    OUT.write_text(json.dumps(records, indent=1), encoding="utf-8")
    print(f"lab dns: {len(records)} TXT records for {', '.join(DOMAINS)} -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
