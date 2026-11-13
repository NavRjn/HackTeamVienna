# Local service API (v0, frozen for the hackathon)

n8n ⇄ local service. n8n does the chat I/O and the single AI step; this service does everything deterministic
(sender lookup, candidate shortlist, graph state, conflict detection, who-gets-told). Models are in
`kraftsync/contracts/models.py` (`TaskUpdate`, `Discrepancy`, `Action`, ...) and `kraftsync/app/api_models.py`.

Start: `uv run python -m kraftsync.app.serve` (first run generates the token + a random port into `.env` and prints
the URL). Change a route or shape only with everyone's OK, and update this file in the same commit.

## Auth
Every route except `GET /health` needs the header `X-KraftSync-Token: <token>` (else `401`).
The service is reachable on the LAN; the token is the only lock. It travels over plain HTTP, so use an https tunnel
for anything outside the local network.

## Routes
| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | `{"ok": true}` (no token) |
| POST | `/context` | n8n → who is this + the candidate tasks to put in the LLM prompt |
| POST | `/updates` | n8n → the interpreted `TaskUpdate`; we apply, check, resolve. Returns the result |
| GET | `/state` | tasks (+ computed `cpm_start/cpm_finish`), dependencies, open discrepancies, planned vs forecast project finish |
| GET | `/events?since=<id>` | append-only event log tail (UI feed) |
| POST | `/reset` | reload the demo plan and clear the event log (demo only) |

`/context` is a POST so phone numbers / ids never appear in URLs or logs.

### POST /context
```json
{ "sender_ref": "+436600000004" }
```
→ `{ "person": Person | null, "candidates": [Task, ...≤15], "today": "2026-11-09" }`.
`sender_ref` = phone, telegram id/handle or email as listed in the persons. The LLM must pick `task_id` only from
`candidates` (or null). Unknown ids are rejected with `422`.

### POST /updates
```json
{
  "sender_ref": "+436600000004",
  "text": "Top 6 Sanitär wird diese Woche nicht fertig ...",
  "received_at": "2026-11-09T08:15:00+01:00",
  "channel": "telegram",
  "chat_ref": null,
  "update": {
    "message_id": "demo:02", "task_id": "T6-PLUMB-RI", "kind": "delayed", "confidence": 0.93,
    "reported_on": "2026-11-09", "new_forecast_finish": "2026-11-13",
    "blocker": "pre-wall elements not delivered", "rationale": "supplier Thursday, +1 day", "model": "n8n-claude"
  }
}
```
`update` is exactly the contract `TaskUpdate`. `message_id` must be unique per message (it is the idempotency key).

Response `UpdateResponse`:
```
status:        applied | needs_clarification | ignored | duplicate
note:          string | null
events:        GraphEvent[]       what was logged
discrepancies: Discrepancy[]      what the deterministic check found
actions:       Action[]           who to tell what (one per person); text is ready to send
recipients:    { person_id: { person_id, name, telegram_chat_id } }
```
- `needs_clarification`: confidence below `CONFIDENCE_MIN` or `task_id` null → nothing applied, one `ask` action to the sender.
- `ignored`: `irrelevant`/`question` kinds or an unknown sender → logged, not applied.
- `duplicate`: same `message_id` seen before → nothing happens (safe to retry).

### Webhook we call (n8n side)
When a response has discrepancies or actions, the service also POSTs this to `N8N_CONFLICT_WEBHOOK_URL`
(header `X-KraftSync-Token: $N8N_HOOK_TOKEN`, 5 s timeout, failure is logged and never fails the request):
```
{ "message_id": "...", "discrepancies": [...], "actions": [...], "recipients": {...} }
```
n8n loops over `actions`, sends `text` to `recipients[action.to_person_id].telegram_chat_id`.
With `DEMO_REDIRECT_CHAT_ID` set, every chat id is that one chat and each text is prefixed `[an <name>]` (stage mode).

## What is checked (deterministic)
| Discrepancy | Trigger | Severity |
|---|---|---|
| `delay` | task's forecast finish moves past its planned finish; carries `impact` (affected downstream ids, project finish before/after) | critical if project finish moves, else warning |
| `out_of_sequence` | `started`/`completed` while a predecessor is unfinished | warning |
| `hold_point_violation` | same, and an unfinished predecessor is a hold point | critical |
| `scope_change` | kind `modified` | warning |
| `blocked` | kind `blocked` | warning |

Known v0 limits: FS/SS dependencies only, calendar days, one forward pass. `progress` updates never start a task.
