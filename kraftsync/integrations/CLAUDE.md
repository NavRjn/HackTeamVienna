# integrations — local rules
Read `docs/03-integrations/README.md` first.
- **Reserved package; empty on purpose.** Channel I/O (Telegram, later WhatsApp/email) is done by n8n. The local service never talks to a chat platform.
- Only add code here if a Python-side channel is really needed (e.g. `parse_whatsapp_export(zip) -> list[InboundMessage]` to replay an exported chat). No interpretation and no graph logic in this package.
- Sender identity is resolved by the service from the **full name** (`ProjectState.resolve_sender`); do not add a second resolver.
- Tokens only from env. Never log tokens, names or message text.
