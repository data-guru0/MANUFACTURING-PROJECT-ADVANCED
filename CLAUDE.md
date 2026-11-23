# CLAUDE.md — Project rules (read before every task)

## What this project is
A learning prototype, built **for real**. *Apex Precision Components*, a CNC job shop, quotes custom parts slowly by hand (see `build-prompts/IDEA.md`). We play the **Forward Deployed Engineering (FDE) team** hired to improve its legacy system with an agentic AI system.

- `build-prompts/01_LEGACY_SYSTEM_BUILD.md`: build the client's current system first (phases L0–L6). It is **deliberately weak** (weaknesses W1–W18). Do not fix those weaknesses there.
- `build-prompts/02_AGENTIC_SOLUTION_BUILD.md`: the FDE engagement (phases S0–S20). It **integrates with** the legacy system rather than replacing it, and each phase fixes named weaknesses.
- Theory and build lessons live in `DEMO.md` (LEARNING.md was merged into it in S20). Add a short note to the phase's "📚 Theory" block when a phase reveals something students should learn.
- **`DEMO.md`: the ONE file for setup and teaching (theory, UI walkthroughs, the few unavoidable commands). It is a required deliverable of every phase** (see "How to work").
- `build-prompts/`: the specifications above (IDEA, 01, 02, KICKOFF_PROMPT). `docs/`: the engagement's deliverables and `PROGRESS.md`.

## Hard rules
1. **Nothing is mocked, faked or stubbed.** No mock servers or LLMs, no `unittest.mock`, no hardcoded fake responses, no demo shortcuts, no `TODO` paths left at the end of a phase. Tests run against real Docker services and the real Groq API. Synthetic *business data* loaded through real APIs is allowed and must be tagged.
2. **Open source only.** The single exception is LLM inference via Groq (`GROQ_API_KEY` in `.env`), using open-weight models.
3. **Agent stack:** LangGraph (with the Postgres checkpointer); MCP tool servers; **LiteLLM Proxy as the only path to any LLM**; **OPA for every permission decision** (fail closed); Envoy for ingress and egress; Keycloak; Langfuse.
4. **The LLM never calculates** prices, dates, CO₂ or terms; deterministic code does. Every number has a provenance. **Nothing is sent to customers or suppliers without approval.**
5. **Secrets** live only in `.env`, which is git-ignored. Never print, log or commit them. Never read `.env` values into the conversation.
6. **Prototype, not product.** Use the simplest *real* implementation that works end to end. Mark shortcuts with a `# ponytail:` comment naming the limit and the upgrade path. Eval targets are measured and reported honestly, not polished endlessly.
7. **Local only.** Everything runs on the developer's machine with Docker Compose. There is no cloud, no remote server, no GitHub Actions or other hosted CI, and no deployment pipeline. The quality gate is `make check` plus pre-commit hooks.
8. **The legacy system must run on its own** (`make demo-legacy`) at any time.

## How to work
- Build **one phase at a time**, in order. Before coding, list the files you plan and the key decisions, and wait for the human's OK.
- At the end of a phase:
  1. Run its "Done when" checks for real.
  2. Append to `docs/PROGRESS.md`: what was built, how to run it, how it was verified, its limits, and the W# fixed.
  3. **Fill in that phase's section of `DEMO.md`** using the section template in that file. Cover the story, the concept, every command **in order** with what it does and what you should see, what to show in which UI, one "break it" demo, the students' vibe-coding prompt with what to verify in the AI's code, the class questions and the checkpoint. **Only list commands you actually ran in this phase**, and summarize their real output. Also update the cheat sheet, the setup section and troubleshooting in `DEMO.md`.
  4. **Stop and wait for "next phase".**
- A phase is **not done** until its `DEMO.md` section lets an instructor reproduce and teach it from a fresh `make demo-reset`.
- Python 3.12 + uv; ruff; mypy on `src/`; Docker Compose (`docker-compose.legacy.yml` and `docker-compose.agent.yml`, layered).
- Platform: Windows host with Docker Desktop. Keep scripts cross-platform (Python over shell where possible).
- Use current versions; check docs when unsure (Groq model IDs, LiteLLM OSS vs enterprise features, Odoo 17 APIs). Never invent an API.
