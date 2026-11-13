# n8n workflow (for the n8n owner)

Goal: Telegram message in → our local service does the checking → the right people get a Telegram message out.
n8n does exactly two things: **the one AI step** and **chat I/O**. Everything else is the local service
([`docs/api.md`](api.md) is the contract). Reuse the existing scope-creep workflow: replace the Notion nodes
with the two HTTP calls below.

```
Telegram Trigger → Normalize → HTTP /context → IF known person → LLM (claims) → Build request → HTTP /updates → Split actions → Telegram Send
```

## Setup (once)
- **Telegram bot** via BotFather. **Privacy mode off** (`/setprivacy` → Disable) so it sees all group messages. Add it to the demo group.
- **n8n credentials / variables**
  - Header Auth credential `KraftSync`: header `X-KraftSync-Token`, value = the shared token (ask the team, never commit it).
  - Variable `KRAFTSYNC_BASE_URL` = the tunnel URL (changes whenever the tunnel restarts).
  - Telegram bot credential, Anthropic credential.
- n8n Cloud cannot reach our LAN address: always use the `https://….trycloudflare.com` URL.

## Nodes

1. **Telegram Trigger** — update type `message`. Used fields: `message.text`, `message.message_id`, `message.chat.id`,
   `message.from.first_name`, `message.from.last_name`, `message.date` (unix seconds).
2. **Normalize** (Code or Set). Stop if there is no text, the sender is a bot, or the text is a `/command`. Produce:
   - `sender_name` = `first_name + " " + last_name` (full name; the service matches on it)
   - `received_at` = ISO timestamp from `message.date`
   - `text`, `chat_ref` = `String(chat.id)`
   - `base_id` = `` `tg:${chat.id}:${message_id}` ``
3. **HTTP Request → `POST {{KRAFTSYNC_BASE_URL}}/context`**, JSON body `{ sender_name, received_at, text }`.
   Returns `{ person, candidates[≤15], today }`.
4. **IF `person` is null** → stop (unknown sender; nothing to do).
5. **LLM node** (Basic LLM Chain or AI Agent + Anthropic chat model + **Structured Output Parser**). It sees the message,
   the sender, `today` (and its weekday) and the `candidates`. Prompt rules (this is the only non-deterministic step):
   - choose `task_id` **only** from `candidates`, otherwise `null`; never invent ids, people, dates or dependencies;
   - one **claim** per task the message talks about; no task content (greeting, question about keys) → `kind: "irrelevant"`;
   - `kind` is one of `started progress completed delayed blocked unblocked modified question irrelevant`;
   - resolve relative dates ("Donnerstag", "morgen", "+1 Tag") against `today` into `YYYY-MM-DD`;
   - honest `confidence` (0–1); ambiguous ("fertig" with several open tasks) → `task_id: null`, low confidence: the service then asks the sender;
   - `rationale`: one short sentence;
   - glossary: Beplankung = boarding, Vorwand = pre-wall, Rohinstallation = rough-in, Estrich = screed, Abnahme = inspection/acceptance, Top = apartment, OG = floor, fertig = done.

   Output schema for the parser (if it rejects the `null` unions, make those fields optional instead):
   ```json
   { "type": "object", "required": ["claims"], "properties": { "claims": { "type": "array", "maxItems": 10, "items": {
     "type": "object", "required": ["task_id", "kind", "confidence", "rationale"], "properties": {
       "task_id": { "type": ["string", "null"] },
       "kind": { "type": "string", "enum": ["started","progress","completed","delayed","blocked","unblocked","modified","question","irrelevant"] },
       "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
       "new_forecast_finish": { "type": ["string", "null"], "description": "YYYY-MM-DD" },
       "percent_complete": { "type": ["integer", "null"] },
       "blocker": { "type": ["string", "null"] },
       "change_description": { "type": ["string", "null"] },
       "rationale": { "type": "string" } } } } } }
   ```
6. **Build request** (Code). For each claim `i` (1-based) add what the LLM must not decide:
   `message_id = base_id + "#" + i` (unique per claim), `reported_on` = the date of `received_at`, `model = "n8n-claude-v1"`.
   Guard: if a claim's `task_id` is not in `candidates`, set `task_id = null` and `confidence = 0`.
   If the LLM returned no claims, send one `{ task_id: null, kind: "irrelevant", confidence: 1, rationale: "no task content" }`
   so the message is still logged. Body: `{ sender_name, text, received_at, channel: "telegram", chat_ref, updates: [...] }`.
7. **HTTP Request → `POST {{KRAFTSYNC_BASE_URL}}/updates`**. Retries are safe: `message_id` makes it idempotent.
   Response: `{ status, results, discrepancies, actions[], recipients{} }`.
8. **Split Out `actions`** → one item per message to send. Skip an item if its recipient has no `telegram_chat_id`.
9. **Telegram Send Message**: `chat_id = recipients[action.to_person_id].telegram_chat_id`, `text = action.text`.

That is the whole walking skeleton. Nothing in n8n decides who is affected or whether something is a conflict.

## What the service sends back (examples)
- Plumber: "Parts missing, Thursday + 1 day" → three actions: inspector (FYI new date), drywaller (FYI new date),
  site manager (decision: handover +3 days).
- Drywaller: "Already started boarding the bathroom" → site manager and inspector (STOP, hold point violated), drywaller (asked to confirm).
- Electrician: "fertig" (ambiguous) → one question back to the electrician: which task?

## Stage mode and testing without phones
- Set `DEMO_REDIRECT_CHAT_ID` (service `.env`) to the group's chat id: every message goes to that one chat, prefixed
  `[an <name>]`. No per-person Telegram ids needed.
- Add a second **Manual Trigger** path that feeds the 10 messages of `data/demo/messages.jsonl` into step 2 (set `sender_name`
  from the persons list). That exercises the real LLM node and lets you compare `kind` and `task_id` with the gold labels
  (this is where the accuracy numbers for the pitch come from).
- `POST /reset` between rehearsals.

## Later (not needed for the skeleton)
- Replies to clarification questions ("Welche Aufgabe meinst du?"): the answer has to be linked to the open question.
- A schedule trigger every 15 min for "task overdue and nobody reported" (service endpoint not built yet).
- An Error Trigger workflow that messages the team if a node fails during the demo.
