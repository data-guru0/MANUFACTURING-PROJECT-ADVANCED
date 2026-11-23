# ADR 0002: A Postgres job queue, not a separate broker

- **Status:** accepted (S0, 2026-11-20)
- **Deciders:** FDE team; reviewed with Vikram (IT) and Meera (sponsor)

## Context
Ingested RFQs must be processed reliably, at most dozens per day, on one machine that Vikram maintains. A message broker (RabbitMQ, Kafka) would be another service to run, secure and back up.

## Decision
Use a `jobs` table in app-postgres, claimed with `SELECT … FOR UPDATE SKIP LOCKED`, with `due_at` for scheduled work (follow-ups, S15). The same database holds the checkpoints, so a job and its graph state are backed up together.

## Consequences
+ One fewer service; transactional with the rest of the state; easy to inspect with SQL.
− Not built for very high throughput (`# ponytail:` fine for hundreds of jobs/day; move to a broker if Apex grows by orders of magnitude).
