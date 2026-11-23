"""S2: the agent's own Odoo accounts, replacing the shared `office` login (W3). Idempotent.

Run once with the Odoo admin login (in a real engagement Vikram runs it; SOW §5):
- group "Apex agent: read-only" with READ access (no write/create/delete) on the business models;
- `erp_reader`  = internal user + that group only: everything the agent reads goes through it;
- `erp_integration` = internal user + Sales and Purchase user rights (+ read group): the only account
  allowed to create things (draft quotations in S8), used only by erp-mcp's write tool set.
Passwords come from .env (ODOO_READER_PASSWORD, ODOO_INTEGRATION_PASSWORD).

S3 adds two more least-privilege accounts:
- MinIO user `apex-agent`: read/write on the bucket `apex` only (MINIO_AGENT_PASSWORD);
- Postgres role `grafana_ro`: SELECT on the triage tables only, for Grafana's "Inbox triage" dashboard.

S7 adds the people and the review screen's own accounts (W3: every decision has a named person):
- Keycloak realm `apex`: users arjun (estimator), ramesh (senior_estimator), kavita (planner), meera (auditor) and
  vikram (admin), with KEYCLOAK_STAFF_PASSWORD; the `review-ui` client becomes confidential (REVIEW_UI_CLIENT_SECRET,
  used only by Envoy's oauth2 filter) and may send users back to the screen after logout;
- Postgres role `review_ui`: SELECT on the review tables, UPDATE on jobs(status, decision) only;
- MinIO user `review-ui`: read raw/ and files/ only (never quarantine/, never write).

Usage: uv run python -m agent.ops.provision_access
"""

import json
import os
import re
import ssl
import subprocess
from typing import Any

import httpx

from legacy.odoo.rpc import ROOT, Odoo, env

READ_GROUP = "Apex agent: read-only"
PARTS_GROUP = "Apex agent: new parts"  # S8: create a new part as a product (no delete)
PRICES_GROUP = "Apex agent: supplier prices"  # S17: record a supplier's quoted price (create only; history is kept)
READ_MODELS = [
    "product.template", "product.product", "product.category", "uom.uom", "mrp.bom", "mrp.bom.line",
    "mrp.routing.workcenter", "mrp.workcenter", "mrp.production", "mrp.workorder", "resource.calendar",
    "resource.calendar.attendance", "sale.order", "sale.order.line", "purchase.order", "purchase.order.line",
    "product.supplierinfo", "stock.quant", "stock.location", "stock.picking", "stock.move", "res.partner",
    "ir.attachment", "res.currency", "res.company", "res.partner.category",  # S5: customer tier tags
]  # fmt: skip
USERS = {
    "erp_reader": ("Apex agent (read-only)", "ODOO_READER_PASSWORD", ["base.group_user"]),
    "erp_integration": ("Apex agent (integration)", "ODOO_INTEGRATION_PASSWORD",
                        ["base.group_user", "sales_team.group_sale_salesman", "purchase.group_purchase_user"]),
}  # fmt: skip


def main() -> None:
    o = Odoo("admin", env("ODOO_ADMIN_PASSWORD"))
    group = o.ensure("res.groups", [("name", "=", READ_GROUP)], {"name": READ_GROUP})
    models = {m["model"]: m["id"] for m in o.call("ir.model", "search_read", [("model", "in", READ_MODELS)], fields=["model"])}
    missing = set(READ_MODELS) - set(models)
    if missing:
        raise SystemExit(f"models not installed in Odoo: {sorted(missing)}")
    for name, model_id in models.items():
        o.ensure("ir.model.access", [("group_id", "=", group), ("model_id", "=", model_id)],
                 {"name": f"apex_agent_read_{name}", "group_id": group, "model_id": model_id, "perm_read": True,
                  "perm_write": False, "perm_create": False, "perm_unlink": False})  # fmt: skip
    # S8: erp_integration may add a NEW part as a product (create/write, never delete) when it drafts its quotation.
    parts = o.ensure("res.groups", [("name", "=", PARTS_GROUP)], {"name": PARTS_GROUP})
    for name in ("product.template", "product.product"):
        model_id = o.call("ir.model", "search", [("model", "=", name)], limit=1)[0]
        o.ensure("ir.model.access", [("group_id", "=", parts), ("model_id", "=", model_id)],
                 {"name": f"apex_agent_new_parts_{name}", "group_id": parts, "model_id": model_id, "perm_read": True,
                  "perm_write": True, "perm_create": True, "perm_unlink": False})  # fmt: skip
    # S17: erp_integration may ADD a supplier price-list entry (never change or delete an old one: the history is W13's evidence)
    prices = o.ensure("res.groups", [("name", "=", PRICES_GROUP)], {"name": PRICES_GROUP})
    model_id = o.call("ir.model", "search", [("model", "=", "product.supplierinfo")], limit=1)[0]
    o.ensure("ir.model.access", [("group_id", "=", prices), ("model_id", "=", model_id)],
             {"name": "apex_agent_supplier_prices", "group_id": prices, "model_id": model_id, "perm_read": True,
              "perm_write": False, "perm_create": True, "perm_unlink": False})  # fmt: skip
    for login, (name, secret, xmlids) in USERS.items():
        groups = [o.xmlid(x) for x in xmlids] + [group] + ([parts, prices] if login == "erp_integration" else [])
        o.ensure("res.users", [("login", "=", login)], {"login": login, "name": name, "password": env(secret),
                                                        "groups_id": [(6, 0, groups)]})  # fmt: skip
    print(
        f"odoo access: group '{READ_GROUP}' reads {len(models)} models; '{PARTS_GROUP}' (erp_integration) creates products, '{PRICES_GROUP}' adds supplier prices; users {', '.join(USERS)} ready"
    )
    minio_user("apex-agent", "MINIO_AGENT_PASSWORD", MINIO_POLICY)
    minio_user("review-ui", "MINIO_REVIEW_PASSWORD", MINIO_REVIEW_POLICY)
    minio_user("whatif", "MINIO_WHATIF_PASSWORD", MINIO_WHATIF_POLICY)
    grafana_role()
    keycloak_people()


COMPOSE = ["docker", "compose", "-p", "apex", "-f", str(ROOT / "docker-compose.legacy.yml"), "-f",
           str(ROOT / "docker-compose.agent.yml")]  # fmt: skip
MINIO_POLICY = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": ["s3:GetBucketLocation", "s3:ListBucket"], "Resource": ["arn:aws:s3:::apex"]},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": ["arn:aws:s3:::apex/*"]},
]}  # fmt: skip
MINIO_REVIEW_POLICY = {"Version": "2012-10-17", "Statement": [  # read only; the client asks the bucket's location first
    {"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": ["arn:aws:s3:::apex"]},
    {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::apex/raw/*", "arn:aws:s3:::apex/files/*",
                                                                 "arn:aws:s3:::apex/redacted/*"]},  # S17: what suppliers would see
]}  # fmt: skip
MINIO_WHATIF_POLICY = {"Version": "2012-10-17", "Statement": [  # S16: read the models (cycle time, win) only
    {"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": ["arn:aws:s3:::apex"]},
    {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::apex/models/*"]},
]}  # fmt: skip


def minio_user(user: str, secret: str, policy: dict[str, Any]) -> None:
    """ponytail: the password is an argument of `mc` inside the MinIO container for a moment (never on the host's
    command line or in a log). Upgrade: MinIO's identity plugin with Keycloak (OIDC), S9."""
    script = ('mc alias set local http://127.0.0.1:9000 apex-admin "$MINIO_ROOT_PASSWORD" >/dev/null'
              f' && cat > /tmp/{user}.json'
              f' && mc admin policy create local {user} /tmp/{user}.json >/dev/null'
              f' && mc admin user add local {user} "$AGENT_PW" >/dev/null'
              f' && (mc admin policy attach local {user} --user {user} >/dev/null 2>&1 || true)'
              f' && mc admin user info local {user} --json')  # fmt: skip
    r = subprocess.run([*COMPOSE, "exec", "-T", "-e", "AGENT_PW", "minio", "sh", "-c", script], input=json.dumps(policy),
                       capture_output=True, text=True, env={**os.environ, "AGENT_PW": env(secret)})  # fmt: skip
    if r.returncode != 0:
        raise SystemExit(f"minio user failed: {r.stderr[-500:]}")
    where = ", ".join(res for st in policy["Statement"] for res in st["Resource"])
    print(f"minio access: user {user}, policies {json.loads(r.stdout).get('policyName')} ({where})")


TABLES = "emails, attachments, jobs, gate_results, extractions, estimates, graph_events, decisions, feedback, review_items, actions, outcomes, budget_alerts, eval_runs, canaries, capacity_snapshots, replies, digests, variance_jobs, variance_ops"
REVIEW_TABLES = "emails, attachments, jobs, extractions, estimates, decisions, feedback, review_items, actions, outcomes, capacity_snapshots, replies, digests, whatif_log, whatif_options, supplier_rfqs"


def sql_secret(name: str) -> str:
    """A password that goes into SQL text (via psql's stdin, never an argument): refuse anything that could break out."""
    value = env(name)
    if not re.fullmatch(r"[A-Za-z0-9_\-.~]+", value):
        raise SystemExit(f"{name} must be letters, digits or _-.~ only (run make secrets)")
    return value


ROLES_SQL = """
        SELECT 'CREATE ROLE grafana_ro LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro')\\gexec
        ALTER ROLE grafana_ro PASSWORD '<GRAFANA_PW>';
        GRANT CONNECT ON DATABASE apex TO grafana_ro;
        GRANT USAGE ON SCHEMA public TO grafana_ro;
        GRANT SELECT ON <TABLES> TO grafana_ro;
        SELECT 'CREATE ROLE review_ui LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'review_ui')\\gexec
        ALTER ROLE review_ui PASSWORD '<REVIEW_PW>';
        GRANT CONNECT ON DATABASE apex TO review_ui;
        GRANT USAGE ON SCHEMA public TO review_ui;
        GRANT SELECT ON <REVIEW_TABLES> TO review_ui;
        GRANT UPDATE (status, decision) ON jobs TO review_ui;
        GRANT INSERT ON whatif_options TO review_ui;
        GRANT UPDATE (status, approval, approved_by) ON supplier_rfqs TO review_ui;
        GRANT USAGE ON SEQUENCE whatif_options_id_seq TO review_ui;
        SELECT 'CREATE ROLE whatif LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'whatif')\\gexec
        ALTER ROLE whatif PASSWORD '<WHATIF_PW>';
        GRANT CONNECT ON DATABASE apex TO whatif;
        GRANT USAGE ON SCHEMA public TO whatif;
        GRANT SELECT ON estimates, extractions, commodity_index, whatif_log TO whatif;
        GRANT INSERT ON whatif_log TO whatif;
        GRANT USAGE ON SEQUENCE whatif_log_id_seq TO whatif;
        SELECT 'CREATE ROLE apex_audit LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'apex_audit')\\gexec
        ALTER ROLE apex_audit PASSWORD '<AUDIT_PW>';
        GRANT CONNECT ON DATABASE apex TO apex_audit;
        GRANT USAGE ON SCHEMA public TO apex_audit;
        GRANT INSERT ON audit_log TO apex_audit;
        GRANT SELECT ON audit_log TO apex, grafana_ro, review_ui;"""


def roles_sql() -> str:
    """The roles' SQL: a fixed template, filled with validated passwords (sql_secret) and our own table lists."""
    return (ROLES_SQL.replace("<GRAFANA_PW>", sql_secret("GRAFANA_DB_PASSWORD")).replace("<REVIEW_PW>", sql_secret("REVIEW_UI_DB_PASSWORD"))
            .replace("<AUDIT_PW>", sql_secret("AUDIT_DB_PASSWORD")).replace("<WHATIF_PW>", sql_secret("WHATIF_DB_PASSWORD")).replace("<REVIEW_TABLES>", REVIEW_TABLES).replace("<TABLES>", TABLES))  # fmt: skip


def grafana_role() -> None:
    """The triage tables are created by the agent's own role first, so `apex` owns them (not the superuser)."""
    schema = (ROOT / "agent" / "src" / "apex_agent" / "ingest" / "schema.sql").read_text(encoding="utf-8")
    # S20: the knowledge layer's tables too (commodity_index, parts…): on a FRESH system they must exist before the grants
    schema += "\n" + (ROOT / "agent" / "src" / "apex_agent" / "knowledge" / "schema.sql").read_text(encoding="utf-8")
    audit = (ROOT / "agent" / "ops" / "postgres" / "audit.sql").read_text(encoding="utf-8")
    for user, sql in (
        ("apex", schema),
        ("postgres", audit),  # S9: owned by the superuser, not by the app's role
        ("postgres", roles_sql()),
    ):
        psql = ["psql", "-q", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", "apex"]
        r = subprocess.run([*COMPOSE, "exec", "-T", "app-postgres", *psql],
                           input=sql, capture_output=True, text=True)  # password via stdin, never an argument  # fmt: skip
        if r.returncode != 0:
            raise SystemExit(f"postgres ({user}) failed: {r.stderr[-500:]}")
    print(f"postgres access: role grafana_ro can SELECT {TABLES} (nothing else)")
    print(f"postgres access: role review_ui can SELECT {REVIEW_TABLES}, UPDATE jobs(status, decision) (nothing else)")
    print("postgres access: role apex_audit can INSERT into audit_log (nothing else); audit_log is append-only and hash-chained")
    print(
        "postgres access: role whatif can SELECT estimates, extractions, commodity_index, whatif_log and INSERT whatif_log (nothing else)"
    )


# S7: the people. Who approves what is decided by OPA from these roles (apex/review), not by the app.
PEOPLE = {
    "arjun": ("Arjun", "estimator"),
    "ramesh": ("Ramesh", "senior_estimator"),
    "kavita": ("Kavita", "planner"),
    "meera": ("Meera", "auditor"),
    "vikram": ("Vikram", "admin"),
}
KEYCLOAK = "https://localhost:8445"


def keycloak_people() -> None:
    tls = ssl.create_default_context(cafile=str(ROOT / "data" / "generated" / "certs" / "ca.crt"))
    with httpx.Client(base_url=KEYCLOAK, verify=tls, timeout=30) as kc:
        tok = kc.post("/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": env("KEYCLOAK_ADMIN_PASSWORD")})  # fmt: skip
        tok.raise_for_status()
        kc.headers["Authorization"] = f"Bearer {tok.json()['access_token']}"
        api = "/admin/realms/apex"
        client = kc.get(f"{api}/clients", params={"clientId": "review-ui"}).raise_for_status().json()[0]
        client.update({"publicClient": False, "clientAuthenticatorType": "client-secret", "secret": env("REVIEW_UI_CLIENT_SECRET"),
                       "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
                       "redirectUris": ["https://localhost:8448/oauth2/callback"], "webOrigins": ["https://localhost:8448"]})  # fmt: skip
        client["attributes"] = {
            **client.get("attributes", {}),
            "pkce.code.challenge.method": "S256",
            "post.logout.redirect.uris": "https://localhost:8448/",
        }
        kc.put(f"{api}/clients/{client['id']}", json=client).raise_for_status()
        for username, (first, role) in PEOPLE.items():
            found = kc.get(f"{api}/users", params={"username": username, "exact": "true"}).raise_for_status().json()
            if not found:
                kc.post(f"{api}/users", json={"username": username, "email": f"{username}@apex.local", "firstName": first,
                                              "lastName": "Apex", "enabled": True, "emailVerified": True}).raise_for_status()  # fmt: skip
                found = kc.get(f"{api}/users", params={"username": username, "exact": "true"}).raise_for_status().json()
            uid = found[0]["id"]
            kc.put(
                f"{api}/users/{uid}/reset-password",
                json={"type": "password", "value": env("KEYCLOAK_STAFF_PASSWORD"), "temporary": False},
            ).raise_for_status()
            have = {r["name"] for r in kc.get(f"{api}/users/{uid}/role-mappings/realm").raise_for_status().json()}
            if role not in have:
                role_rep = kc.get(f"{api}/roles/{role}").raise_for_status().json()
                kc.post(f"{api}/users/{uid}/role-mappings/realm", json=[role_rep]).raise_for_status()
    print(f"keycloak access: client review-ui confidential; users {', '.join(f'{u} ({r})' for u, (_, r) in PEOPLE.items())}")


if __name__ == "__main__":
    main()
