# 07 · Deployment, UI & demo

Goal: it runs somewhere reachable, it looks clear on a projector, and the submission is complete by **Sat 15:00**.

## UI (minimum)

One page, three panels, served by FastAPI (`kraftsync/app/`):
1. **Graph view** — Cytoscape.js with dagre layout (data from `nx.cytoscape_data`). Node colour = status, red border = open discrepancy, thick edges = critical path. Group by location (Top 5 / Top 6).
2. **Gantt strip** — baseline vs forecast bars (frappe-gantt or plain SVG). The "handover moved" moment must be visible here.
3. **Feed** — live messages → interpretation → discrepancies → actions sent, newest first (SSE or 2 s polling).
Plus a **manual message box** (sender dropdown + text) as the on-stage fallback if Telegram misbehaves.

Lovable (credits available) can generate this front end against our API if that's faster — keep the API contract (`GET /state`, `GET /events`, `POST /messages`) stable either way.

## API (target)

```
GET  /state            → tasks with status + forecast, edges, critical path, project finish
GET  /graph            → cytoscape JSON
GET  /events?since=id  → event log tail
GET  /discrepancies    → open discrepancies
POST /messages         → inject an InboundMessage (manual channel / n8n webhook)
POST /reset            → reload demo plan, clear events (demo only)
```

## Hosting options (pick one by Fri night)

| Option | Fit |
|---|---|
| **Laptop + Telegram polling** | Zero deploy risk. Default for the live demo. |
| **Maritime** (3 months Growth for every participant, top-3 prize) | Persistent agent runtime with sleep/wake and webhook/cron triggers — good fit for "one agent per site". Deploy the same process; use it for the `silent_overdue` cron. |
| n8n Cloud (1 month Pro) | Glue for email / WhatsApp Cloud API → `POST /messages`. Not for core logic. |
| Docker on any VM | Fallback. |

Whatever is chosen: one `docker compose up` or one `uv run` command, documented in README.

## Demo checklist

- [ ] Demo Telegram group with 3 team phones acting as electrician / plumber / drywaller; presenter phone = site manager (`DEMO_REDIRECT_CHAT_ID`).
- [ ] `POST /reset` between rehearsals.
- [ ] Scripted messages printed on a card (from `data/demo/messages.jsonl`), in order.
- [ ] Fallback: replay script / manual box if Wi-Fi or Telegram fails.
- [ ] 3-min video recorded by **Sat 13:30** (end-to-end, real Telegram).

## Submission (Discord form, by 15:00)

- Repo: check with organisers whether Talona work goes public MIT in the proximata org or private to the partner (the slides say both). Add LICENSE accordingly.
- README: setup, run, environment, architecture, assumptions. `.env.example`, no secrets.
- 3-minute demo video.
- Written submission: title, one-line pitch, team, problem, solution, stack, links.
