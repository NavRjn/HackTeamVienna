# 07 · Deployment, UI & demo

Goal: it runs somewhere reachable, it looks clear on a projector, and the submission is complete by **Sat 15:00**.

## Runtime layout

```
Telegram group ⇄ n8n Cloud ⇄ (cloudflared tunnel, https) ⇄ local FastAPI service (our laptop) ⇄ plan.json + events.jsonl
                                                                   ▲
                                                                   └── UI (GET /state, GET /events)
```

Start the service (always through this entry point):
```bash
uv run python -m kraftsync.app.serve            # run
uv run python -m kraftsync.app.serve --reload   # dev: restart on code/yaml changes
uv run python -m kraftsync.app.serve --init     # only create token/port in .env and print them
cloudflared tunnel --url http://127.0.0.1:<KRAFTSYNC_PORT>   # prints https://<random>.trycloudflare.com
```
First run generates `KRAFTSYNC_TOKEN` and a random `KRAFTSYNC_PORT` into the gitignored `.env`, so the local URL stays stable. The **tunnel URL changes every time the tunnel restarts**; update the n8n variable `KRAFTSYNC_BASE_URL`. The service binds all interfaces by default (teammates on the same Wi-Fi can reach it); set `KRAFTSYNC_HOST=127.0.0.1` to keep it local.

### Security on shared Wi-Fi
- Every route except `GET /health` needs the header `X-KraftSync-Token`; the service refuses to start without a token of at least 24 characters. The token is the only lock: share it privately, never commit it.
- Plain HTTP on the LAN can be sniffed; prefer the https tunnel URL, including for teammates.
- API docs are off (`KRAFTSYNC_DOCS=1` enables them on loopback only), the access log is off, nothing logs tokens or message text, `/context` is a POST so names never land in URLs.
- The webhook URL to n8n comes only from the environment.

## Environment (`.env`, see `.env.example`)

| Variable | Meaning |
|---|---|
| `KRAFTSYNC_TOKEN`, `KRAFTSYNC_HOST`, `KRAFTSYNC_PORT` | shared secret, bind address, port (all auto-generated/defaulted) |
| `PLAN_PATH`, `EVENTS_PATH` | plan file (default `data/demo/plan.json`), event log (default `data/events.jsonl`) |
| `DEMO_TODAY` | freeze "today" (`2026-11-09`): **set it for the scripted demo**, or "Donnerstag" resolves against the real date |
| `DEMO_REDIRECT_CHAT_ID` | stage mode: all outbound messages to this one chat |
| `CONFIDENCE_MIN` | below this the sender is asked (default 0.7) |
| `N8N_CONFLICT_WEBHOOK_URL`, `N8N_HOOK_TOKEN` | optional webhook we call when there is a conflict |

LLM and Telegram credentials live in **n8n**, not in this repo. `ANTHROPIC_API_KEY`, `TELEGRAM_*`, `DB_PATH`, `DRY_RUN`, WhatsApp/email variables in `.env.example` are not read by the local service.

## API

See [`docs/api.md`](../api.md): `GET /health`, `POST /context`, `POST /updates`, `GET /state`, `GET /events`, `POST /reset`. (There is no `POST /messages`; the manual fallback is `POST /updates` or the n8n Manual Trigger.)

## UI (minimum)

One page, polling the API (every ~2 s):
1. **Graph view** — Cytoscape.js with dagre layout from `GET /state` (`tasks`, `dependencies`). Node colour = status, red border = open discrepancy. Group by location (Top 5 / Top 6).
2. **Dates** — per task planned vs forecast (`cpm_start`/`cpm_finish`); the **project finish planned vs forecast** (`project_finish_planned` / `project_finish_forecast`) always visible: the "handover moved" moment.
3. **Feed** — `GET /events?since=<id>` plus `open_discrepancies`, newest first.

The API needs the token on every call: the page asks for it once and keeps it in the browser (do not hard-code it in a committed file). Lovable (credits available) can generate this front end against the API if that's faster.

## Hosting

| Option | Fit |
|---|---|
| **Laptop + cloudflared quick tunnel + n8n Cloud** | The setup in use. Zero deploy risk; needs the laptop and its network up. |
| Named Cloudflare tunnel | Stable URL (needs a free Cloudflare account) |
| Maritime (3 months Growth for every participant) | Possible later for hosting the service itself; not needed for the demo |
| Docker on any VM | Fallback |

## Demo checklist

- [ ] `.env`: `DEMO_TODAY=2026-11-09`; `DEMO_REDIRECT_CHAT_ID` = the demo group if per-person chat ids are not filled in.
- [ ] Service running, tunnel up, n8n `KRAFTSYNC_BASE_URL` points at the current tunnel URL; `GET <url>/health` is ok.
- [ ] Demo Telegram group with the bot (privacy mode off); each demo person's Telegram user id is set as `telegram_chat_id` in the plan (see `docs/03-integrations`: edit `scripts/generate_demo_plan.py` and regenerate; the service reads `plan.json`).
- [ ] `POST /reset` between rehearsals.
- [ ] Scripted messages printed on a card (from `data/demo/messages.jsonl`), in order.
- [ ] Fallback if Telegram or Wi-Fi fails: n8n Manual Trigger with the scripted messages, or `curl` to `/updates`.
- [ ] 3-min video recorded by **Sat 13:30** (end-to-end, real Telegram).

## Submission (Discord form, by 15:00)

- Repo: the repo carries an MIT `LICENSE`, but the Talona slide says Talona work goes to a private repository, not MIT. Check with the organisers.
- README: setup, run, environment, architecture, assumptions (done). `.env.example`, no secrets.
- 3-minute demo video.
- Written submission: title, one-line pitch, team, problem, solution, stack, links.
