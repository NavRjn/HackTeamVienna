# n8n workflow (for the n8n owner)

Goal: Telegram message in → our local service does the checking → the right people get a Telegram message out.
n8n does exactly two things: **the one AI step** and **chat I/O**. Everything else is the local service
([`docs/api.md`](api.md) is the contract). Reuse the existing scope-creep workflow: replace the Notion nodes
with the two HTTP calls below.

```
Workflow 1 (intake):   Telegram Trigger → Normalize → HTTP /context → IF known person → LLM (claims) → Build request → HTTP /updates
Workflow 2 (notifier): Webhook (or schedule + GET /actions) → Split actions → (optional AI reword) → Telegram Send → HTTP /actions/result
```

Two workflows so that sending is a separate step that can be retried, reworded by an agent, and reported back. **Exactly one workflow sends an action.**

## Setup (once)
- **Telegram bot** via BotFather. **Privacy mode off** (`/setprivacy` → Disable) so it sees all group messages. Add it to the demo group.
- **n8n credentials / variables**
  - Header Auth credential `KraftSync`: header `X-KraftSync-Token`, value = the shared token (ask the team, never commit it).
  - Variable `KRAFTSYNC_BASE_URL` = the tunnel URL (changes whenever the tunnel restarts).
  - Telegram bot credential, Anthropic credential.
- n8n Cloud cannot reach our LAN address: always use the `https://….trycloudflare.com` URL.

## Nodes

1. **Telegram Trigger** — update type `message`. Used fields: `message.text`, `message.message_id`, `message.chat.id`,
   `message.from.id` (the sender's Telegram user id), `message.date` (unix seconds).
2. **Normalize** (Code or Set). Stop if there is no text, the sender is a bot, or the text is a `/command`. Produce:
   - `sender_id` = `message.from.id` (Telegram user id; the service matches it to `telegram_chat_id` in the plan)
   - `received_at` = ISO timestamp from `message.date`
   - `text`, `chat_ref` = `String(chat.id)`
   - `base_id` = `` `tg:${chat.id}:${message_id}` ``
3. **HTTP Request → `POST {{KRAFTSYNC_BASE_URL}}/context`**, JSON body `{ sender_id, received_at, text }`.
   Returns `{ person, tasks[≤15], related_tasks[≤15], today }`: `tasks` = the sender's own tasks active within ±3 days of the message date, `related_tasks` = other people's tasks that may be meant.
4. **IF `person` is null** → stop (unknown sender; nothing to do).
5. **LLM node** (Basic LLM Chain or AI Agent + Anthropic chat model + **Structured Output Parser**). It sees the message,
   the sender, `today` (and its weekday) and the `tasks` and `related_tasks`. Prompt rules (this is the only non-deterministic step):
   - choose `task_id` **only** from `tasks` or `related_tasks`, and prefer `tasks` (the sender's own work); otherwise `null`; never invent ids, people, dates or dependencies;
   - one **claim** per task the message talks about; no task content (greeting, question about keys) → `kind: "irrelevant"`;
   - `kind` is one of `started progress completed delayed blocked unblocked modified question irrelevant`;
   - resolve relative dates ("Donnerstag", "morgen", "+1 Tag") against `today` into `YYYY-MM-DD`;
   - honest `confidence` (0–1); ambiguous ("fertig" with several open tasks) → `task_id: null`, low confidence: the service then asks the sender;
   - `rationale`: one short sentence;
   - glossary: Beplankung = boarding, Vorwand = pre-wall, Rohinstallation = rough-in, Estrich = screed, Abnahme = inspection/acceptance, Top = apartment, OG = floor, fertig = done.

   Output schema for the parser: [`n8n/claims.schema.json`](../n8n/claims.schema.json) (compact: `task_id kind confidence finish days_left detail why`).
   - `finish` = a date the message names (copied from the calendar table); `days_left` = "2 more days" (days of work still needed from today). The model never adds dates itself: **Build request** turns `days_left` into `today + n`.
   - Put a **Prep prompt** Code node ([`n8n/prep-prompt.js`](../n8n/prep-prompt.js)) before the LLM: it renders a 15-day calendar (weekday + date) and the task lists, so a small model copies dates instead of computing them.
   - One claim per task, merge repeats; low temperature; no tools (the LLM calls no API).
6. **Build request** (Code, [`n8n/build-request.js`](../n8n/build-request.js)). Adds what the LLM must not decide:
   `message_id = base_id#i`, `reported_on`, `model`, `new_forecast_finish = finish ?? today + days_left`, `detail` → `blocker` / `change_description` by kind.
   Guards: unknown `task_id` → `null` + confidence 0; duplicate claims per task collapsed (most confident wins); a task-less claim is dropped when a real one exists
   (otherwise the sender gets a needless "which task?"); no claims at all → one `irrelevant` claim so the message is still logged.
7. **HTTP Request → `POST {{KRAFTSYNC_BASE_URL}}/updates`**. Retries are safe: `message_id` makes it idempotent.
   Response: `{ status, results, discrepancies, actions[], recipients{} }`.
The intake workflow **ends here**. The service has already written the live files (`data/live/events.jsonl`, `graph.json`) and put the
messages to send into its outbox; it also pushes them to the notifier's webhook if `N8N_CONFLICT_WEBHOOK_URL` is set.
(Simplest alternative without a second workflow: loop over `actions` from the response, Telegram Send to
`recipients[to_person_id].telegram_chat_id`, then call `POST /actions/result` for each. Do not combine it with workflow 2.)

## Workflow 2: Notifier (the "send messages to the relevant people" agent)
1. **Trigger**, one of:
   - **Webhook** node, Header Auth with the `N8N_HOOK_TOKEN` value; body `{ message_id, discrepancies, actions, recipients }`; or
   - **Schedule** every ~15 s → **HTTP Request `GET {{KRAFTSYNC_BASE_URL}}/actions`** (default `status=open`, i.e. drafted + failed).
2. **Split Out `actions`** → one item per message. Only handle items whose `status` is `drafted` or `failed` (a webhook push carries drafts).
3. *(Optional)* **AI agent**: reword `text` for tone and language (`recipient`/person language, role, urgency). It may only rephrase:
   every date, task name and number in `text` must appear unchanged in the result; if in doubt send the original `text`.
4. **Telegram Send Message**: `chat_id = recipient.telegram_chat_id` (from `GET /actions`; from the webhook use
   `recipients[to_person_id].telegram_chat_id`), `text`. If the chat id is null, skip sending.
5. **HTTP Request `POST {{KRAFTSYNC_BASE_URL}}/actions/result`** with `{ action_id, status: "sent" }`, or `{ action_id, status: "failed", detail }`
   when the send errored or there was no chat id (use the node's error output so failures are reported too). Retrying is safe.

What the people receive for the demo messages is listed below. Nothing in n8n decides who is affected or whether something is a conflict.

## What the service sends back (examples)
- Plumber: "Parts missing, Thursday + 1 day" → three actions: inspector (FYI new date), drywaller (FYI new date),
  site manager (decision: handover +3 days).
- Drywaller: "Already started boarding the bathroom" → site manager and inspector (STOP, hold point violated), drywaller (asked to confirm).
- Electrician: "fertig" (ambiguous) → one question back to the electrician: which task?

## Stage mode and testing without phones
- Set `DEMO_REDIRECT_CHAT_ID` (service `.env`) to the group's chat id: every message goes to that one chat, prefixed
  `[an <name>]`. No per-person Telegram ids needed.
- Add a second **Manual Trigger** path that feeds the 10 messages of `data/demo/messages.jsonl` into step 2 (set `sender_id`
  from the persons list). That exercises the real LLM node and lets you compare `kind` and `task_id` with the gold labels
  (this is where the LLM accuracy numbers come from).
- `POST /reset` between rehearsals.

## Later (not needed for the skeleton)
- Replies to clarification questions ("Welche Aufgabe meinst du?"): the answer has to be linked to the open question.
- A schedule trigger every 15 min for "task overdue and nobody reported" (service endpoint not built yet).
- An Error Trigger workflow that messages the team if a node fails during the demo.
