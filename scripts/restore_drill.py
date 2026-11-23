"""S19 (W16): a backup is worthless until a restore has worked. Restore the LATEST backup into throwaway containers
(never the live ones) and prove it:

- each database dump -> `pg_restore` into a scratch Postgres of the same image (postgres:16 for Odoo, pgvector pg17 for
  the agent); the row counts must equal the manifest's; the agent's hash-chained audit log must still verify
- the Odoo filestore tar -> unpacks, file count > 0
- the MinIO tar -> every object under raw/ and files/ is content-addressed (named by its sha256): re-hash EVERY one
- every backup file's sha256 = the manifest's (nothing changed on disk since)
Writes docs/runbooks/RESTORE_DRILL.md.

Usage: uv run python -m scripts.restore_drill [backups/<stamp>]
"""

import hashlib
import json
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
IMAGES = {"odoo-db": "postgres:16", "app-postgres": "pgvector/pgvector:0.8.7-pg17-trixie"}
CHECKS = {("app-postgres", "apex"): "SELECT (SELECT count(*) FROM emails) || '/' || (SELECT count(*) FROM estimates) || '/' || (SELECT count(*) FROM audit_log)",
          ("odoo-db", "apex"): "SELECT count(*) FROM sale_order"}  # fmt: skip


# the S9 audit chain, re-verified INSIDE the restored copy (same rule as apex_agent.audit.verify)
CHAIN_SQL = """WITH c AS (SELECT id, prev_hash = lag(hash, 1, repeat('0', 64)) OVER (ORDER BY id)
                           AND hash = encode(sha256(convert_to(prev_hash || audit_payload(a), 'UTF8')), 'hex') AS good FROM audit_log a)
               SELECT CASE WHEN bool_and(good) THEN 't' ELSE 'f' END || '|' || count(*) || '|' || coalesce(min(id) FILTER (WHERE NOT good)::text, '-') FROM c"""


def docker(*args: str, stdin: Path | None = None, timeout: int = 900) -> str:
    if stdin:
        with stdin.open("rb") as f:
            r = subprocess.run(["docker", *args], stdin=f, capture_output=True, timeout=timeout)
    else:
        r = subprocess.run(["docker", *args], capture_output=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {r.stderr.decode(errors='replace')[-500:]}")
    return r.stdout.decode(errors="replace")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scratch(image: str, name: str) -> None:
    docker("rm", "-f", name) if name in docker("ps", "-a", "--format", "{{.Names}}").split() else None
    docker(
        "run", "-d", "--name", name, "--network", "none", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", image
    )  # no network, no ports
    for _ in range(60):
        if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True).returncode == 0:
            time.sleep(1)
            return
        time.sleep(1)
    raise RuntimeError(f"{name} did not start")


def drill(src: Path) -> dict[str, Any]:
    m = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    res: dict[str, Any] = {"backup": src.name, "taken_at": m["taken_at"], "files_intact": True, "databases": {}, "ok": True}
    for name, meta in m["files"].items():
        if sha256(src / name) != meta["sha256"]:
            res["files_intact"] = res["ok"] = False
    started = time.time()
    for svc, image in IMAGES.items():
        box = f"apex-restore-drill-{svc}"
        scratch(image, box)
        try:
            for name, meta in m["files"].items():
                if meta.get("service") != svc:
                    continue
                db = meta["database"]
                docker("exec", box, "createdb", "-U", "postgres", db)
                if svc == "app-postgres":  # the roles the dump's GRANTs and owners refer to
                    for role in ("apex", "grafana_ro", "review_ui", "apex_audit", "whatif"):
                        docker(
                            "exec",
                            box,
                            "psql",
                            "-U",
                            "postgres",
                            "-qc",
                            f"DO $$BEGIN CREATE ROLE {role}; EXCEPTION WHEN duplicate_object THEN NULL; END$$",
                        )
                else:
                    docker(
                        "exec",
                        box,
                        "psql",
                        "-U",
                        "postgres",
                        "-qc",
                        "DO $$BEGIN CREATE ROLE odoo; EXCEPTION WHEN duplicate_object THEN NULL; END$$",
                    )
                docker(
                    "exec",
                    "-i",
                    box,
                    "pg_restore",
                    "-U",
                    "postgres",
                    "-d",
                    db,
                    "--no-owner",
                    "--exit-on-error",
                    stdin=src / name,
                    timeout=1800,
                )
                entry: dict[str, Any] = {"restored": True}
                if (svc, db) in CHECKS:
                    got = docker("exec", box, "psql", "-U", "postgres", "-d", db, "-Atc", CHECKS[(svc, db)]).strip()
                    entry.update(expected=m["counts"][f"{svc}:{db}"], got=got, counts_match=got == m["counts"][f"{svc}:{db}"])
                    res["ok"] &= entry["counts_match"]
                if (svc, db) == ("app-postgres", "apex"):
                    v = docker("exec", box, "psql", "-U", "postgres", "-d", db, "-Atc", CHAIN_SQL).strip()
                    ok, checked, bad = v.split("|")
                    entry["audit_chain"] = {"ok": ok == "t", "rows_checked": int(checked), "first_bad": bad}
                    res["ok"] &= ok == "t"
                res["databases"][f"{svc}:{db}"] = entry
        finally:
            docker("rm", "-f", box)
    with tarfile.open(src / "odoo_filestore.tar") as t:
        res["odoo_filestore_files"] = sum(1 for x in t.getmembers() if x.isfile())
    res["ok"] &= res["odoo_filestore_files"] > 0
    rehashed = mismatched = 0
    with tarfile.open(src / "minio_apex.tar") as t:
        objects = [x for x in t.getmembers() if x.isfile()]
        for x in objects:
            key = x.name.removeprefix("apex/")
            if key.startswith(("raw/", "files/")):  # content-addressed: the name IS the sha256 of the bytes
                data = t.extractfile(x).read()  # type: ignore[union-attr]
                rehashed += 1
                mismatched += hashlib.sha256(data).hexdigest() != key.split("/", 1)[1].removesuffix(".eml")
    res["minio"] = {
        "objects": len(objects),
        "expected": int(m["counts"]["minio_objects"]),
        "content_addressed_rehashed": rehashed,
        "hash_mismatch": mismatched,
    }
    res["ok"] &= len(objects) == int(m["counts"]["minio_objects"]) and mismatched == 0
    res["restore_minutes"] = round((time.time() - started) / 60, 1)
    return res


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else max((REPO / "backups").iterdir())
    r = drill(src if src.is_absolute() else REPO / src)
    out = REPO / "docs" / "runbooks" / "RESTORE_DRILL.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join([
        "# Restore drill (S19)", "",
        f"_Run {datetime.now().isoformat(timespec='minutes')} by `uv run python -m scripts.restore_drill` on backup `{r['backup']}` "
        f"(taken {r['taken_at']}), into throwaway containers with no network; the live system was not touched._", "",
        f"**Result: {'PASSED' if r['ok'] else 'FAILED'}** in {r['restore_minutes']} min of restoring.", "",
        f"- Backup files unchanged since the backup (sha256): **{r['files_intact']}**",
        *[f"- `{k}` restored: counts {v.get('got', '-')} (expected {v.get('expected', '-')})"
          + (f"; audit chain {'verifies' if v['audit_chain']['ok'] else 'BROKEN'} over {v['audit_chain']['rows_checked']} rows" if "audit_chain" in v else "")
          for k, v in r["databases"].items()],
        f"- Odoo filestore: {r['odoo_filestore_files']} files",
        f"- MinIO: {r['minio']['objects']} objects (expected {r['minio']['expected']}); {r['minio']['content_addressed_rehashed']} content-addressed "
        f"objects re-hashed, mismatches: **{r['minio']['hash_mismatch']}**", "",
        "How to restore for real: `docs/runbooks/RUNBOOKS.md` → *Restore from a backup*.", ""]), encoding="utf-8")  # fmt: skip
    print(json.dumps(r))


if __name__ == "__main__":
    main()
