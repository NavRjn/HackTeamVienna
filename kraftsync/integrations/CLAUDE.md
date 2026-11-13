# integrations — local rules
Read `docs/03-integrations/README.md` first.
- Translate channel I/O ↔ `InboundMessage` / `Action`. No interpretation, no graph logic here.
- Sender resolution is deterministic (phone E.164 / telegram id or handle / email → Person.id). Unknown sender → None, still forward the message.
- Hand inbound messages to `kraftsync.engine.pipeline.handle(msg)`; until it exists, write them to `data/inbox.jsonl`.
- Respect `DRY_RUN` and `DEMO_REDIRECT_CHAT_ID`. A failed send returns the Action with status=failed; it never raises into the pipeline.
- Tokens only from env. Never log tokens or full phone numbers.
