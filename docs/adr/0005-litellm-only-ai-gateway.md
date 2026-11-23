# ADR 0005: LiteLLM is the only AI gateway

- **Status:** accepted (S0, 2026-11-20)
- **Deciders:** FDE team; reviewed with Vikram (IT) and Meera (sponsor)

## Context
Several parts of the system call LLMs. Without one gateway, keys spread across services, costs are invisible, and swapping a model means editing code.

## Decision
All LLM calls go to the **LiteLLM proxy**, the **only** holder of the Groq key. Agents use **model aliases** (`quote-reasoner`, `quote-triage`, …) from `config/models.yaml` and **one virtual key per agent** with its allowed aliases, rate limits and budget. Fallback chains end at "escalate to a human", never at an unvetted model. Langfuse records every call with its cost. Only OSS LiteLLM features are used; anything enterprise-only is implemented as a custom callback.

## Consequences
+ Model swaps by config; budgets and kill switch in one place; full cost visibility.
+ Switching to a self-hosted vLLM/Ollama later is a config change.
− A single component on every AI path (it must be healthy; when it isn't, the system drops to manual mode).
