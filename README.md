# KraftSync

The site chat becomes a live construction schedule. Every "done", "late" or "we already started" message updates a dependency graph; everyone whose work moves hears about it in minutes.

Building Better Worlds Hackathon, Vienna 2026 · Track 01 · Talona — *Context for the Physical World*.

## Quickstart
```bash
cp .env.example .env
uv sync
uv run python scripts/generate_demo_plan.py
uv run pytest -q
```

## Docs
| Folder | Content |
|---|---|
| `docs/00-overview` | Problem, product, demo story |
| `docs/01-architecture` | Data flow, contracts, determinism rule |
| `docs/02-ingestion` | Plans → `ProjectPlan` |
| `docs/03-integrations` | Telegram / WhatsApp / email |
| `docs/04-graph` | Dependency graph, CPM, discrepancy rules |
| `docs/05-engine` | Message interpretation + live loop + eval |
| `docs/06-resolution-policy` | Who gets told what |
| `docs/07-deployment-demo` | UI, hosting, demo, submission |

## Results
| Metric | Value |
|---|---|
| Task mapping accuracy | TBD |
| Update-kind accuracy | TBD |
| Wrong-and-confident rate | TBD |
| Message → notification latency | TBD |

## Assumptions
- Demo project and chat messages are synthetic (`data/demo/`), generated to be schedule-consistent.
- Calendar days, no working calendars.
