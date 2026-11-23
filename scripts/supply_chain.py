"""Supply-chain checks (S9), run locally by `make check` (no hosted CI):

  pip-audit  known vulnerabilities in the agent's locked Python packages (agent/requirements.lock)
  bandit     risky code patterns in our own code (medium and high severity; reviewed exceptions carry `# nosec <id> (why)`)
  trivy      HIGH/CRITICAL vulnerabilities with a fix available in our agent image (apex/agent:local)
  syft       an SBOM of the agent image -> data/generated/sbom/apex-agent.spdx.json

Scanner versions are pinned. trivy keeps its vulnerability database in the Docker volume `apex-trivy-cache`.
ponytail: only our own image is scanned; S19 scans every pinned image (Odoo, Envoy, LiteLLM...) and pins them by digest.

Usage: uv run python -m scripts.supply_chain
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "apex/agent:local"
TRIVY = "aquasec/trivy:0.75.0"
SYFT = "anchore/syft:v1.50.0"
DOCKER_SOCK = ["-v", "//var/run/docker.sock:/var/run/docker.sock"]  # // keeps Git Bash from rewriting the path


def run(name: str, cmd: list[str], **kw: object) -> bool:
    print(f"--- {name}", flush=True)
    r = subprocess.run(cmd, env={**os.environ, "PYTHONIOENCODING": "utf-8", "MSYS_NO_PATHCONV": "1"}, **kw)  # type: ignore[call-overload]
    print(f"--- {name}: {'ok' if r.returncode == 0 else 'FAILED'}", flush=True)
    return bool(r.returncode == 0)


def main() -> None:
    lock = (ROOT / "agent" / "requirements.lock").read_text(encoding="utf-8").splitlines()
    # the spaCy model is a GitHub release wheel, not a PyPI package: pip-audit can't look it up
    pinned = [ln for ln in lock if ln.strip() and not ln.startswith(("#", " ")) and " @ " not in ln]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(pinned) + "\n")
    sbom = ROOT / "data" / "generated" / "sbom"
    sbom.mkdir(parents=True, exist_ok=True)
    results = [
        run("pip-audit", ["uvx", "pip-audit==2.10.1", "--disable-pip", "--no-deps", "--progress-spinner", "off", "-r", f.name]),
        run("bandit", ["uvx", "bandit==1.9.4", "-q", "-r", "agent/src", "agent/ops", "-ll"], cwd=ROOT),
        run(
            "trivy",
            [
                "docker",
                "run",
                "--rm",
                *DOCKER_SOCK,
                "-v",
                "apex-trivy-cache:/root/.cache",
                TRIVY,
                "image",
                "--quiet",
                "--scanners",
                "vuln",
                "--severity",
                "HIGH,CRITICAL",
                "--ignore-unfixed",
                "--exit-code",
                "1",
                IMAGE,
            ],
        ),  # fmt: skip
        run(
            "syft (SBOM)",
            [
                "docker",
                "run",
                "--rm",
                *DOCKER_SOCK,
                "-v",
                f"{sbom}:/out",
                SYFT,
                "scan",
                IMAGE,
                "-q",
                "-o",
                "spdx-json=/out/apex-agent.spdx.json",
            ],
        ),  # fmt: skip
    ]
    os.unlink(f.name)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
