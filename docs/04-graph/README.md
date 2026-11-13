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
