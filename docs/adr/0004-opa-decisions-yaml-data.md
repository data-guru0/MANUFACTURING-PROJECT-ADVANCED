# ADR 0004: OPA makes every permission decision; YAML holds the policy data

- **Status:** accepted (S0, 2026-11-20)
- **Deciders:** FDE team; reviewed with Vikram (IT) and Meera (sponsor)

## Context
Approval limits, escalation reasons, which tool a node may use and whether an email may be sent will change as Apex learns. Hardcoding them in Python scatters them and makes them hard to audit.

## Decision
Every permission decision is an **OPA query** (`approval.rego`, `escalation.rego`, `actions.rego`, `tools.rego`). The numbers and lists (limits, margin floors, NDA rules, the kill switch) live in `config/policy.yaml`, which OPA loads as data. If OPA cannot be reached, the answer is **no** (fail closed). Policies have `opa test` tests in `make check`, and decision logs are referenced from the audit log.

## Consequences
+ One place to read and change the rules; testable; every decision explainable.
− Another language (Rego) for the team to learn; every decision costs a local HTTP call.
