# 03 · Integrations

Goal: channel-specific I/O ↔ channel-agnostic `InboundMessage` / `Action`. No business logic here.

## Channels

| Channel | Inbound | Outbound | Hackathon reality |
|---|---|---|---|
| **Telegram** | Bot in the site group (privacy mode **off** via BotFather so it sees all messages) or DMs | `sendMessage` to `telegram_chat_id` | Primary live demo channel. Works in minutes. |
| **WhatsApp** | (a) Chat export `.zip` (txt + media) → replay; (b) WhatsApp Business Cloud API webhook (Meta test number) | Cloud API send (template rules apply outside 24h window) | Export import is reliable and shows "works with your existing groups". Cloud API = stretch. |
| **Email** | IMAP poll or n8n Gmail trigger → webhook | SMTP / n8n | Good for inspector/ÖBA formal messages. |
| **Manual** | `POST /messages` from the UI | shown in UI | Always available fallback for the demo. |

n8n (credits available) is allowed as glue for email/WhatsApp: n8n receives → `POST /messages` on our API. Telegram stays in Python for reliability.

## Responsibilities

1. Normalise every inbound message to `InboundMessage` (unique `id` per channel, tz-aware `received_at`, attachments downloaded to `data/media/`).
2. **Sender resolution** — deterministic: phone (E.164) / telegram handle or chat id / email → `Person.id`. Unknown → `sender_person_id=None` (engine still sees it).
3. Hand off to the pipeline: `pipeline.handle(msg)` (engine owns the pipeline).
4. Deliver `Action`s on the person's channel; record status (`sent`/`failed`); never crash the pipeline on a send failure.
5. Replies: keep `reply_to_id` so the engine can match answers to its clarification questions.

## Demo-safety switches

- `DRY_RUN=1` → actions are printed/logged, not sent.
- `DEMO_REDIRECT_CHAT_ID` → all outbound goes to one chat (the presenter's phone), prefixed with the intended recipient's name. Use this on stage.

## Interface

```python
def resolve_sender(sender_ref: str, persons: list[Person]) -> str | None
def parse_whatsapp_export(zip_path: Path, persons: list[Person]) -> list[InboundMessage]
async def send(action: Action, persons: list[Person]) -> Action
```
CLI: `uv run python -m kraftsync.integrations.telegram_bot`

## Done when

- A message typed in the Telegram group appears as an `InboundMessage` with the right `sender_person_id`.
- An `Action` reaches the right Telegram chat (or the redirect chat).
- A WhatsApp export zip replays through the pipeline.
