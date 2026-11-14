# app — local rules
Read `docs/api.md` (the contract) and `docs/07-deployment-demo/README.md` first.
- FastAPI service for n8n and the UI. Routes: `/health`, `POST /context`, `POST /updates`, `GET /state`, `GET /events`, `GET /actions`, authenticated `/documents` upload/list/content, `POST /reset`. Keep `docs/api.md` true: if you change a route or shape, update it in the same commit and tell the n8n owner.
- Start only via `python -m kraftsync.app.serve` (token check, random port, no docs, no access log). `create_app()` refuses to run without a strong token.
- Never log tokens, names or message text. `/context` is a POST on purpose.
- `state.py` is a placeholder for the `graph/` package; `api_models.py` holds the HTTP envelopes so domain-model refactors don't change the wire format by accident.
- UI must be legible on a projector: big fonts, status colours, forecast/handover date always visible. It only needs `GET /state` and `GET /events`.
- Stage fallback: `POST /updates` (or the n8n Manual Trigger) must work even if Telegram is down.
