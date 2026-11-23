"""Download the NIST MBE PMI test-case drawings (CTC 1-5, FTC 6-11) into data/reference/nist/.

These are real, publicly released engineering drawings with GD&T, used in S4 to check extraction on
drawings we did NOT generate ourselves. Skips files already present. Writes LICENSE_NOTES.md.

Run: uv run python scripts/download_nist.py
"""

import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "data" / "reference" / "nist"
PAGE = "https://www.nist.gov/ctl/smart-connected-systems-division/smart-connected-manufacturing-systems-group/mbe-pmi-0"
CASES = [f"ctc-{i:02d}" for i in range(1, 6)] + [f"ftc-{i:02d}" for i in range(6, 12)]
URL = "https://www.nist.gov/document/nist-cad-model-{case}"


def fetch(case: str) -> Path:
    dst = DEST / f"nist_{case.replace('-', '_')}.pdf"
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    req = urllib.request.Request(URL.format(case=case), headers={"User-Agent": "apex-fde-lab/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if not data.startswith(b"%PDF"):
        raise RuntimeError(f"{case}: not a PDF ({r.headers.get('Content-Type')})")
    dst.write_bytes(data)
    return dst


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in CASES:
        p = fetch(case)
        rows.append(f"| `{p.name}` | {URL.format(case=case)} | {p.stat().st_size // 1024} KB |")
        print(f"{p.name}: {p.stat().st_size // 1024} KB")
    notes = (
        f"""# NIST MBE PMI test drawings: source and licence

Downloaded by `scripts/download_nist.py` on {date.today().isoformat()}.

Source: NIST "MBE PMI Validation and Conformance Testing" project, {PAGE}

Licence wording on that page (Disclaimers):

> "The test cases, CAD models, and STEP files can be used without any restrictions."

NIST asks for acknowledgement when they are used and does not allow use of the NIST
logo in promotional materials. See also https://www.nist.gov/copyrights-disclaimers (information on NIST
sites is public information and may be distributed or copied, unless marked as copyrighted).

Acknowledgement: test drawings courtesy of the National Institute of Standards and Technology (NIST).

These are real third-party drawings (Combined Test Cases CTC 1-5 and Fully-toleranced Test Cases FTC 6-11).
We use them only to evaluate drawing extraction (S4); they are not Apex customer parts.

| File | URL | Size |
|---|---|---|
"""
        + "\n".join(rows)
        + "\n"
    )
    (DEST / "LICENSE_NOTES.md").write_text(notes, encoding="utf-8")
    print(f"{len(rows)} NIST drawings in {DEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
