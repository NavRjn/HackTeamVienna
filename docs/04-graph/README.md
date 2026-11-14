# 04 · Dependency graph

**Status: built. `kraftsync/graph/` is the single point of control** for everything about the project: state, schedule, applying
updates, detecting conflicts and the event log. Nothing else in the repo computes dates, applies a claim or writes an event.
The service (`app/`) and the policy (`engine/resolve.py`) call into it. Contract models: `kraftsync/contracts/models.py`.

Goal: the deterministic heart. Holds the plan + live state, recomputes dates, and detects discrepancies with explainable impact.

## Library decision

**NetworkX `DiGraph` for structure + a small CPM layer we own (`graph/cpm.py`) + one append-only JSONL event log.**

- NetworkX: topological order, DAG check, descendants, shortest-path levels (limit who is notified). In-memory, zero infra.
- We own the date logic so "actuals override plan" and "out-of-sequence work is not progress" are ours to control.
- The event log is `data/live/events.jsonl` (one `GraphEvent` per line; the live, gitignored counterpart of the committed baseline in
  `data/demo/`): no database, trivially replayable, easy to inspect. Graph changes, discrepancies and the notification outbox are all
  events in this one log.

Considered and rejected for the MVP: Graphiti / temporal knowledge graphs (the LLM would mutate the graph, which breaks determinism),
Neo4j / FalkorDB / Supabase (extra infra, a second state store), MPXJ (an ingestion tool, see `02-ingestion`), SQLite (a second format next to the JSONL log).

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

Three date layers per task, never mixed: **baseline** (`planned_*`, never overwritten), **actual** (`actual_start/finish`, set by
`started` / `completed`), **forecast** (CPM from today + actuals + reported `forecast_finish`).

## CPM rules (forecast)

Forward + backward pass in `graph/cpm.py`, calendar days, FS/SS/FF/SF with lag, total float, critical path = float <= 0.
- **done**: actuals. A `done` task is final: later `progress` / `started` / `delayed` / `blocked` chatter about it is logged but never reopens it.
- **in progress / blocked**: start = `actual_start`; finish = max(start + duration, today + 1, reported `forecast_finish`, finish of unfinished predecessors).
  Behind an **open hold point** the work counts as redone after the inspection, so starting early never improves the forecast.
- **not started**: start = max(today, `planned_start`, predecessor constraints by type + lag, reported `forecast_finish` minus duration).
- Project finish = latest finish. The schedule is cached until the state or `today` changes: one `apply()` + `detect()` = one pass.

## Discrepancy detection (all deterministic, `ProjectGraph.detect`)

| Type | Rule | Severity |
|---|---|---|
| `delay` | the task's finish moved later and is later than `planned_finish`; `Impact`: delay days, tasks whose finish moved, project finish before/after | critical if the task was critical or the handover moves, else warning |
| `out_of_sequence` | status changes to in progress/done while a predecessor is open (`open_predecessors`) | warning |
| `hold_point_violation` | same, and an open predecessor is a hold point | critical |
| `blocked` | claim kind `blocked` | critical on the critical path, else warning |
| `scope_change` | claim kind `modified` | warning |
| `unknown_task` | claim about work with `task_id = null` (not irrelevant / question) | info |
| `silent_overdue` | `check_overdue()` (call on a timer): due before today, not done, no report for ~24 h; once per task per day | info, warning after 2 days |
| `conflicting_reports` | not built | |

Below `CONFIDENCE_MIN` nothing is applied and nothing is raised (except `unknown_task`, so the sender is asked). Every discrepancy has a
description naming the task and the rule, and an `Impact`.

## Events and state

- Event kinds in `events.jsonl`: `plan_loaded`, `message_received`, `update_interpreted` (always, even when ignored or low confidence),
  `task_changed` (before/after of the changed fields + schedule impact), `discrepancy_raised`, `action_drafted`, `action_sent`.
- **One writer:** `ProjectGraph.record()` (via `EventStore.append`). State = baseline (`data/demo/plan.json`, never written) + replay of the log.
  The outbox (`Action` + status) is rebuilt from the same log by `ProjectState`. `data/live/graph.json` is the derived `GET /state` body.
- **Demo story = baseline + history through `apply()`**: on a fresh log `ProjectState` feeds `data/demo/history.jsonl` (what happened 2–9 Nov)
  before anything else; `POST /reset` clears the log and seeds it again. Progress is never baked into `plan.json`.
- Idempotency: a claim whose `message_id` already has an `update_interpreted` event is answered `duplicate` and not applied again.

## Interface

```python
# kraftsync/graph  (public: EventStore, ProjectGraph, Report, feed, read_reports)
ProjectGraph.from_plan(plan, store, today=None, confidence_min=None)   # new log -> plan_loaded; existing log -> replay
  .apply(update, actor) -> [GraphEvent]            # logs update_interpreted (+ task_changed); low confidence changes nothing
  .log_interpretation(update, actor, applied)      # log a claim without applying it (ignored / asked)
  .detect(events) -> [Discrepancy]                 # logs discrepancy_raised;  .check_overdue() for silent_overdue
  .schedule() -> {task_id: (start, finish)};  .project_finish();  .planned_finish();  .critical_path()
  .downstream(id) / .upstream(id) / .reach(id, levels);  .final_task();  .open_predecessors(id);  .has_message(message_id)
  .context_for(person_id, today, text) -> (tasks, related_tasks)   # what the LLM may choose from, <= 15 each
  .snapshot() -> dict                              # the one serialisation of the state (GET /state, graph_view)
  .record(kind, actor, ...) -> GraphEvent          # the one event writer;  .today (settable);  .discrepancies
```

`context_for` keeps two lists apart on purpose. **`tasks`**: the sender's own tasks (assigned to them or to their trade) that are *active
around today*: in progress/blocked, overlapping the ±3-day window, overdue and still open, or finished inside the window; own tasks further
out are not listed. **`related_tasks`**: other people's tasks that may be what the message is about, ordered: tasks at locations whose name
words all appear in the text ("Top 6", "Bad Top 6" include everything below that location), then direct predecessors/successors of the
sender's tasks, then other tasks active around today; never a task from `tasks`. The sender's own tasks outside the window show up here
only when the text names their location.

Callers: `app/api.py` (`process_claim`: `apply` → `detect` → `engine/resolve.resolve_all`), `app/state.py` (`ProjectState` = plan with Telegram ids,
history seeding, outbox, snapshot file), `scripts/` and the tests.

## Implementation

| File | What |
|---|---|
| `kraftsync/graph/store.py` | `EventStore`: the append-only event log (`events.jsonl`; memory-only when no path), thread-safe |
| `kraftsync/graph/cpm.py` | `Net.compile()` (topological order + adjacency lists, built once), forward/backward pass |
| `kraftsync/graph/project_graph.py` | `ProjectGraph` (everything in the interface above) |
| `kraftsync/graph/feed.py` | `read_reports` / `feed`: apply already-interpreted JSONL lines (history, gold labels). Not the engine. |
| `data/demo/history.jsonl` | what happened 2–9 Nov, as messages |
| `tests/test_graph.py`, `tests/test_baseline.py`, `tests/test_api.py` | the "Done when" cases, the baseline guard, the HTTP replay of the gold messages |

Decisions (deterministic, tested):
- `progress` on a task that has not started, with no `%`, is a confirmation ("screed is fine for Thursday"), not a start.
- out_of_sequence / hold_point are checked only when the status changes, so later progress reports do not raise the same alert again.
- A started-out-of-sequence task still cannot finish before its unfinished FS/FF predecessors.
- A `done` task with only `actual_finish` gets start = finish − duration + 1.

## Done when (tests)

- Loading `data/demo/plan.json` reproduces its planned dates via CPM (`test_cpm_reproduces_planned_dates_on_baseline`).
- Demo message 2 (plumbing delay to 13 Nov) → handover moves 12-06 → 12-09 and `T6-INSP`, `T6-BOARD`, … are in `affected_task_ids`.
- Demo message 5 (T6-BOARD started) → `out_of_sequence` + `hold_point_violation`.
- Replaying the event log reproduces identical state (`test_replay_reproduces_identical_state`, `test_state_survives_restart_and_reset`).
- All demo messages in order produce exactly the expected discrepancy types (`test_demo_replay_matches_gold`, `test_whole_demo_chat_raises_exactly_the_gold_discrepancies`).
