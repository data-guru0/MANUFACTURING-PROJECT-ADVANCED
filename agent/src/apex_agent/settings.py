"""Agent configuration from the environment. Fails fast: a missing value stops the process at start-up
with the names of what is missing, instead of failing later in the middle of a job.

One small settings class per concern, so each process asks only for what it uses:
LLM (APEX_*), database (APEX_DB_*), Odoo (APEX_ODOO_*), file share (APEX_SHARE_*), MCP (APEX_MCP_*)."""

import ssl

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APEX_", extra="ignore")

    agent: str  # the registered agent name (agent/config/agents.yaml) this process acts as
    litellm_url: str  # the ONLY LLM endpoint (ADR 0005), e.g. http://litellm:4000
    llm_key: SecretStr  # this agent's LiteLLM virtual key
    opa_url: str  # e.g. http://opa:8181
    ca_file: str | None = None  # trust this CA for https endpoints (the lab CA on the host)
    llm_timeout_s: float = 90.0

    def tls(self) -> ssl.SSLContext | bool:
        """httpx `verify=`: the lab CA if configured, else the system trust store."""
        return ssl.create_default_context(cafile=self.ca_file) if self.ca_file else True


class DbSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APEX_DB_", extra="ignore")
    url: SecretStr  # postgresql://apex:...@app-postgres:5432/apex


class OdooSettings(BaseSettings):
    """One scoped Odoo account per process (erp_reader or erp_integration), never the shared `office`."""

    model_config = SettingsConfigDict(env_prefix="APEX_ODOO_", extra="ignore")
    url: str
    db: str = "apex"
    login: str
    password: SecretStr


class ShareSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APEX_SHARE_", extra="ignore")
    server: str  # fileserver
    port: int = 445
    name: str = "Quotes"
    user: str  # agent-ro: read-only
    password: SecretStr


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APEX_MCP_", extra="ignore")
    opa_url: str
    port: int = 8000


class MailSettings(BaseSettings):
    """The agent's own IMAP login to rfq@ (Dovecot master user `agent`), never the shared rfq@ password."""

    model_config = SettingsConfigDict(env_prefix="APEX_MAIL_", extra="ignore")
    host: str  # mail.apex.local
    port: int = 993
    user: str  # rfq@apex.local*agent
    password: SecretStr
    mailbox: str = "INBOX"
    ca_file: str  # the lab CA that signed the mail server's certificate
    poll_s: float = 30.0  # polling fallback when IDLE is quiet or unsupported


class ReviewSettings(BaseSettings):
    """The review screen (S7): its own read-mostly Postgres role, OPA, and Keycloak (to verify each request's token)."""

    model_config = SettingsConfigDict(env_prefix="APEX_REVIEW_", extra="ignore")
    db_url: SecretStr  # postgresql://review_ui:...@app-postgres:5432/apex
    opa_url: str
    jwks_url: str  # Keycloak's published signing keys (inside the app network)
    issuer: str  # https://localhost:8445/realms/apex (what Keycloak writes into every token)
    client_id: str = "review-ui"
    approval_key: SecretStr  # S8: signs the approval token; only this screen and OPA hold it, never the worker
    whatif_url: str = "http://whatif:8000"  # S16: the what-if assistant's service (this screen has no LLM key)


class S3Settings(BaseSettings):
    """MinIO with the scoped `apex-agent` user (bucket `apex` only)."""

    model_config = SettingsConfigDict(env_prefix="APEX_S3_", extra="ignore")
    endpoint: str  # minio:9000
    access_key: str
    secret_key: SecretStr
    bucket: str = "apex"


class GateSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APEX_GATE_", extra="ignore")
    clamd_host: str  # clamav
    clamd_port: int = 3310
    lab_dns: str  # JSON file of the lab's DNS TXT records (DKIM keys, DMARC), written by agent/ops/lab_dns.py
    triage_key: SecretStr  # the triage agent's LiteLLM key (this worker acts as security_gate AND triage)


class DocSettings(BaseSettings):
    """S4: the worker reads drawings and emails as the registered agent `extractor` (its own LiteLLM key)."""

    model_config = SettingsConfigDict(env_prefix="APEX_DOC_", extra="ignore")
    extractor_key: SecretStr
    drafter_key: SecretStr  # S6: the drafter agent writes the reply around the computed numbers


class ConfigError(SystemExit):
    pass


def load_as[T: BaseSettings](cls: type[T]) -> T:
    prefix = str(cls.model_config.get("env_prefix", ""))
    try:
        return cls()
    except ValidationError as e:
        missing = sorted(prefix + str(err["loc"][0]).upper() for err in e.errors())
        raise ConfigError(f"configuration error, set: {', '.join(missing)}") from None


def load() -> Settings:
    return load_as(Settings)
