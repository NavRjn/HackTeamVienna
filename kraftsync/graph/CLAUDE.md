# graph — local rules
Read `docs/04-graph/README.md` first.
- **Reserved package.** State and events currently live in `kraftsync/app/state.py` (`ProjectState`); the date logic is `kraftsync/engine/check.py`. If you take this over, move them here behind the same method names and keep `tests/test_api.py` green.
- 100% deterministic. No LLM calls, no network in this package.
- NetworkX DiGraph: nodes = Task ids, edges = Dependency (type, lag). Persons/locations are referenced by id.
- Baseline (`planned_*`) never changes after load. Actuals and forecasts change only via `apply()` which emits GraphEvents.
- Event store is append-only `data/events.jsonl`. State must be reproducible by replaying events — test this.
- Every Discrepancy must have a human-readable `description` naming the task(s) and the rule that fired, plus an `Impact`.
- Tests are the spec: the "Done when" list in the doc must exist as pytest cases using data/demo.
