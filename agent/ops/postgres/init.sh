#!/bin/bash
# Runs once, on an empty data volume (docker-entrypoint-initdb.d). One role + database per service;
# passwords come from the environment (.env), never from this file.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE apex     LOGIN PASSWORD '${APEX_DB_PASSWORD}';
CREATE ROLE litellm  LOGIN PASSWORD '${LITELLM_DB_PASSWORD}';
CREATE ROLE langfuse LOGIN PASSWORD '${LANGFUSE_DB_PASSWORD}';
CREATE ROLE keycloak LOGIN PASSWORD '${KEYCLOAK_DB_PASSWORD}';
CREATE DATABASE apex     OWNER apex;
CREATE DATABASE litellm  OWNER litellm;
CREATE DATABASE langfuse OWNER langfuse;
CREATE DATABASE keycloak OWNER keycloak;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname apex -c "CREATE EXTENSION IF NOT EXISTS vector;"
