"""S19 (W16): back up everything that can't be rebuilt, into backups/<timestamp>/ (git-ignored). Local only.

- every database of both Postgres instances (`pg_dump -Fc`): odoo-db (Odoo: the system of record) and app-postgres
  (the agent: jobs, estimates, the audit log, Langfuse/LiteLLM state...)
- Odoo's filestore (attachments such as the drawings live there, NOT in the database)
- the MinIO bucket `apex` (raw emails, files, models, redacted drawings)
- manifest.json: every file's size and sha256, and row/object counts the restore drill checks against

Not backed up (rebuildable): ClickHouse (Langfuse traces), Prometheus, Redis cache. Secrets stay in .env (back that up
separately, encrypted; never next to the data).

Usage: uv run python -m scripts.backup
"""

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

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
INSTANCES = {"odoo-db": "odoo", "app-postgres": "postgres"}  # service -> superuser
COUNTS = {  # what the restore drill compares ((service, database) -> query); both instances have a database "apex"
    (
        "app-postgres",
        "apex",
    ): "SELECT (SELECT count(*) FROM emails) || '/' || (SELECT count(*) FROM estimates) || '/' || (SELECT count(*) FROM audit_log)",
}


MINIO_TAR = """
import io, sys, tarfile
from apex_agent.ingest.store import Store
st = Store()
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as t:
    for o in st.client.list_objects(st.bucket, recursive=True):
        data = st.get(o.object_name)
        info = tarfile.TarInfo("apex/" + o.object_name)
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
"""


def run(args: list[str], out: Path | None = None) -> str:
    if out:
        with out.open("wb") as f:
            r = subprocess.run([*COMPOSE, *args], stdout=f, stderr=subprocess.PIPE, timeout=1800)
    else:
        r = subprocess.run([*COMPOSE, *args], capture_output=True, timeout=600)
    if r.returncode:
        raise SystemExit(f"{' '.join(args[:4])}: {r.stderr.decode(errors='replace')[-600:]}")
    return "" if out else r.stdout.decode(errors="replace")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup() -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = REPO / "backups" / stamp
    dest.mkdir(parents=True)
    manifest: dict[str, Any] = {"taken_at": datetime.now().isoformat(timespec="seconds"), "files": {}, "counts": {}}
    for svc, user in INSTANCES.items():
        dbs = run(["exec", "-T", svc, "psql", "-U", user, "-d", "postgres", "-Atc",
                   "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY 1"]).split()  # fmt: skip
        for db in dbs:
            f = dest / f"{svc}__{db}.dump"
            run(["exec", "-T", svc, "pg_dump", "-U", user, "-Fc", db], out=f)
            manifest["files"][f.name] = {"bytes": f.stat().st_size, "sha256": sha256(f), "service": svc, "database": db}
            if (svc, db) in COUNTS:
                manifest["counts"][f"{svc}:{db}"] = run(
                    ["exec", "-T", svc, "psql", "-U", user, "-d", db, "-Atc", COUNTS[(svc, db)]]
                ).strip()
            if svc == "odoo-db":
                manifest["counts"][f"{svc}:{db}"] = run(["exec", "-T", svc, "psql", "-U", user, "-d", db, "-Atc",
                                                        "SELECT count(*) FROM sale_order"]).strip()  # fmt: skip
    f = dest / "odoo_filestore.tar"
    run(["exec", "-T", "odoo", "tar", "-C", "/var/lib/odoo", "-cf", "-", "filestore"], out=f)
    manifest["files"][f.name] = {"bytes": f.stat().st_size, "sha256": sha256(f)}
    # MinIO: streamed as a tar by the knowledge job with the scoped `apex-agent` user (no root key, no tar in the image)
    f = dest / "minio_apex.tar"
    run(["run", "--rm", "-T", "knowledge", "python", "-c", MINIO_TAR], out=f)
    manifest["files"][f.name] = {"bytes": f.stat().st_size, "sha256": sha256(f)}
    import tarfile

    with tarfile.open(f) as t:
        manifest["counts"]["minio_objects"] = str(sum(1 for m in t.getmembers() if m.isfile()))
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return {"dir": str(dest.relative_to(REPO)), "files": len(manifest["files"]),
            "megabytes": round(sum(v["bytes"] for v in manifest["files"].values()) / 1e6, 1), "counts": manifest["counts"]}  # fmt: skip


if __name__ == "__main__":
    print(json.dumps(backup()))
