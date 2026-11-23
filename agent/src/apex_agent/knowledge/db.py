"""Connection to the agent's database (app-postgres, database `apex`), creating the given schema first."""

from pathlib import Path
from typing import Any

import psycopg

from apex_agent.settings import DbSettings, load_as

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(schema: Path = SCHEMA) -> psycopg.Connection[tuple[Any, ...]]:
    conn = psycopg.connect(load_as(DbSettings).url.get_secret_value(), autocommit=False)
    with conn.cursor() as cur:
        cur.execute(schema.read_text(encoding="utf-8"))  # idempotent: CREATE ... IF NOT EXISTS
    conn.commit()
    return conn
