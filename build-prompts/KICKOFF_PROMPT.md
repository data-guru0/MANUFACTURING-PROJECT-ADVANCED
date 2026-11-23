# Kickoff prompts for new Claude sessions

## First session (start the build)
```
You are joining a learning project as the engineer building it. Read these files fully before doing anything:
CLAUDE.md, IDEA.md, 01_LEGACY_SYSTEM_BUILD.md, 02_AGENTIC_SOLUTION_BUILD.md, LEARNING.md, DEMO.md.
After every phase you must also fill that phase's section in DEMO.md (the classroom runbook: every
command in order, what it does, what to show, what to break — only commands you actually ran).

Context: we are a Forward Deployed Engineering team. First we build the client's (Apex Precision
Components) legacy system exactly as specified in 01 — deliberately weak, weaknesses W1–W18 present —
then in later sessions we build the agentic solution from 02 that integrates with it and fixes them.

Rules that override everything: nothing mocked or faked (real services in Docker, real Groq API,
tests against live containers); open source only; prototype-simple but fully working; secrets only in
.env and never printed; one phase at a time.

Start with Phase L0. First summarize in 5 bullets what you understood about the project, then list the
files you will create for L0 and your key decisions, and wait for my OK before writing code.
```

## Every later session (continue)
```
Read CLAUDE.md, docs/PROGRESS.md and DEMO.md, then the section of 01_LEGACY_SYSTEM_BUILD.md or
02_AGENTIC_SOLUTION_BUILD.md for the next unfinished phase. Check that the previous phase's
"Done when" still passes (bring the stack up and run its tests). Then tell me which phase is next,
list the files you'll create and your key decisions, and wait for my OK.
```
