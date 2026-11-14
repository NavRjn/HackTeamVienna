# Local service API (v0, frozen for the hackathon)

n8n ⇄ local service. n8n does the chat I/O and the single AI step; this service does everything deterministic
(sender lookup, candidate shortlist, graph state, conflict detection, who-gets-told). Models are in
`kraftsync/contracts/models.py` (`TaskUpdate`, `Discrepancy`, `Action`, ...) and `kraftsync/app/api_models.py`.

Start: `uv run python -m kraftsync.app.serve` (first run generates the token + a random port into `.env` and prints
the URL). Change a route or shape only with everyone's OK, and update this file in the same commit.

## Auth
Every route except `GET /health` needs the header `X-KraftSync-Token: <token>` (else `401`).
The service is reachable on the LAN and through the tunnel; the token is the only lock. Plain HTTP on the LAN
means the token can be sniffed there, so use the https tunnel URL whenever possible.

## Who is the sender
By **Telegram user id** (`message.from.id`, an integer; a numeric string is accepted), sent as `sender_id`. The service matches it to
`telegram_chat_id` of a person in the plan. An unknown id gives `person: null` with empty task lists (and `ignored: unknown sender`
on `/updates`), so nothing about the project leaks to strangers. Telegram authenticates the id; display names are not used.
Where the ids come from: Type each person's Telegram user id into the `telegram_chat_id` column of `data/demo/persons.csv`: the service reads it from there (every non-empty numeric value overrides `plan.json`; blank or invalid cells are ignored). Ids are loaded at startup and by `POST /reset`, not on every request, so restart the service or call `/reset` after editing (`/reset` also clears the event log). `scripts/generate_demo_plan.py` rewrites both files without ids: re-running it wipes them.

"Today" = `DEMO_TODAY` if set (stage override), else the date of `received_at`, else the server's date.

## Logs
Every call is logged as one compact line to the console of the process running `serve` and to `data/kraftsync.log`
(`LOG_PATH`, `off` = console only), e.g.
```
00:25:45 POST /context 200 5ms | sender=1125811575 -> P-ELE | today=2026-11-09 tasks=2 related=10
00:25:45 POST /context 200 2ms | sender=123456789 -> UNKNOWN (no person in the plan has this telegram id) | today=2026-11-09 tasks=0 related=0
00:26:02 POST /updates 200 31ms | sender=1125811575 -> P-ELE | tg:1:5#1 delayed T6-PLUMB-RI c=0.93 -> applied [delay] actions=3
00:26:09 GET /state 401 1ms | invalid or missing token
00:26:15 POST /context 422 1ms | invalid request: body.sender_id: Field required
```
On startup it prints which persons have a Telegram id and which do not. Logged: method, path, status, duration, the sender id and
the person it resolved to, task ids, claim kinds, confidence, resulting status, discrepancy types, number of actions, validation
errors (field names only), the n8n webhook result. **Never logged:** the token, message text, submitted values.
Successful `GET /health`, `/state`, `/events` are not logged (UI polling would bury the real calls); errors on them are.

## Routes
| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | `{"ok": true}` (no token) |
| POST | `/context` | n8n → who is this + the candidate tasks to put in the LLM prompt |
| POST | `/updates` | n8n → one or more interpreted claims (`TaskUpdate`) from one message; we apply, check, resolve |
| GET | `/state` | tasks (+ computed `cpm_start/cpm_finish`), dependencies, open discrepancies, planned vs forecast project finish |
| GET | `/events?since=<id>` | append-only event log tail (UI feed) |
| POST | `/reset` | reload the demo plan and clear the event log (demo only) |

Both n8n calls are POSTs so names and message text never appear in URLs or logs.

### POST /context
```json
{ "sender_id": 123456789, "received_at": "2026-11-09T08:15:00+01:00", "text": "Top 6 Sanitär wird ..." }
```
`received_at` and `text` are optional. → `{ "person": Person | null, "tasks": [Task, ...≤15], "related_tasks": [Task, ...≤15], "today": "2026-11-09" }`.

- **`tasks`**: the sender's own tasks (assigned to them or to their trade) that are *active around today*: in progress/blocked,
  overlapping the ±3-day window, overdue and still open, or finished inside the window. Own tasks further out are not listed.
- **`related_tasks`**: other people's tasks that may be meant, in this order: tasks at locations whose name words all appear in
  `text` ("Top 6", "Bad Top 6" → that unit/room and everything below it), direct predecessors/successors of the sender's tasks,
  other tasks active around today. Never contains a task from `tasks`. The sender's own tasks outside the ±3-day window
  appear here only when the message names their location (so "can I start waterproofing in Top 5?" can still find a task 3 weeks out).

The LLM must pick `task_id` only from these two lists, preferring `tasks` (or null). Unknown ids get `422`.

### POST /updates
```json
{
  "sender_id": 123456789,
  "text": "Top 6 Sanitär wird diese Woche nicht fertig ...",
  "received_at": "2026-11-09T08:15:00+01:00",
  "channel": "telegram",
  "chat_ref": null,
  "updates": [ {
    "message_id": "tg:-100123:456#1", "task_id": "T6-PLUMB-RI", "kind": "delayed", "confidence": 0.93,
    "reported_on": "2026-11-09", "new_forecast_finish": "2026-11-13",
    "blocker": "pre-wall elements not delivered", "rationale": "supplier Thursday, +1 day", "model": "n8n-claude"
  } ]
}
```
`updates` holds 1–10 claims, each exactly the contract `TaskUpdate`. **Each claim needs its own unique
`message_id`** (Telegram chat id + message id + `#1`, `#2`, ...): it is the idempotency key, and duplicates within a
request are rejected with `422`. Claims are processed in order.

Response `UpdateResponse` (aggregate; `results` has the per-claim detail):
```
status:        applied | needs_clarification | ignored | duplicate   (the most "applied" status over the claims)
results:       [ { message_id, status, note, events, discrepancies, actions } ]
events:        GraphEvent[]       everything logged
discrepancies: Discrepancy[]      what the deterministic check found
actions:       Action[]           who to tell what; text is ready to send (one per person per claim)
recipients:    { person_id: { person_id, name, telegram_chat_id } }
```
Per claim:
- `needs_clarification`: confidence below `CONFIDENCE_MIN` or `task_id` null → nothing applied, one `ask` action to the sender.
- `ignored`: `irrelevant`/`question` kinds or an unknown sender → logged, not applied.
- `duplicate`: same `message_id` seen before → nothing happens (safe to retry).

### Webhook we call (n8n side, optional)
When a response has discrepancies or actions and `N8N_CONFLICT_WEBHOOK_URL` is set, the service also POSTs this
(header `X-KraftSync-Token: $N8N_HOOK_TOKEN`, 5 s timeout, failure is logged and never fails the request):
```
{ "message_id": "<first claim>", "discrepancies": [...], "actions": [...], "recipients": {...} }
```
n8n can simply use the `/updates` response instead; the webhook is for events the server starts itself (later: overdue timer).
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
No handling yet for replies to clarification questions.
