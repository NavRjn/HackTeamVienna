"""MS Project XML (MSPDI, "Save as XML") -> ProjectPlan. Standard library only: no MPXJ, no Java.

What MS Project gives us, and how it maps:

    <Task>  (Summary=0, IsNull=0, OutlineLevel>0)  -> Task
        Start / Finish (date part)                 -> planned_start / planned_finish
        duration_days = finish - start + 1         (CALENDAR days, so our CPM reproduces the file's dates
                                                    even when MS Project used a Mon-Fri calendar)
        Milestone=1                                -> kind milestone, duration 0
        PredecessorLink                            -> Dependency (Type 0=FF 1=FS 2=SF 3=SS, LinkLag in
                                                    tenths of a minute, unit from LagFormat)
    <Task Summary=1>                               -> not a task; its outline gives the location tree
    <Resource> + <Assignment>                      -> responsible Person (first assigned resource)
    custom fields (<ExtendedAttributes>), matched by their alias, case-insensitive:
        "KraftSync-ID" / "ID" / "Code"             -> task id         (fallback: T<UID>)
        "Ort" / "Location"                         -> location id     (fallback: enclosing summary)
        "Gewerk" / "Trade"                         -> trade
        "LV-Position" / "BoQ"                      -> boq_ref
        "Haltepunkt" / "Hold point" (Flag)         -> hold_point      (fallback: inspection by name)
    Resource Initials                              -> person id       (fallback: R<UID>)

Progress in the file (% complete, actual dates) is NOT imported: ingestion produces a baseline.
Tasks without an assigned resource get the placeholder person P-UNASSIGNED (validate() warns).
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
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
from kraftsync.ingestion.csv_loader import IngestionError, _build, _clean, _rows
from kraftsync.ingestion.validate import UNASSIGNED

LINK_TYPE = {"0": "FF", "1": "FS", "2": "SF", "3": "SS"}
# LagFormat -> (minutes per unit is irrelevant; LinkLag is always tenths of minutes) elapsed?
ELAPSED_FORMATS = {4, 6, 8, 10, 12, 36, 38, 40, 42, 44}
PERCENT_FORMATS = {19, 20, 51, 52}
ALIASES = {
    "id": {"kraftsync-id", "kraftsync id", "id", "code", "vorgangs-id", "task id"},
    "location": {"ort", "location", "bauabschnitt"},
    "trade": {"gewerk", "trade"},
    "boq": {"lv-position", "lv-pos", "lv position", "boq", "boq_ref"},
    "hold": {"haltepunkt", "hold point", "hold_point", "holdpoint"},
}
INSPECTION = re.compile(r"abnahme|inspection|prüfung|pruefung|begehung", re.IGNORECASE)
DELIVERY = re.compile(r"lieferung|delivery|anlieferung", re.IGNORECASE)
DEPTH_KIND = (LocationKind.building, LocationKind.floor, LocationKind.unit, LocationKind.room)


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _txt(el: ET.Element | None, tag: str, default: str = "") -> str:
    found = el.find(tag) if el is not None else None
    return (found.text or "").strip() if found is not None and found.text else default


def _day(s: str) -> date:
    return date.fromisoformat(s[:10])


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "L-" + (re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").upper() or "X")


def load_mspdi(path: str | Path, project_id: str | None = None, name: str | None = None,
               data_date: date | None = None, reference: str | Path | None = None) -> ProjectPlan:
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"{path}: file not found")
    text = path.read_text(encoding="utf-8-sig")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise IngestionError(f"{path}: DOCTYPE/ENTITY declarations are not accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise IngestionError(f"{path}: not valid XML ({e})") from e
    _strip_ns(root)
    if root.tag != "Project":
        raise IngestionError(f"{path}: not an MS Project XML file (root is <{root.tag}>)")

    minutes_per_day = float(_txt(root, "MinutesPerDay", "480") or 480)
    day_end = _txt(root, "DefaultFinishTime", "17:00:00")[:5]

    # custom field FieldID -> our key, from the alias definitions
    field_key: dict[str, str] = {}
    for ea in root.iterfind("ExtendedAttributes/ExtendedAttribute"):
        alias = (_txt(ea, "Alias") or _txt(ea, "FieldName")).lower()
        for key, names in ALIASES.items():
            if alias in names:
                field_key[_txt(ea, "FieldID")] = key

    def custom(el: ET.Element) -> dict[str, str]:
        out = {}
        for ea in el.iterfind("ExtendedAttribute"):
            key = field_key.get(_txt(ea, "FieldID"))
            if key:
                out[key] = _txt(ea, "Value")
        return out

    # ---- reference data (optional)
    ref_persons: dict[str, Person] = {}
    ref_locations: list[Location] = []
    if reference:
        ref = Path(reference)
        if (ref / "persons.csv").exists():
            ref_persons = {p.id: p for p in (_build(Person, ref / "persons.csv", n, _clean(r))
                                             for n, r in _rows(ref / "persons.csv"))}
        if (ref / "locations.csv").exists():
            ref_locations = [_build(Location, ref / "locations.csv", n, _clean(r))
                             for n, r in _rows(ref / "locations.csv")]

    # ---- resources and assignments
    resources: dict[str, Person] = {}
    for r in root.iterfind("Resources/Resource"):
        uid, rname = _txt(r, "UID"), _txt(r, "Name")
        if not rname or _txt(r, "IsNull") == "1" or _txt(r, "Type", "1") != "1":   # 1 = work resource
            continue
        pid = _txt(r, "Initials") or f"R{uid}"
        resources[uid] = ref_persons.get(pid) or Person(
            id=pid, name=rname, role=Role.foreman, company=_txt(r, "Group") or None,
            email=_txt(r, "EmailAddress") or None)
    first_resource: dict[str, str] = {}
    for a in root.iterfind("Assignments/Assignment"):
        t_uid, r_uid = _txt(a, "TaskUID"), _txt(a, "ResourceUID")
        if r_uid in resources:
            first_resource.setdefault(t_uid, r_uid)

    # ---- tasks: walk in file order, keep the summary (outline) stack for locations
    uid_to_id: dict[str, str] = {}
    raw: list[tuple[ET.Element, dict[str, str], list[tuple[str, str]]]] = []   # (task, custom, summary path)
    summary_stack: list[tuple[int, str, str]] = []                            # (level, name, location id)
    loc_nodes: dict[str, tuple[str, str | None, int]] = {}                    # id -> (name, parent, depth)
    for t in root.iterfind("Tasks/Task"):
        level = int(_txt(t, "OutlineLevel", "1") or 1)
        if _txt(t, "IsNull") == "1" or level == 0 or not _txt(t, "Name"):
            continue
        c = custom(t)
        while summary_stack and summary_stack[-1][0] >= level:
            summary_stack.pop()
        if _txt(t, "Summary") == "1":
            lid = c.get("location") or _slug(_txt(t, "Name"))
            parent = summary_stack[-1][2] if summary_stack else None
            loc_nodes.setdefault(lid, (_txt(t, "Name"), parent, len(summary_stack)))
            summary_stack.append((level, _txt(t, "Name"), lid))
            continue
        tid = c.get("id") or f"T{_txt(t, 'UID')}"
        uid_to_id[_txt(t, "UID")] = tid
        raw.append((t, c, [(n, lid) for _, n, lid in summary_stack]))

    if not raw:
        raise IngestionError(f"{path}: no tasks (only summary or empty rows)")

    tasks: list[Task] = []
    deps: list[Dependency] = []
    persons: dict[str, Person] = {}
    for t, c, spath in raw:
        uid, tname, tid = _txt(t, "UID"), _txt(t, "Name"), uid_to_id[_txt(t, "UID")]
        start, finish = _day(_txt(t, "Start")), _day(_txt(t, "Finish") or _txt(t, "Start"))
        milestone = _txt(t, "Milestone") == "1"
        if milestone:
            # MS Project puts a milestone at the END of the day its predecessor finishes (17:00).
            # In calendar days that is "ready the next morning"; otherwise our CPM shows a false +1 d.
            if _txt(t, "Start")[11:16] >= day_end:
                start = date.fromordinal(start.toordinal() + 1)
            kind, duration, finish = TaskKind.milestone, 0.0, start
        else:
            kind = (TaskKind.inspection if INSPECTION.search(tname)
                    else TaskKind.delivery if DELIVERY.search(tname) else TaskKind.work)
            duration = float((finish - start).days + 1)
        hold = c["hold"] in ("1", "true", "yes", "ja") if "hold" in c else kind == TaskKind.inspection

        r_uid = first_resource.get(uid)
        person = resources[r_uid] if r_uid else None
        if person is None:
            person = persons.get(UNASSIGNED) or Person(id=UNASSIGNED, name="(nicht zugewiesen)", role=Role.foreman)
        persons.setdefault(person.id, person)

        location_id = c.get("location") or (spath[-1][1] if spath else "L-PROJECT")
        if location_id not in loc_nodes:
            parent = spath[-1][1] if spath and spath[-1][1] != location_id else None
            depth = loc_nodes[parent][2] + 1 if parent in loc_nodes else 0
            loc_nodes[location_id] = (location_id if parent else "Projekt", parent, depth)

        tasks.append(_build(Task, path, 0, {
            "id": tid, "name": tname, "kind": kind.value, "trade": c.get("trade") or None,
            "location_id": location_id, "responsible_person_id": person.id, "duration_days": duration,
            "planned_start": start, "planned_finish": finish, "hold_point": hold,
            "boq_ref": c.get("boq") or None,
        }))
        for link in t.iterfind("PredecessorLink"):
            pred_uid = _txt(link, "PredecessorUID")
            if pred_uid not in uid_to_id:
                raise IngestionError(f"{path}: task {tid} links to unknown or summary task UID {pred_uid}")
            deps.append(Dependency(
                id=f"D{len(deps) + 1:03d}", predecessor_id=uid_to_id[pred_uid], successor_id=tid,
                type=LINK_TYPE.get(_txt(link, "Type", "1"), "FS"),
                lag_days=_lag_days(link, minutes_per_day, raw, uid_to_id, pred_uid)))

    if ref_locations:
        locations = ref_locations
    else:
        locations = [Location(id=lid, name=n, kind=DEPTH_KIND[min(d, len(DEPTH_KIND) - 1)], parent_id=p)
                     for lid, (n, p, d) in loc_nodes.items()]

    start_date = _txt(root, "StartDate")
    return ProjectPlan(
        project_id=project_id or path.stem,
        name=name or _txt(root, "Title") or _txt(root, "Name") or path.stem,
        data_date=data_date or (_day(start_date) if start_date else min(t.planned_start for t in tasks)),
        persons=list(persons.values()), locations=locations, tasks=tasks, dependencies=deps,
    )


def _lag_days(link: ET.Element, minutes_per_day: float, raw, uid_to_id, pred_uid: str) -> float:
    """LinkLag is tenths of a minute. Elapsed formats count 24 h days; working formats count
    MinutesPerDay (default 8 h), which we then treat as calendar days. Percent lags use the
    predecessor's duration."""
    lag = float(_txt(link, "LinkLag", "0") or 0)
    fmt = int(_txt(link, "LagFormat", "7") or 7)
    if fmt in PERCENT_FORMATS:          # LinkLag holds percent * 10
        pred = next(t for t, _, _ in raw if _txt(t, "UID") == pred_uid)
        days = (_day(_txt(pred, "Finish")) - _day(_txt(pred, "Start"))).days + 1
        return round(days * lag / 1000, 2)
    minutes = lag / 10
    return round(minutes / (1440 if fmt in ELAPSED_FORMATS else minutes_per_day), 2)
