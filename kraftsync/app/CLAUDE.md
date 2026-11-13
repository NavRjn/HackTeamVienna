# app — local rules
Read `docs/07-deployment-demo/README.md` first.
- FastAPI. Read-only views over graph state + event log, plus `POST /messages` and `POST /reset` (demo).
- UI must be legible on a projector: big fonts, status colours, critical path obvious, handover date always visible.
- The manual message box is the stage fallback — it must always work, even if Telegram is down.
- Keep the API contract in the doc stable; if you change it, update the doc in the same commit.
- Start only via `python -m kraftsync.app.serve` (token check, random port, no docs). The HTTP contract is `docs/api.md`.
- Never log tokens, sender refs or message text. `/context` is POST on purpose.
