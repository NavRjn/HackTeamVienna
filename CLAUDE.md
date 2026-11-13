# CLAUDE.md — KraftSync

Hackathon project · Building Better Worlds, Vienna · Track 01 / Talona "Context for the Physical World".
**Code freeze: Sat 14 Nov 2026, 15:00.** Optimise for a working end-to-end demo. Cut scope before cutting quality.

## What KraftSync is

A construction schedule as a **live dependency graph**. Crews keep texting the site chat like they do today. KraftSync reads each message, maps it to a task in the graph, updates the graph, detects what that breaks downstream (delay, out-of-sequence work, hold-point violation, scope change), and tells **the people whose work is affected** — not just the sender.

Read before doing anything non-trivial:
- `docs/00-overview/` — problem, product, demo story
- `docs/01-architecture/` — data flow, contracts, ontology, the determinism rule
- the doc folder for the module you are touching (`docs/02-…` to `docs/07-…`)

## The one rule: AI interprets, the graph decides

- The **only** non-deterministic step is the engine's interpretation: `InboundMessage → TaskUpdate` (+ drafting outbound text).
- Everything else is deterministic, unit-tested Python: graph state, CPM dates, discrepancy detection, resolution policy, routing.
- A `TaskUpdate` below the confidence threshold is **never applied**; the engine asks the sender instead.
- Never let an LLM invent task ids, person ids, dates or dependencies. It may only choose from candidates we give it.

## Layout

```
kraftsync/
  contracts/      Pydantic models shared by everyone (models.py)   ← the contract
  ingestion/      CSV / PDF / MS Project / P6 → ProjectPlan
  integrations/   Telegram / WhatsApp / email ↔ InboundMessage / Action
  graph/          dependency graph, CPM, event store, discrepancy detection
  engine/         message interpretation (LLM), orchestration loop
  policy/         resolution policy: Discrepancy → Actions (deterministic, YAML-driven)
  app/            API + visualisation
data/demo/        generated demo project + scripted demo messages (with gold labels)
scripts/          generators and one-off tools
tests/            pytest; one test file per module at least
docs/NN-*/        one folder per part
```

Each package folder has its own `CLAUDE.md` with local rules. Stay inside the package you were asked to work on; if you need something from another package, use its public interface or ask for a contract change.

## Contracts (`kraftsync/contracts/models.py`)

- All modules communicate only via these models. No module imports another module's internals.
- Additive changes (new optional field) are OK. Renames / removals / type changes need the whole team — flag it, don't do it silently.
- Flow: `ProjectPlan` (ingestion → graph) · `InboundMessage` (integrations → engine) · `TaskUpdate` (engine → graph) · `GraphEvent` (graph log) · `Discrepancy` (graph → policy) · `Action` (policy → integrations).

## Commands

```bash
uv sync                                         # install
uv run python scripts/generate_demo_plan.py     # regenerate data/demo/*
uv run pytest -q                                # all tests
uv run python -m kraftsync.engine.replay data/demo/messages.jsonl   # replay demo chat through the whole pipeline
uv run python -m kraftsync.engine.eval data/demo/messages.jsonl     # interpretation accuracy vs gold labels
uv run uvicorn kraftsync.app.api:app --reload   # API + UI
```
Commands that don't exist yet are the target interface — create them with exactly these names. Keep this list true.

## Conventions

- Python 3.11+, `uv`, Pydantic v2, type hints everywhere, small modules.
- Dates are calendar days (`date`), timestamps are timezone-aware (`Europe/Vienna`).
- Config only via env vars (`.env`, see `.env.example`). Never commit secrets, tokens, real phone numbers or `.env`.
- The event log is append-only. Never UPDATE or DELETE an event. Current state = replay of events.
- Use `data/demo/` for everything until the demo works. Real ingestion output is a bonus, not a dependency.
- Every module must run without network and without an LLM key in tests (use fakes).
- Before finishing a task: run `uv run pytest -q` and the relevant command above. Update the module's doc if behaviour changed.
- Commit small, commit often, merge to `main` often. `main` must always run the demo replay.
