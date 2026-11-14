"""Primavera P6 XER -> ProjectPlan. Standard library only: no MPXJ, no Java.

XER is tab-separated text:  ERMHDR … / %T <table> / %F <fields…> / %R <values…> / … / %E

What P6 gives us, and how it maps:

    PROJECT                    -> project_id (proj_short_name), data_date (plan_start_date)
    PROJWBS                    -> location tree (wbs_short_name = id, wbs_name = name); the project
                                  root node (proj_node_flag=Y) is the project itself, its name = plan name
    TASK                       -> Task: task_code = id, task_name = name, WBS = location,
                                  target_start/end (fallback early_start/end) = planned dates,
                                  duration = finish - start + 1 CALENDAR days (as for MS Project)
        task_type TT_Mile / TT_FinMile -> milestone (a finish milestone at end of day -> next morning)
        task_type TT_LOE / TT_WBS      -> skipped (level of effort / WBS summary), links to them dropped
    TASKPRED                   -> Dependency: pred_type PR_FS/SS/FF/SF, lag_hr_cnt / hours per day of
                                  the successor's calendar (CALENDAR.day_hr_cnt, default 8)
    RSRC + TASKRSRC            -> responsible Person: first labour/equipment resource
                                  (rsrc_short_name = id); material is ignored; none -> P-UNASSIGNED
    activity codes (ACTVTYPE/ACTVCODE/TASKACTV) and user fields (UDFTYPE/UDFVALUE) on TASK,
    matched by their name, case-insensitive:
        Ort / Location             -> location id (overrides the WBS, e.g. a room inside a unit)
        Gewerk / Trade             -> trade
        LV-Position / BoQ          -> boq_ref
        Haltepunkt / Hold point    -> hold_point (fallback: inspection by name, e.g. "Abnahme")
        KraftSync-ID / ID          -> task id (fallback: task_code)

Progress (status, actual dates, % complete) is NOT imported: ingestion produces a baseline.
Several projects in one file: pass project_id = the proj_short_name to pick one.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from kraftsync.contracts import (
    Dependency,
    Location,
    LocationKind,
    Person,
    ProjectPlan,
    Role,
    Task,
    TaskKind,
)
from kraftsync.ingestion.csv_loader import (
    IngestionError,
    _build,
    _rows,
    reference_from_rows,
)
from kraftsync.ingestion.validate import UNASSIGNED

LINK = {"PR_FS": "FS", "PR_SS": "SS", "PR_FF": "FF", "PR_SF": "SF"}
SKIP_TYPES = {"TT_LOE", "TT_WBS"}
MILESTONES = {"TT_Mile", "TT_FinMile"}
WORK_RESOURCES = {"RT_Labor", "RT_Equip", ""}
ALIASES = {
    "id": {"kraftsync-id", "kraftsync id", "id", "code"},
    "location": {"ort", "location", "bauabschnitt", "area", "bereich"},
    "trade": {"gewerk", "trade", "discipline"},
    "boq": {"lv-position", "lv-pos", "lv position", "boq", "boq_ref"},
    "hold": {"haltepunkt", "hold point", "hold_point", "holdpoint"},
}
INSPECTION = re.compile(r"abnahme|inspection|prüfung|pruefung|begehung", re.IGNORECASE)
DELIVERY = re.compile(r"lieferung|delivery|anlieferung", re.IGNORECASE)
DEPTH_KIND = (LocationKind.building, LocationKind.floor, LocationKind.unit, LocationKind.room)
DAY_END = "17:00"
YES = ("y", "yes", "ja", "true", "1", "x")


def _key(label: str) -> str | None:
    label = label.strip().lower()
    return next((k for k, names in ALIASES.items() if label in names), None)


def parse_xer(text: str, src: str = "xer") -> dict[str, list[dict[str, str]]]:
    """{table: [ {field: value} ]}. Raises IngestionError on a malformed file."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("ERMHDR"):
        raise IngestionError(f"{src}: not a Primavera XER file (first line must start with ERMHDR)")
    tables: dict[str, list[dict[str, str]]] = {}
    table, fields = None, []
    for n, line in enumerate(lines[1:], 2):
        parts = line.split("\t")
        tag = parts[0]
        if tag == "%T":
            table, fields = parts[1].strip(), []
            tables.setdefault(table, [])
        elif tag == "%F":
            fields = [f.strip() for f in parts[1:]]
        elif tag == "%R":
            if table is None or not fields:
                raise IngestionError(f"{src}:{n}: data row before a %T/%F header")
            values = parts[1:] + [""] * (len(fields) - len(parts) + 1)
            tables[table].append(dict(zip(fields, (v.strip() for v in values))))
        elif tag == "%E":
            break
    return tables


def _day(s: str) -> date:
    return date.fromisoformat(s.strip()[:10])


def load_xer(path: str | Path, project_id: str | None = None, name: str | None = None,
             data_date: date | None = None, reference: str | Path | None = None) -> ProjectPlan:
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"{path}: file not found")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")                 # classic P6 exports use the Windows code page
    t = parse_xer(text, str(path))

    # ---- which project
    projects = t.get("PROJECT", [])
    if not projects:
        raise IngestionError(f"{path}: no PROJECT table")
    if len(projects) > 1:
        chosen = [p for p in projects if p.get("proj_short_name") == project_id]
        if not chosen:
            ids = ", ".join(p.get("proj_short_name", "?") for p in projects)
            raise IngestionError(f"{path}: {len(projects)} projects in this file ({ids}); "
                                 f"pass --project-id to choose one")
        proj = chosen[0]
    else:
        proj = projects[0]
    pid = proj["proj_id"]

    def rows(table: str) -> list[dict[str, str]]:
        return [r for r in t.get(table, []) if r.get("proj_id", pid) == pid]

    # ---- calendars: hours per day
    hours_per_day = {c["clndr_id"]: float(c.get("day_hr_cnt") or 8) or 8.0 for c in t.get("CALENDAR", [])}

    # ---- custom values per task: activity codes + user-defined fields
    custom: dict[str, dict[str, str]] = {}
    code_type = {a["actv_code_type_id"]: _key(a.get("actv_code_type", "")) for a in t.get("ACTVTYPE", [])}
    code_value = {c["actv_code_id"]: c.get("short_name") or c.get("actv_code_name", "") for c in t.get("ACTVCODE", [])}
    code_name = {c["actv_code_id"]: c.get("actv_code_name") or c.get("short_name", "") for c in t.get("ACTVCODE", [])}
    for ta in t.get("TASKACTV", []):
        k = code_type.get(ta.get("actv_code_type_id"))
        if k:
            custom.setdefault(ta["task_id"], {})[k] = code_value.get(ta.get("actv_code_id"), "")
            if k == "location":                       # the code's description names the place ("Top 5 Bad")
                custom[ta["task_id"]]["location_name"] = code_name.get(ta.get("actv_code_id"), "")
    udf_type = {u["udf_type_id"]: _key(u.get("udf_type_label", "")) for u in t.get("UDFTYPE", [])
                if u.get("table_name", "TASK") == "TASK"}
    for uv in t.get("UDFVALUE", []):
        k = udf_type.get(uv.get("udf_type_id"))
        if k:
            val = uv.get("udf_text") or uv.get("udf_code_id") or uv.get("udf_number") or ""
            custom.setdefault(uv["fk_id"], {})[k] = val

    # ---- reference data (optional)
    ref_persons: dict[str, Person] = {}
    ref_locations: list[Location] = []
    if reference:
        ref = Path(reference)
        if (ref / "persons.csv").exists():
            ref_persons, _ = reference_from_rows(_rows(ref / "persons.csv"), None, str(ref / "persons.csv"))
        if (ref / "locations.csv").exists():
            _, ref_locations = reference_from_rows(None, _rows(ref / "locations.csv"), str(ref / "locations.csv"))

    # ---- WBS -> location tree
    wbs = {w["wbs_id"]: w for w in rows("PROJWBS")}
    root = next((w for w in wbs.values() if w.get("proj_node_flag") == "Y"), None)
    short_count: dict[str, int] = {}
    for w in wbs.values():
        short_count[w.get("wbs_short_name", "")] = short_count.get(w.get("wbs_short_name", ""), 0) + 1

    def wbs_chain(wbs_id: str | None) -> list[dict[str, str]]:
        chain, seen = [], set()
        while wbs_id in wbs and wbs_id not in seen and wbs[wbs_id] is not root:
            seen.add(wbs_id)
            chain.append(wbs[wbs_id])
            wbs_id = wbs[wbs_id].get("parent_wbs_id")
        return list(reversed(chain))

    def wbs_loc_id(chain: list[dict[str, str]], i: int) -> str:
        short = chain[i].get("wbs_short_name") or f"WBS{chain[i]['wbs_id']}"
        return short if short_count.get(short, 0) <= 1 else ".".join(c.get("wbs_short_name", "") for c in chain[:i + 1])

    loc_nodes: dict[str, tuple[str, str | None, int]] = {}      # id -> (name, parent, depth)

    # ---- resources
    resources: dict[str, Person] = {}
    for r in t.get("RSRC", []):
        if r.get("rsrc_type", "") not in WORK_RESOURCES:
            continue
        rid = r.get("rsrc_short_name") or f"R{r['rsrc_id']}"
        resources[r["rsrc_id"]] = ref_persons.get(rid) or Person(
            id=rid, name=r.get("rsrc_name") or rid, role=Role.foreman, email=r.get("email_addr") or None)
    first_resource: dict[str, str] = {}
    for tr in rows("TASKRSRC"):
        if tr.get("rsrc_id") in resources:
            first_resource.setdefault(tr["task_id"], tr["rsrc_id"])

    # ---- tasks
    task_rows = rows("TASK")
    skipped = {r["task_id"] for r in task_rows if r.get("task_type") in SKIP_TYPES}
    id_of: dict[str, str] = {}
    for r in task_rows:
        if r["task_id"] not in skipped:
            id_of[r["task_id"]] = custom.get(r["task_id"], {}).get("id") or r.get("task_code") or f"T{r['task_id']}"
    if not id_of:
        raise IngestionError(f"{path}: project {proj.get('proj_short_name')} has no activities")

    tasks: list[Task] = []
    persons: dict[str, Person] = {}
    succ_calendar: dict[str, str] = {}
    for r in task_rows:
        if r["task_id"] in skipped:
            continue
        c = custom.get(r["task_id"], {})
        tid, tname = id_of[r["task_id"]], r.get("task_name", "")
        start_s = r.get("target_start_date") or r.get("early_start_date") or ""
        end_s = r.get("target_end_date") or r.get("early_end_date") or start_s
        if not start_s:
            raise IngestionError(f"{path}: activity {tid} has no start date")
        start, finish = _day(start_s), _day(end_s)
        ttype = r.get("task_type", "TT_Task")
        if ttype in MILESTONES:
            if ttype == "TT_FinMile":
                start = finish
                if end_s.strip()[11:16] >= DAY_END:
                    start = date.fromordinal(start.toordinal() + 1)
            kind, duration, finish = TaskKind.milestone, 0.0, start
        else:
            kind = (TaskKind.inspection if INSPECTION.search(tname)
                    else TaskKind.delivery if DELIVERY.search(tname) else TaskKind.work)
            duration = float((finish - start).days + 1)
        hold = c["hold"].strip().lower() in YES if "hold" in c else kind == TaskKind.inspection

        rsrc = first_resource.get(r["task_id"])
        person = resources[rsrc] if rsrc else (persons.get(UNASSIGNED) or
                                               Person(id=UNASSIGNED, name="(nicht zugewiesen)", role=Role.foreman))
        persons.setdefault(person.id, person)

        chain = wbs_chain(r.get("wbs_id"))
        parent = None
        for i, w in enumerate(chain):
            lid = wbs_loc_id(chain, i)
            loc_nodes.setdefault(lid, (w.get("wbs_name") or lid, parent, i))
            parent = lid
        location_id = c.get("location") or parent or "L-PROJECT"
        if location_id not in loc_nodes:
            depth = loc_nodes[parent][2] + 1 if parent in loc_nodes else 0
            loc_name = c.get("location_name") or (location_id if parent else "Projekt")
            loc_nodes[location_id] = (loc_name, parent if parent != location_id else None, depth)

        succ_calendar[tid] = r.get("clndr_id", "")
        tasks.append(_build(Task, path, 0, {
            "id": tid, "name": tname, "kind": kind.value, "trade": c.get("trade") or None,
            "location_id": location_id, "responsible_person_id": person.id, "duration_days": duration,
            "planned_start": start, "planned_finish": finish, "hold_point": hold, "boq_ref": c.get("boq") or None,
        }))

    deps: list[Dependency] = []
    for p in rows("TASKPRED"):
        succ, pred = p.get("task_id"), p.get("pred_task_id")
        if succ in skipped or pred in skipped:
            continue                                   # links to level-of-effort / WBS summaries don't drive dates
        if succ not in id_of or pred not in id_of:
            raise IngestionError(f"{path}: relationship {p.get('task_pred_id')} links activity {pred} -> {succ}, "
                                 f"which is not in project {proj.get('proj_short_name')}")
        hpd = hours_per_day.get(succ_calendar.get(id_of[succ], ""), 8.0)
        deps.append(Dependency(id=f"D{len(deps) + 1:03d}", predecessor_id=id_of[pred], successor_id=id_of[succ],
                               type=LINK.get(p.get("pred_type", "PR_FS"), "FS"),
                               lag_days=round(float(p.get("lag_hr_cnt") or 0) / hpd, 2)))

    locations = ref_locations or [Location(id=lid, name=n, kind=DEPTH_KIND[min(d, len(DEPTH_KIND) - 1)], parent_id=par)
                                  for lid, (n, par, d) in loc_nodes.items()]
    start_date = proj.get("plan_start_date", "")
    return ProjectPlan(
        project_id=project_id or proj.get("proj_short_name") or path.stem,
        name=name or (root.get("wbs_name") if root else None) or proj.get("proj_short_name") or path.stem,
        data_date=data_date or (_day(start_date) if start_date else min(x.planned_start for x in tasks)),
        persons=list(persons.values()), locations=locations, tasks=tasks, dependencies=deps,
    )
