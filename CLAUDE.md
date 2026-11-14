# CLAUDE.md — KraftSync

Hackathon project · Building Better Worlds, Vienna · Track 01 / Talona "Context for the Physical World".
**Code freeze: Sat 14 Nov 2026, 15:00.** Optimise for a working end-to-end demo. Cut scope before cutting quality.

## What KraftSync is

A construction schedule as a **live dependency graph**. Crews keep texting the site chat like they do today. KraftSync reads each message, maps it to a task in the graph, updates the graph, detects what that breaks downstream (delay, out-of-sequence work, hold-point violation, scope change), and tells **the people whose work is affected** — not just the sender.

**Architecture: Telegram → n8n Cloud (LLM reads the message) → local FastAPI service (graph checks it) → n8n → Telegram.** n8n owns chat I/O and the one AI step; the local service owns state, dates, detection and routing, and calls no LLM. The two talk over an https tunnel with a shared token.

Read before doing anything non-trivial:
- `README.md` — the picture (diagrams), the flow, ownership
- `docs/00-overview/` — problem, product, demo story
- `docs/01-architecture/` — data flow, contracts, ontology, the determinism rule
- `docs/api.md` — the HTTP contract between n8n and the service (frozen; change only with everyone's OK)
- `docs/n8n-workflow.md` — what the n8n workflow does, node by node
- the doc folder for the module you are touching (`docs/02-…` to `docs/07-…`)

## The one rule: AI interprets, the graph decides

- The **only** non-deterministic step is the interpretation in n8n's LLM node: chat message → `TaskUpdate` claims (one claim about one task). There is no LLM call in this repo's Python.
- Everything else is deterministic, unit-tested Python: sender lookup, candidate shortlist, graph state, forecast dates, discrepancy detection, resolution policy, routing.
- A claim below `CONFIDENCE_MIN` (or without a task) is **never applied**; the service asks the sender instead.
- Never let an LLM invent task ids, person ids, dates or dependencies. It may only choose from the `tasks` / `related_tasks` that `POST /context` returns; the service rejects unknown ids.
- Don't make the LLM send a graph fragment or decide "is this a conflict" — it sends claims, the graph decides.

## Layout

```
kraftsync/
  contracts/      Pydantic domain models shared by everyone (models.py)   ← the contract
  app/            local FastAPI service: api.py (routes), api_models.py (HTTP envelopes), settings.py,
                  state.py (service glue around the graph: plan + Telegram ids, history seeding, action outbox), serve.py (the only entry point)
  engine/         resolve.py (who gets told) — deterministic, no LLM; reads the graph, computes no dates itself
  policy/         policy.yaml (message templates de/en + notify depth; read by engine/resolve.py)
  graph/          THE single point of control: ProjectGraph (state, CPM schedule, apply, detect, context shortlist), EventStore (events.jsonl), feed
  ingestion/      CSV / Excel / MS Project XML / P6 XER → ProjectPlan, PDF → reviewed draft (plan time only, see docs/02-ingestion)
  integrations/   reserved: channels are handled by n8n; only a Python WhatsApp-export parser would go here
n8n/              (to add) exported workflow JSON + prompt
data/demo/        generated demo project + scripted demo messages (with gold labels)
scripts/          generators and one-off tools
tests/            pytest; test_api.py replays the gold messages through the API
docs/             NN-* one folder per part; api.md (HTTP contract); n8n-workflow.md
```

Each package folder has its own `CLAUDE.md` with local rules. Stay inside the package you were asked to work on; if you need something from another package, use its public interface or ask for a contract change.

## Contracts

- `kraftsync/contracts/models.py` = domain models; `kraftsync/app/api_models.py` = HTTP envelopes (`UpdateRequest`, `UpdateResponse`, …). Modules communicate only via these. No module imports another module's internals.
- Additive changes (new optional field) are OK. Renames / removals / type changes need the whole team — flag it, don't do it silently. The n8n output schema is derived from `TaskUpdate`.
- Flow: `ProjectPlan` (file → state) · `TaskUpdate` (n8n → service, via `UpdateRequest`) · `GraphEvent` (log) · `Discrepancy` (check → resolve) · `Action` (resolve → n8n). `InboundMessage` is defined but unused (n8n normalises messages).

## Commands

```bash
uv sync                                         # install
uv run pytest -q                                # all tests (includes the demo replay of the 10 gold messages)
uv run python -m kraftsync.app.serve            # local API for n8n (LAN; token + random port generated into .env on first run)
uv run python -m kraftsync.app.serve --reload   # same, restarts on code/yaml changes
uv run python -m kraftsync.app.serve --init     # only create token/port in .env and print them
uv run python scripts/tunnel.py                # https tunnel for n8n Cloud: starts cloudflared, checks the token lock, prints KRAFTSYNC_BASE_URL (quick tunnel URL changes on restart)
uv run python scripts/generate_demo_plan.py     # regenerate data/demo/* (WIPES the telegram ids typed into persons.csv)
uv run python scripts/generate_demo_sources.py  # regenerate data/demo/sources/ (CSV + PDF) from plan.json
```
Keep this list true: if a command doesn't exist, it isn't here. (LLM accuracy against gold labels is measured in n8n with a Manual Trigger, see `docs/05-engine`.)

## Conventions

- Python 3.11+, `uv`, Pydantic v2, type hints everywhere, small modules.
- Dates are calendar days (`date`), timestamps are timezone-aware (local time; the machine runs in Vienna).
- Config only via env vars (`.env`, see `.env.example`). Never commit secrets, tokens, real phone numbers or `.env`. Never log tokens or message text.
- The local service is started only through `kraftsync.app.serve` (token check, no docs, no access log).
- Static baseline = `data/demo/` (committed, never written by the service). Live data = `data/live/` (gitignored: `events.jsonl`, `graph.json`, `kraftsync.log`).
- The event log (`data/live/events.jsonl`) is append-only. Never UPDATE or DELETE an event. Current state = baseline + replay of events; `graph.json` is only a derived copy.
- Use `data/demo/` for everything until the demo works. Real ingestion output is a bonus, not a dependency.
- Every module must run without network and without an LLM key in tests (the gold labels are used as claims; the n8n webhook is faked).
- Before finishing a task: run `uv run pytest -q`. Update the module's doc (and `docs/api.md` for any route change) in the same commit.
- Commit small, commit often, merge to `main` often. `main` must always pass `uv run pytest -q` (it contains the demo replay).
