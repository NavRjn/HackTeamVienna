# KraftSync dashboard

Read-only UI for the FastAPI project state and audit log. The dashboard uses a local Node proxy, browser modules, and a zoomable SVG dependency graph. The graph lays out backend dependencies in location lanes, outlines open issues, and highlights task IDs named by backend impact records.

## Run locally

Use Python 3.11+, uv, and Node 22+. From the repository root, start FastAPI:

```powershell
uv sync --locked
$env:KRAFTSYNC_HOST = '127.0.0.1'
uv run python -m kraftsync.app.serve
```

From a second terminal in the same checkout:

```powershell
npm --prefix frontend start
```

Open http://127.0.0.1:5173. The proxy reads `KRAFTSYNC_TOKEN` and `KRAFTSYNC_PORT` from the repository's ignored `.env`, with environment variables taking precedence. For an existing remote API set `KRAFTSYNC_API_URL` to its HTTPS origin and supply the matching token through the server environment. `KRAFTSYNC_UI_PORT` overrides 5173. The dashboard serves loopback only. Keep the backend token private.

## Demo

Point n8n and the dashboard at the same API instance. Follow `docs/07-deployment-demo/README.md` for the scripted Top 6 plumbing delay. In the dashboard, open **Issues**, select **Inspect impact**, and compare the reported task, downstream highlights, schedule, and activity against the project handover at the top of the page. The API receives the updates through the existing integration. The dashboard refreshes both read endpoints every two seconds after the previous requests finish.

The activity view fetches the complete log so that resets and reused event IDs appear correctly during rehearsals. For larger logs, the team would need event pagination and reset detection. The UI shows location and assignee IDs supplied by the read contract. Project progress is the unweighted mean of task percentages excluding cancelled tasks. The dashboard displays reported downstream impact and whether the critical path is affected, since the API supplies no formal critical-path chain. Notification delivery is visible only when recorded by the backend.

## Checks

```powershell
npm --prefix frontend run check
npm --prefix frontend test
uv run pytest -q
```
