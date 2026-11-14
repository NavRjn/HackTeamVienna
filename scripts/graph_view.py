"""Draw the dependency graph as an HTML page, step by step through the site messages.

    uv run python scripts/graph_view.py            # build graph_view.html once and open it
    uv run python scripts/graph_view.py --watch    # keep running: rebuilds whenever a message file changes,
                                                   # and the open browser tab refreshes itself

The plan (data/demo/plan.json) is the untouched baseline: every task not_started.
Progress comes ONLY from messages, read in this order every run:
  1. data/demo/history.jsonl    what happened 2-9 Nov (frames, rough-ins, Top 5 inspection, delivery)
  2. data/demo/messages.jsonl   the team's scripted demo chat (uses each line's "gold" interpretation)
  3. data/my_updates.jsonl      YOUR new messages - optional; append one line per message, e.g.

     {"text": "Abnahme Top 6 passt", "task_id": "T6-INSP", "kind": "completed", "date": "2026-11-12"}
     {"text": "Top 6 Estrich fertig", "task_id": "T6-SCREED", "kind": "completed", "date": "2026-11-15"}
     {"text": "Fliesen kommen erst Freitag", "task_id": "T6-TILE", "kind": "delayed", "date": "2026-11-20",
      "new_forecast_finish": "2026-12-09"}

     kind: started | progress | completed | delayed | blocked | unblocked | modified | question | irrelevant
     optional: "new_forecast_finish", "percent_complete", "blocker", "change_description", "sender"

Every message becomes one step in the page. Use the slider or Prev/Next to go back in time; the newest
step is shown by default. Nothing is written back to the repo data.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from datetime import date
from pathlib import Path

import networkx as nx

from kraftsync.contracts import ProjectPlan
from kraftsync.graph import EventStore, ProjectGraph, feed, read_reports

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "data/demo/plan.json"
HISTORY = ROOT / "data/demo/history.jsonl"
DEMO_MESSAGES = ROOT / "data/demo/messages.jsonl"
MY_UPDATES = ROOT / "data/my_updates.jsonl"
OUT = ROOT / "graph_view.html"
W, BOX_W, BOX_H, ROW_H, TOP = 680, 118, 40, 62, 20


# ---------------------------------------------------------------------------- building the steps

def layout(pg: ProjectGraph) -> dict[str, tuple[int, int]]:
    """Rows = longest path from the start (top to bottom). Top 5 left, Top 6 right."""
    layer: dict[str, int] = {}
    for n in nx.topological_sort(pg.g):
        layer[n] = max((layer[p] + 1 for p in pg.g.predecessors(n)), default=0)
    side = {"T5": 0, "F2": 1, "T6": 2}
    pos = {}
    for row in range(max(layer.values()) + 1):
        ids = sorted((n for n in layer if layer[n] == row), key=lambda n: (side.get(n[:2], 1), n))
        gap = (W - len(ids) * BOX_W) / (len(ids) + 1)
        for i, n in enumerate(ids):
            pos[n] = (round(gap + i * (BOX_W + gap)), TOP + row * ROW_H)
    return pos


def capture(pg: ProjectGraph, title: str, text: str, new_alerts: list, all_alerts: list) -> dict:
    snap = pg.snapshot()
    flagged: dict[str, list[str]] = {}
    for d in all_alerts:
        flagged.setdefault(d.task_id, []).append(d.type.value)
    new_ids = {id(d) for d in new_alerts}
    return {
        "title": title, "text": text, "today": snap["today"], "handover": snap["project_finish"],
        "alerts": [{"sev": d.severity.value, "text": d.description, "new": id(d) in new_ids}
                   for d in reversed(all_alerts)],
        "nodes": {
            t["id"]: {
                "name": t["name"], "status": t["status"], "critical": t["critical"],
                "start": t["forecast_start"], "finish": t["forecast_finish_cpm"],
                "slip": (date.fromisoformat(t["forecast_finish_cpm"]) - date.fromisoformat(t["planned_finish"])).days,
                "alerts": flagged.get(t["id"], []),
            }
            for t in snap["tasks"]
        },
    }


def build() -> dict:
    plan = ProjectPlan.model_validate_json(PLAN.read_text(encoding="utf-8"))
    pg = ProjectGraph.from_plan(plan, EventStore(), today=plan.data_date)
    all_alerts: list = []
    states = [capture(pg, "Start: the baseline plan (nothing done yet)", "", [], all_alerts)]

    reports = []
    for path in (HISTORY, DEMO_MESSAGES, MY_UPDATES):
        try:
            reports += read_reports(path)
        except ValueError as e:
            print(f"  ! {e} - file skipped")
    for r in reports:
        if r.task_id is not None and r.task_id not in pg.tasks:
            print(f"  ! {r.id}: unknown task_id '{r.task_id}', skipped")
            continue
        [(_, new)] = feed(pg, [r])
        all_alerts += new
        label = f"{r.id} · {r.task_id or '—'} {r.kind.value}"
        states.append(capture(pg, label, r.text, new, all_alerts))

    return {"pos": layout(pg), "edges": [[u, v, d["lag"]] for u, v, d in pg.g.edges(data=True)],
            "states": states, "box": [BOX_W, BOX_H], "height": TOP + 10 * ROW_H}


# ---------------------------------------------------------------------------- the page

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>KraftSync graph</title>
<style>
body{font-family:system-ui,sans-serif;margin:24px auto;color:#222;max-width:760px;padding:0 12px}
button{font:inherit;padding:5px 12px;border:1px solid #bbb;background:#fff;border-radius:6px;cursor:pointer}
.bar{display:flex;gap:10px;align-items:center;margin:6px 0}
.msg{background:#f4f4f1;border-radius:8px;padding:8px 12px;margin:8px 0;font-size:15px;min-height:20px}
.alert{border-radius:6px;padding:7px 10px;margin:5px 0;font-size:14px;opacity:.55}
.alert.new{opacity:1;font-weight:500}
.critical{background:#fdecec;color:#7a1f1f}.warning{background:#fff4e0;color:#7a4a00}.info{background:#eef3fb;color:#1f3d7a}
.legend span{display:inline-block;margin-right:12px;font-size:13px}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;margin-right:4px}
#watch{font-size:12px;color:#888}
</style></head><body>
<div class="bar"><button onclick="go(cur-1)">◀ Prev</button><input id="slider" type="range" min="0" style="flex:1"
 oninput="go(+this.value)"><button onclick="go(cur+1)">Next ▶</button></div>
<div class="bar"><b id="title"></b><span id="watch"></span></div>
<div class="msg" id="text"></div>
<div style="font-size:15px;margin:6px 0">Today: <b id="today"></b> · Handover: <b id="handover"></b></div>
<div class="legend"><span><i class="sw" style="background:#cfe8c4"></i>done</span><span><i class="sw" style="background:#cfe0f5"></i>in progress</span><span><i class="sw" style="background:#ecebe6"></i>not started</span><span><i class="sw" style="background:#f6c9c9"></i>blocked</span><span><i class="sw" style="border:2px solid #d33"></i>alert</span><span>thick = critical path</span></div>
<svg id="g" width="100%" viewBox="0 0 680 __H__"></svg>
<div id="alerts"></div>
<script>
const D = __DATA__, WATCH = __WATCH__;
const FILL = {done:"#cfe8c4", in_progress:"#cfe0f5", not_started:"#ecebe6", blocked:"#f6c9c9", cancelled:"#eee"};
const [BW, BH] = D.box, last = D.states.length - 1;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const saved = parseInt(location.hash.slice(1));
let cur = Number.isInteger(saved) && saved < last ? saved : last;   // newest step unless you went back
function go(i){
  cur = Math.max(0, Math.min(last, i));
  history.replaceState(null, "", cur === last ? "#" : "#" + cur);
  render();
}
function render(){
  const s = D.states[cur];
  document.getElementById("slider").max = last; document.getElementById("slider").value = cur;
  document.getElementById("title").textContent = `Step ${cur}/${last} · ${s.title}`;
  document.getElementById("text").textContent = s.text ? "“" + s.text + "”" : "(no message yet: this is the plan)";
  document.getElementById("today").textContent = s.today;
  document.getElementById("handover").textContent = s.handover;
  let svg = '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M1 1L9 5L1 9" fill="none" stroke="#888" stroke-width="1.5"/></marker></defs>';
  for (const [u, v, lag] of D.edges){
    const [x1, y1] = D.pos[u], [x2, y2] = D.pos[v];
    const crit = s.nodes[u].critical && s.nodes[v].critical;
    svg += `<line x1="${x1+BW/2}" y1="${y1+BH}" x2="${x2+BW/2}" y2="${y2-2}" stroke="${crit ? '#555' : '#bbb'}" stroke-width="${crit ? 2.2 : 0.8}" ${lag>0 ? 'stroke-dasharray="4 3"' : ''} marker-end="url(#ah)"/>`;
  }
  for (const [id, [x, y]] of Object.entries(D.pos)){
    const n = s.nodes[id], al = n.alerts.length > 0;
    const slip = n.slip > 0 ? ` <tspan fill="#b45309">+${n.slip}d</tspan>` : "";
    svg += `<g><title>${esc(n.name)}\\n${n.status} · ${n.start} → ${n.finish}${al ? "\\n" + n.alerts.join(", ") : ""}</title>
      <rect x="${x}" y="${y}" width="${BW}" height="${BH}" rx="5" fill="${FILL[n.status]}" stroke="${al ? '#d33' : '#999'}" stroke-width="${al ? 2.5 : 0.6}"/>
      <text x="${x+BW/2}" y="${y+16}" text-anchor="middle" font-size="12" font-weight="600" fill="#222">${id}</text>
      <text x="${x+BW/2}" y="${y+31}" text-anchor="middle" font-size="11" fill="#555">${n.finish.slice(5)}${slip}</text></g>`;
  }
  document.getElementById("g").innerHTML = svg;
  document.getElementById("alerts").innerHTML = s.alerts.map(a =>
    `<div class="alert ${a.sev}${a.new ? ' new' : ''}">${a.new ? 'NEW · ' : ''}[${a.sev}] ${esc(a.text)}</div>`).join("");
}
if (WATCH) {   // the script rewrites this file when a message file changes; reload to pick it up
  document.getElementById("watch").textContent = "· live";
  setTimeout(() => location.reload(), 2000);
}
render();
</script>
</body></html>"""


def write_page(watch: bool) -> int:
    data = build()
    html = (PAGE.replace("__DATA__", json.dumps(data)).replace("__H__", str(data["height"]))
            .replace("__WATCH__", "true" if watch else "false"))
    OUT.write_text(html, encoding="utf-8")
    return len(data["states"]) - 1


def mtimes() -> tuple:
    return tuple(p.stat().st_mtime if p.exists() else 0 for p in (PLAN, HISTORY, DEMO_MESSAGES, MY_UPDATES))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles (cp1251/cp1252)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", action="store_true", help="rebuild whenever a message file changes")
    args = ap.parse_args()

    n = write_page(args.watch)
    print(f"Wrote {OUT.resolve()} ({n} messages applied)")
    webbrowser.open(OUT.resolve().as_uri())
    if args.watch:
        print(f"Watching {DEMO_MESSAGES} and {MY_UPDATES} - add a line and save. Ctrl+C to stop.")
        seen = mtimes()
        try:
            while True:
                time.sleep(1)
                if mtimes() != seen:
                    seen = mtimes()
                    n = write_page(True)
                    print(f"  updated ({n} messages applied) at {time.strftime('%H:%M:%S')}")
        except KeyboardInterrupt:
            write_page(False)   # leave a static page behind
            print("stopped")