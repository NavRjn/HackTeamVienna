# 03 · Channels (Telegram via n8n)

**Status: the Python `integrations/` package is not used.** Channel I/O is done by n8n; the local service never talks to Telegram. This doc says what that means in practice. The workflow itself is in [`docs/n8n-workflow.md`](../n8n-workflow.md); the HTTP contract is [`docs/api.md`](../api.md).

## Channels

| Channel | Inbound | Outbound | Status |
|---|---|---|---|
| **Telegram** | n8n *Telegram Trigger* on the site group (bot privacy mode **off** so it sees all messages) | n8n *Telegram Send Message* to `recipients[person].telegram_chat_id` | Primary demo channel |
| **WhatsApp** | n8n WhatsApp Business Cloud trigger, or a chat export replayed into the same flow | n8n send node (template rules apply outside the 24 h window) | Later |
| **Email** | n8n Gmail/IMAP trigger | n8n Gmail/SMTP send | Later; good for inspector/ÖBA formal messages |
| **Manual** | n8n Manual Trigger feeding `data/demo/messages.jsonl`; or a direct `POST /updates` | — | Test path and stage fallback |

Every channel joins the same flow: normalise to `sender_id` (Telegram user id), `received_at`, `text`, a unique id → `POST /context` → LLM → `POST /updates`. Adding a channel = adding a trigger and a send node in n8n; the local service does not change.

## Who does what

| Responsibility | Where |
|---|---|
| Receive/send channel messages, download media | n8n |
| Normalise to `sender_id` (`message.from.id`), `received_at`, `text`, `base_id` (`tg:<chat>:<msg>`) | n8n (Normalize node) |
| **Sender resolution** | local service, by **Telegram user id** = `Person.telegram_chat_id` in the plan. Unknown id → `person: null` and no tasks → claim is `ignored: unknown sender`. Telegram authenticates the id, so it cannot be faked with a display name |
| Message idempotency | `message_id` per claim (`<base_id>#1`, `#2`, ...); the service answers `duplicate` on a repeat |
| Choosing the chat to send to | the service returns `recipients[person_id].telegram_chat_id`; n8n sends |
| Telling the right people | local service (`engine/resolve.py` + `policy.yaml`) |

## Demo-safety switches

- `DEMO_REDIRECT_CHAT_ID` (local `.env`): every recipient's chat id becomes this chat and each text is prefixed `[an <name>]`. Use it on stage, and for the skeleton before per-person chat ids exist. **Implemented.**
- `DRY_RUN`: **not implemented** (n8n decides whether to send; disable the Send node to dry-run).
- `DEMO_TODAY` (local `.env`): freezes "today" for candidate selection, date sanity checks and the LLM prompt (it is returned by `/context`).

## For the demo phones

Every demo person needs their Telegram **user id** (the number n8n sees as `message.from.id`) as `telegram_chat_id`: it is how the service recognises the sender and where it sends messages back (a person must have started the bot once to receive DMs). Put each person's Telegram user id into `scripts/generate_demo_plan.py` (`Person(..., telegram_chat_id=<id>)`) and run `uv run python scripts/generate_demo_plan.py`: it writes both `persons.csv` and `plan.json`, and the service reads **`plan.json`**, so editing only the CSV changes nothing. Without an id the person is an unknown sender and their messages are ignored.

## Stretch (Python side, only if needed)

`kraftsync/integrations/` could hold `parse_whatsapp_export(zip) -> list[InboundMessage]` for replaying an exported chat. `InboundMessage` exists in the contracts for that purpose but the live flow does not use it.
