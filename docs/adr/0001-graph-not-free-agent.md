# ADR 0001: A LangGraph state graph, not a free-roaming agent

- **Status:** accepted (S0, 2026-11-20)
- **Deciders:** FDE team; reviewed with Vikram (IT) and Meera (sponsor)

## Context
Quoting is a known business process: read, extract, check, cost, review, send. A free-roaming agent (an LLM choosing its next tool in a loop) is hard to test, can take unexpected actions, and its cost is unpredictable. Ramesh and Meera need every step to be visible and repeatable.

## Decision
Build the agent as a **LangGraph `StateGraph`** with fixed nodes and conditional edges (`not_rfq`, `clarify`, `escalate`, `no_bid`), a typed state, and the **Postgres checkpointer**. Human review is an `interrupt()`. The LLM works *inside* nodes (extract, triage, draft); it does not decide the control flow. The only ReAct-style agent is the read-only what-if assistant (S16).

## Consequences
+ Every run follows a known path that can be traced, tested and resumed after a crash.
+ Per-node timeouts, retries and budgets are easy to enforce.
− New behaviour needs a new node or edge (by design). The state must be designed so later phases add nodes without a rewrite.
