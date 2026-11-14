# 04 · Dependency graph

Goal: the deterministic heart. Holds the plan + live state, recomputes dates, and detects discrepancies with explainable impact.

## Library decision

**Use NetworkX `DiGraph` as the graph core + a thin CPM layer we own (~100 lines) + SQLite event store.**

Why:
- NetworkX is the standard, mature Python graph library: topological sort, DAG checks, ancestors/descendants, longest path, subgraphs, and export to Cytoscape JSON for the UI (`nx.cytoscape_data`). In-memory, zero infra, fast for thousands of tasks.
- CPM (forward/backward pass, total float, critical path) with FS/SS/FF/SF + lag is small enough to own and test; owning it means we control "actuals override plan" logic, which generic CPM libs don't do. `scripts/generate_demo_plan.py` already contains a forward pass to start from.
- Optional time-saver to evaluate in ≤15 min: `trueppm-scheduler` (pure-Python CPM with FS/SS/FF/SF). If it doesn't support actuals/forecasts cleanly, don't fight it.

Considered and rejected for the MVP:
- **Graphiti (Zep) / temporal knowledge graphs** — the LLM builds and mutates the graph. That breaks our determinism rule and makes discrepancies unexplainable. Good idea for a later "project memory" layer, not for the schedule.
- **Neo4j / FalkorDB** — nice browser visualisation, but extra infra and a second state store. Revisit only if the UI person wants Neo4j Bloom for the demo.
- **MPXJ** — not a graph library, but the right *ingestion* tool for MS Project/P6 (see 02).

## Ontology

```
Task (node)
  id, name, kind {work | inspection | delivery | milestone}, trade, location_id,
  responsible_person_id, duration_days,
  planned_start/finish (baseline, never changes after load),
  status {not_started | in_progress | blocked | done | cancelled},
  actual_start/finish, forecast_finish, percent_complete,
  hold_point (bool), boq_ref, drawing_refs

Dependency (edge)  predecessor → successor
  type {FS | SS | FF | SF}, lag_days, reason ("same crew", "screed drying", "hold point")

Person, Location: reference entities by id (Location is a tree: building > floor > unit > room)
GraphEvent: append-only log, the only way state changes
Discrepancy (+ Impact): derived after each applied update
```

Three date layers per task — keep them separate, never overwrite baseline:
1. **Baseline**: `planned_*` from the plan.
2. **Actual**: from updates (`started` → actual_start, `completed` → actual_finish).
3. **Forecast**: CPM recomputed from today + actuals + reported forecasts.

## CPM rules (forecast)

- Forward pass in topological order. For a task:
  - if `done`: start/finish = actuals.
  - if `in_progress`: start = actual_start; finish = max(reported forecast_finish, actual_start + duration, today).
  - else: start = max(today, constraints from predecessors by dependency type + lag).
- Backward pass from the project's last milestone → total float. Critical path = float ≤ 0.
- Calendar days only (working calendars are a stretch).

## Discrepancy detection (all deterministic)

Run after every applied `TaskUpdate`, plus `silent_overdue` on a timer.

| Type | Rule | Severity |
|---|---|---|
| `delay` | forecast_finish > planned_finish (or > previous forecast) | warning; critical if critical path / project finish moves |
| `out_of_sequence` | task `in_progress`/`done` while an FS predecessor is not `done` (SS: predecessor not started) | warning |
| `hold_point_violation` | out_of_sequence where the unfinished predecessor has `hold_point=True` | critical |
| `blocked` | update kind `blocked` | warning; critical if on critical path |
| `scope_change` | update kind `modified` | warning (needs site-manager decision; billable) |
| `unknown_task` | engine returned `task_id=None` with kind ≠ irrelevant/question | info |
| `conflicting_reports` | two updates for the same task contradict within 24 h (e.g. completed vs delayed) | warning |
| `silent_overdue` | planned/forecast finish < today, status ≠ done, no update for that task in 24 h | info → warning after 48 h |

Every discrepancy carries an `Impact`: delay days, downstream affected task ids (topological order), critical-path flag, project finish before/after. The policy uses `affected_task_ids` to decide who gets told.

## Event store

SQLite table `events(id, ts, kind, actor, task_id, source_message_id, before_json, after_json, payload_json)`. Append-only. `ProjectGraph` state = plan + replay(events). Snapshot to JSON for the UI.

## Interface

See `01-architecture` → Public interfaces (`ProjectGraph`).

## Done when (tests)

- Loading `data/demo/plan.json` reproduces its planned dates via CPM.
- Demo message 2 (plumbing delay to 13 Nov) → handover milestone moves and `T6-INSP`, `T6-BOARD`, … are in `affected_task_ids`.
- Demo message 5 (T6-BOARD started) → `out_of_sequence` + `hold_point_violation`.
- Replaying the full event log reproduces identical state.

## Implementation (status)

| File | What |
|---|---|
| `kraftsync/graph/store.py` | `EventStore`: SQLite, append-only (`":memory:"` in tests), one locked connection (thread-safe reads + writes) |
| `kraftsync/graph/cpm.py` | `Net.compile()` (topological order + adjacency lists, built once) and the forward/backward pass |
| `kraftsync/graph/project_graph.py` | `ProjectGraph`: `from_plan`, `apply`, `detect`, `check_overdue`, `schedule`, `critical_path`, `downstream`, `upstream`, `candidates_for`, `snapshot` |
| `kraftsync/graph/feed.py` | `read_reports` / `feed`: apply already-interpreted JSONL lines (history, gold labels, manual updates). Not the engine. |
| `data/demo/history.jsonl` | what happened 2-9 Nov, as messages. `plan.json` itself is the untouched baseline (data_date 2 Nov, nothing started). |
| `tests/test_graph.py` | the "Done when" cases, the whole demo chat against its gold `expected_discrepancies`, CPM edge cases |

**Demo state = baseline plan + history.jsonl + messages.jsonl, all through `apply()`.** Progress is never baked into `plan.json`. After the history, CPM reproduces every planned date (tested).

Rules implemented: `delay`, `out_of_sequence`, `hold_point_violation`, `blocked`, `scope_change`, `unknown_task`, `silent_overdue` (`check_overdue()`, call it on a timer; at most once per task per day; info, warning after 2 days). Not yet: `conflicting_reports`.

Decisions (deterministic, tested):
- Below `CONFIDENCE_MIN` nothing is applied **and nothing is raised** (this includes scope changes). `unknown_task` is still raised so the policy can ask the sender.
- `progress` on a task that has not started, with no `%`, is a confirmation ("screed is fine for Thursday"), not a start. Only `started` (or a `progress` that has a `%`) sets `actual_start`.
- out_of_sequence / hold_point are checked only when the status changes, so later progress reports do not raise the same alert again.
- A task started out of sequence still cannot finish before its unfinished FS/FF predecessors. Behind an **open hold point** the work counts as redone after the inspection (finish >= inspection + duration). So starting early never makes the forecast look better.
- A `done` task with only `actual_finish` gets start = finish - duration + 1.

Efficiency: the structure is compiled once; the schedule is cached until the state or `today` changes; one `apply()` + `detect()` = one CPM pass, O(V+E) over plain lists. `snapshot()` is cached and only re-serialises tasks that changed. Measured on a synthetic 2,601 tasks / 3,595 dependencies: 100 x (apply + detect + snapshot) took 301 ms each before, 61 ms each now. The demo plan (26 tasks) takes well under 1 ms.
