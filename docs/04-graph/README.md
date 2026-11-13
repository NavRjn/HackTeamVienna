# 04 · Dependency graph

**Status: v0 built, in two places.** State and events are in `kraftsync/app/state.py` (a placeholder for the `graph/` package, which is still empty); dates and detection are in `kraftsync/engine/check.py`. The owner of this part can move/replace them behind the same functions (see Interface). Contract models: `kraftsync/contracts/models.py`.

Goal: the deterministic heart. Holds the plan + live state, recomputes dates, and detects discrepancies with explainable impact.

## Library decision

**NetworkX `DiGraph` for structure + a small forward-pass we own (`check.forecast`) + an append-only JSONL event log.**

- NetworkX: topological order, DAG check, descendants, shortest-path levels (used to limit who is notified). In-memory, zero infra, fast for thousands of tasks.
- We own the date logic so "actuals override plan" and "out-of-sequence work is not progress" are ours to control.
- Event store is `data/events.jsonl` (one `GraphEvent` per line), not SQLite: no database, trivially replayable, easy to inspect. SQLite stays an option if the UI needs queries.

Considered and rejected for the MVP:
- **Graphiti (Zep) / temporal knowledge graphs**: the LLM builds and mutates the graph, which breaks the determinism rule and makes discrepancies unexplainable. Maybe later as a "project memory" layer.
- **Neo4j / FalkorDB / Supabase**: extra infra and a second state store; nobody on the team knows them.
- **MPXJ**: not a graph library; the right *ingestion* tool for MS Project/P6 (see `02-ingestion`).

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

Person: id, name, role (authority), job_title (craft), trade, company, contact fields, language
Location: tree building > floor > unit > room
GraphEvent: append-only log, the only way state changes
Discrepancy (+ Impact): derived after each applied claim
```

Three date layers per task, never mixed:
1. **Baseline**: `planned_*` from the plan, never overwritten.
2. **Actual**: `actual_start` / `actual_finish`, set by `started` / `completed` claims.
3. **Forecast**: computed on demand by `forecast()` from baseline + actuals + reported `forecast_finish`.

## Forecast rules (as implemented)

Forward pass in topological order, calendar days, FS and SS only (FF/SF are ignored):
- **done**: start/finish = actuals.
- **not started**: start = max(`planned_start`, FS: predecessor finish + lag + 1, SS: predecessor start + lag); finish = start + ⌈duration⌉ − 1 (milestones: finish = start); if a `forecast_finish` was reported, finish = max(computed, reported).
- **started, not done**: start = `actual_start`; finish = max(reported or planned finish, finish computed from predecessors that are **not done**). Out-of-sequence work therefore does not pull the schedule in: boarding started before the inspection still has to wait for the inspection.
- **project finish** = latest finish among tasks without successors.
- There is no backward pass, float or critical-path computation yet. "Critical path affected" means "the project finish moved".

Check: for the unchanged demo plan, `forecast()` reproduces every open task's planned dates and the project finish 2026-12-06 (`tests/test_check.py`).

## Discrepancy detection (all deterministic)

`check(before, after, deps, update, now)` compares the task map before and after one applied claim and raises only what is **newly** wrong.

| Type | Rule | Severity | Built |
|---|---|---|---|
| `out_of_sequence` | claim `started`/`completed` on a task that was `not_started`, while an FS predecessor is not done (SS: not started) | warning | yes |
| `hold_point_violation` | same, and an unfinished predecessor has `hold_point=True` (raised in addition to `out_of_sequence`) | critical | yes |
| `delay` | forecast finish after the claim is later than `planned_finish`, and it moved or a new date was reported; `Impact`: delay days, downstream task ids whose forecast changed (topological order), project finish before/after | critical if the project finish moves, else warning | yes |
| `scope_change` | claim kind `modified` | warning | yes |
| `blocked` | claim kind `blocked` | warning | yes |
| `conflicting_reports` | claim contradicts state or another report within 24 h (e.g. completed vs delayed; "started" on a done task) | warning | planned |
| `silent_overdue` | planned/forecast finish < today, not done, no update in 24 h | info → warning after 48 h | planned (needs a timer endpoint) |
| `unknown_task` | claim about work not in the graph | info | planned (today: `task_id` null → the sender is asked) |

`progress` claims never start a task (a readiness statement like "screed is fine for Thursday" is not a start). Every discrepancy has a human-readable `description` naming the task(s) and the rule.

## Events and state

- `GraphEvent` lines in `EVENTS_PATH` (default `data/events.jsonl`), append-only. Emitted today: `message_received`, `update_interpreted` (always, even when ignored or low confidence), `task_changed` (before/after of the changed fields), `discrepancy_raised`.
- State = `plan.json` + replay of events on startup. `POST /reset` clears the file and reloads the plan (demo only).
- Idempotency: a claim whose `message_id` already has an `update_interpreted` event is answered `duplicate` and not applied again.

## Interface

```python
# kraftsync/engine/check.py
forecast(tasks, deps) -> dict[str, tuple[date, date]]
project_finish(fc, tasks, deps) -> date | None
unfinished_predecessors(task_id, tasks, deps) -> list[Task]
check(before, after, deps, update, now) -> list[Discrepancy]

# kraftsync/app/state.py — ProjectState (to become graph/)
resolve_sender(sender_id: int) -> Person | None            # Telegram user id == Person.telegram_chat_id
context_for(person_id, today, text="") -> (tasks, related_tasks)   # each <= 15; unknown sender -> both empty
apply(update, actor, now) -> GraphEvent | None
record_discrepancy(d, now) -> GraphEvent
```
`context_for` keeps two lists apart on purpose. **`tasks`**: the sender's own tasks (assigned to them or to their trade) that are *active around today*, meaning in progress/blocked, overlapping the ±3-day window, overdue and still open, or finished inside the window; own tasks further out (e.g. 18 days away) are not listed. **`related_tasks`**: other people's tasks that may be what the message is about, ordered: tasks at locations whose name words all appear in the text ("Top 6", "Bad Top 6" include everything below that location), then direct predecessors/successors of the sender's tasks, then other tasks active around today; never a task from `tasks`. The sender's own tasks outside the window show up here only when the text names their location.

## Done when (tests)

- Loading `data/demo/plan.json` reproduces its planned dates via `forecast` → `tests/test_check.py`. ✅
- Demo message 02 (plumbing delay to 13 Nov) → handover moves 12-06 → 12-09 and `T6-INSP`, `T6-BOARD`, … are in `affected_task_ids` → `test_ripple_notifies_affected_trades_and_escalates`. ✅
- Demo message 05 (T6-BOARD started) → `out_of_sequence` + `hold_point_violation` → `test_hold_point_violation_escalates_immediately`. ✅
- Replaying the event log reproduces identical state → `test_state_survives_restart_and_reset`. ✅
- All 10 gold messages in order produce exactly the expected discrepancy types → `test_demo_replay_matches_gold`. ✅
- Not yet: `conflicting_reports`, `silent_overdue`, FF/SF dependencies, critical path/float.
