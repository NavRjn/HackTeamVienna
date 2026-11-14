# graph — local rules
Read `docs/04-graph/README.md` first.
- **The single point of control.** All project state, the CPM schedule, `apply`, `detect`, the event log (`EventStore`) and the "what may the LLM choose from" shortlist (`context_for`) live here. `app/` and `engine/` only call `ProjectGraph`; do not re-implement dates, state changes or event writing elsewhere. `ProjectGraph.record()` is the only event writer.
- 100% deterministic. No LLM calls, no network in this package.
- NetworkX DiGraph: nodes = Task ids, edges = Dependency (type, lag). Persons/locations are referenced by id.
- Baseline (`planned_*`) never changes after load. Actuals and forecasts change only via `apply()` which emits GraphEvents.
- Event store is append-only `data/live/events.jsonl` (`EventStore`) (live, gitignored); the baseline in `data/demo/` is never written. State must be reproducible by replaying events — test this.
- Every Discrepancy must have a human-readable `description` naming the task(s) and the rule that fired, plus an `Impact`.
- Tests are the spec: the "Done when" list in the doc must exist as pytest cases using data/demo.
