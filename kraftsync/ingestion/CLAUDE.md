# ingestion — local rules
Read `docs/02-ingestion/README.md` first.
- **Reserved package (stretch, not built).** The service loads `PLAN_PATH` (default `data/demo/plan.json`) directly.
- Output is always a `ProjectPlan` (kraftsync/contracts). Nothing else leaves this package.
- Structured sources (CSV, Excel, MSPDI, XER) are parsed deterministically. PDFs may use an LLM, but only to produce a *draft* plan that is validated and frozen to JSON — never at live runtime.
- `validate(plan)` must catch: dangling ids, cycles, finish < start, duplicate ids. Return readable problems, don't just raise.
- Round-trip test: `data/demo/*.csv` → `ProjectPlan` == `data/demo/plan.json`.
- Do not import from graph/engine/app.
