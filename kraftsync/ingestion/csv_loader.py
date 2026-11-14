"""CSV -> ProjectPlan. Deterministic, no network, no LLM.

Two shapes:

1. Folder with the four tables (same columns as data/demo/*.csv):
       persons.csv  locations.csv  tasks.csv  dependencies.csv
   -> exactly the plan they describe.

2. One flat schedule export (data/demo/sources/schedule_export.csv): one row per task with
   predecessors in one column ("T5-FRAME; T5-SCREED FS+14") and person/location written out.
   German headers and values are understood too (Vorgang, Beginn, Ende, Dauer, Vorgänger,
   Gewerk, Ort, Haltepunkt …; "Abnahme", "3 d", "05.11.2026").
   It has less information than a full plan, so missing parts are filled deterministically:
     - people: id, name, company, phone from the rows; role "foreman"; enriched from a
       reference persons list if given
     - locations: leaf ids from the rows, parents from the "A > B > C" path (ids reused where
       known, otherwise "L-<SLUG>"), kind from depth (building/floor/unit/room); or taken from a
       reference locations list if given
     - dependencies: ids D001… in file order, no reasons
     - status: everything not_started (a baseline)

The table logic works on plain rows (`Rows`), so the Excel loader reuses it unchanged.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from kraftsync.contracts import (
    Dependency,
    Location,
    LocationKind,
    Person,
    ProjectPlan,
    Role,
    Task,
)

Rows = list[tuple[int, dict[str, str]]]        # (line or spreadsheet row number, {column: text})

TABLES = ("persons.csv", "locations.csv", "tasks.csv", "dependencies.csv")
FLAT_REQUIRED = {"id", "name", "kind", "location_id", "responsible_person_id", "duration_days",
                 "planned_start", "planned_finish", "predecessors"}
REF = re.compile(r"(?P<id>[^\s;]+)(?:\s+(?P<type>FS|SS|FF|SF)\s*(?P<lag>[+-]\s*\d+(?:\.\d+)?)?)?")
DEPTH_KIND = (LocationKind.building, LocationKind.floor, LocationKind.unit, LocationKind.room)

# flat-schedule column aliases (lower-case) -> canonical column
HEADER_ALIASES = {
    "id": ("id", "vorgangs-id", "vorgang-id", "nr", "nr.", "code", "kraftsync-id"),
    "name": ("name", "vorgang", "vorgangsname", "bezeichnung", "task"),
    "kind": ("kind", "art", "typ", "type"),
    "trade": ("trade", "gewerk"),
    "location_id": ("location_id", "ort", "ort-id", "bauabschnitt"),
    "location": ("location", "ortspfad", "pfad", "location path"),
    "responsible_person_id": ("responsible_person_id", "verantwortlich-id", "verantw.-id", "person-id"),
    "responsible": ("responsible", "verantwortlich", "verantw."),
    "company": ("company", "firma"),
    "phone": ("phone", "telefon"),
    "duration_days": ("duration_days", "dauer", "dauer (tage)", "dauer [d]", "duration"),
    "planned_start": ("planned_start", "beginn", "anfang", "start"),
    "planned_finish": ("planned_finish", "ende", "fertig", "finish"),
    "predecessors": ("predecessors", "vorgänger", "vorgaenger"),
    "hold_point": ("hold_point", "haltepunkt", "hp"),
    "boq_ref": ("boq_ref", "lv-position", "lv-pos.", "lv-pos", "lv"),
}
_ALIAS = {a: canon for canon, names in HEADER_ALIASES.items() for a in names}
KIND_ALIASES = {"arbeit": "work", "abnahme": "inspection", "prüfung": "inspection", "lieferung": "delivery",
                "meilenstein": "milestone"}
YES = ("yes", "ja", "true", "1", "x", "wahr")
GERMAN_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})")


class IngestionError(ValueError):
    """Unreadable input. The message names the file and line."""


# ---------------------------------------------------------------------------- reading

def _rows(path: Path) -> Rows:
    if not path.exists():
        raise IngestionError(f"{path}: file not found")
    text = path.read_text(encoding="utf-8-sig")            # tolerate Excel's BOM
    first = text.split("\n", 1)[0]
    delim = ";" if first.count(";") > first.count(",") else ","   # German Excel saves with ';'
    reader = csv.DictReader(text.splitlines(), delimiter=delim)
    if not reader.fieldnames:
        raise IngestionError(f"{path}: empty file")
    return [(n, {k.strip(): (v or "").strip() for k, v in row.items() if k}) for n, row in enumerate(reader, 2)]


def canonical_header(h: str) -> str:
    return _ALIAS.get(h.strip().lower(), h.strip())


def _clean(row: dict[str, str], lists: tuple[str, ...] = ()) -> dict[str, Any]:
    """'' -> None (pydantic fills defaults), list columns split on ';'."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k in lists:
            out[k] = [x.strip() for x in v.split(";") if x.strip()]
        elif v != "":
            out[k] = v
    return out


def _build(model, src: Path | str, line: int, data: dict[str, Any]):
    try:
        return model.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        field = ".".join(str(x) for x in first["loc"])
        raise IngestionError(f"{src}:{line}: column {field!r}: {first['msg']}") from e


def _date(s: str) -> str:
    """ISO stays ISO; '05.11.2026' / '5.11.26' -> '2026-11-05'; anything else is left for pydantic to reject."""
    m = GERMAN_DATE.fullmatch(s.strip())
    if not m:
        return s
    d, mth, y = m.groups()
    return f"{int(y) + 2000 if len(y) == 2 else int(y):04d}-{int(mth):02d}-{int(d):02d}"


def _days(s: str) -> str:
    """'3', '3 d', '3 Tage', '3,5' -> '3' / '3.5'."""
    return re.sub(r"\s*(d|t|tage?|days?)\.?$", "", s.strip(), flags=re.IGNORECASE).replace(",", ".")


# ---------------------------------------------------------------------------- 1. four tables

def plan_from_tables(tables: dict[str, Rows], src: dict[str, str], project_id: str | None, name: str | None,
                     data_date: date | None, default_id: str) -> ProjectPlan:
    """tables: persons/locations/tasks/dependencies rows; src: table -> label used in error messages."""
    persons = [_build(Person, src["persons"], n, _clean(r)) for n, r in tables["persons"]]
    locations = [_build(Location, src["locations"], n, _clean(r)) for n, r in tables["locations"]]
    tasks = [_build(Task, src["tasks"], n, _clean(r, lists=("drawing_refs",))) for n, r in tables["tasks"]]
    deps = [_build(Dependency, src["dependencies"], n, _clean(r)) for n, r in tables["dependencies"]]
    return _plan(default_id, project_id, name, data_date, persons, locations, tasks, deps)


def load_csv_folder(folder: str | Path, project_id: str | None = None, name: str | None = None,
                    data_date: date | None = None) -> ProjectPlan:
    folder = Path(folder)
    names = ("persons", "locations", "tasks", "dependencies")
    tables = {n: _rows(folder / f"{n}.csv") for n in names}
    return plan_from_tables(tables, {n: str(folder / f"{n}.csv") for n in names}, project_id, name, data_date,
                            folder.stem)


def _plan(default_id: str, project_id, name, data_date, persons, locations, tasks, deps) -> ProjectPlan:
    if not tasks:
        raise IngestionError(f"{default_id}: no tasks")
    return ProjectPlan(
        project_id=project_id or default_id,
        name=name or project_id or default_id,
        data_date=data_date or min(t.planned_start for t in tasks),   # baseline: the project start
        persons=persons, locations=locations, tasks=tasks, dependencies=deps,
    )


# ---------------------------------------------------------------------------- 2. flat schedule

def parse_predecessors(cell: str, src: Path | str, line: int) -> list[tuple[str, str, float]]:
    """'T5-FRAME; T5-SCREED FS+14' -> [('T5-FRAME','FS',0.0), ('T5-SCREED','FS',14.0)]."""
    out = []
    for part in (p.strip() for p in cell.split(";")):
        if not part or part in ("-", "–"):
            continue
        m = REF.fullmatch(part)
        if not m:
            raise IngestionError(f"{src}:{line}: cannot read predecessor {part!r} "
                                 f"(expected 'ID' or 'ID FS+3' / 'ID SS-1')")
        out.append((m["id"], m["type"] or "FS", float((m["lag"] or "0").replace(" ", ""))))
    return out


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "L-" + (re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").upper() or "X")


def reference_from_rows(persons: Rows | None, locations: Rows | None,
                        src: str) -> tuple[dict[str, Person], list[Location]]:
    ref_p = {p.id: p for p in (_build(Person, src, n, _clean(r)) for n, r in persons or [])}
    ref_l = [_build(Location, src, n, _clean(r)) for n, r in locations or []]
    return ref_p, ref_l


def plan_from_flat_rows(rows: Rows, src: str, project_id: str | None, name: str | None, data_date: date | None,
                        default_id: str, ref_persons: dict[str, Person] | None = None,
                        ref_locations: list[Location] | None = None) -> ProjectPlan:
    rows = [(n, {canonical_header(k): v for k, v in r.items()}) for n, r in rows]
    rows = [(n, r) for n, r in rows if any(r.values())]    # skip blank rows
    if not rows:
        raise IngestionError(f"{src}: no tasks")
    missing = FLAT_REQUIRED - set(rows[0][1])
    if missing:
        raise IngestionError(f"{src}: missing column(s) {', '.join(sorted(missing))}")
    ref_persons = ref_persons or {}

    tasks: list[Task] = []
    persons: dict[str, Person] = {}
    deps: list[Dependency] = []
    paths: dict[str, list[str]] = {}                       # leaf location id -> path names
    for line, r in rows:
        kind = r["kind"].strip()
        tasks.append(_build(Task, src, line, _clean({
            "id": r["id"], "name": r["name"], "kind": KIND_ALIASES.get(kind.lower(), kind),
            "trade": "" if r.get("trade", "") in ("-", "–") else r.get("trade", ""),
            "location_id": r["location_id"], "responsible_person_id": r["responsible_person_id"],
            "duration_days": _days(r["duration_days"]), "planned_start": _date(r["planned_start"]),
            "planned_finish": _date(r["planned_finish"]), "boq_ref": r.get("boq_ref", ""),
        }) | {"hold_point": r.get("hold_point", "").strip().lower() in YES}))
        pid = r["responsible_person_id"]
        if pid not in persons:
            persons[pid] = ref_persons.get(pid) or _build(Person, src, line, _clean({
                "id": pid, "name": r.get("responsible") or pid, "role": Role.foreman.value,
                "company": r.get("company", ""), "phone": r.get("phone", ""),
                "trade": tasks[-1].trade or ""}))
        if r.get("location"):
            paths.setdefault(r["location_id"], [x.strip() for x in r["location"].split(">")])
        for pred, typ, lag in parse_predecessors(r["predecessors"], src, line):
            deps.append(_build(Dependency, src, line, {
                "id": f"D{len(deps) + 1:03d}", "predecessor_id": pred, "successor_id": r["id"],
                "type": typ, "lag_days": lag}))

    locations = ref_locations or _locations_from_paths(paths, {t.location_id for t in tasks})
    return _plan(default_id, project_id, name, data_date, list(persons.values()), locations, tasks, deps)


def load_flat_csv(path: str | Path, project_id: str | None = None, name: str | None = None,
                  data_date: date | None = None, reference: str | Path | None = None) -> ProjectPlan:
    """`reference`: optional folder with persons.csv / locations.csv to take full details from."""
    path = Path(path)
    ref_p: dict[str, Person] = {}
    ref_l: list[Location] = []
    if reference:
        ref = Path(reference)
        if (ref / "persons.csv").exists():
            ref_p, _ = reference_from_rows(_rows(ref / "persons.csv"), None, str(ref / "persons.csv"))
        if (ref / "locations.csv").exists():
            _, ref_l = reference_from_rows(None, _rows(ref / "locations.csv"), str(ref / "locations.csv"))
    return plan_from_flat_rows(_rows(path), str(path), project_id, name, data_date, path.stem, ref_p, ref_l)


def _locations_from_paths(paths: dict[str, list[str]], used: set[str]) -> list[Location]:
    """Rebuild the location tree from 'Building > Floor > Unit > Room' paths."""
    id_of: dict[tuple[str, ...], str] = {tuple(p): leaf for leaf, p in paths.items()}
    out: dict[str, Location] = {}
    for leaf in sorted(paths, key=lambda k: (len(paths[k]), k)):
        names = paths[leaf]
        parent = None
        for depth in range(len(names)):
            key = tuple(names[:depth + 1])
            lid = id_of.setdefault(key, _slug(names[depth]))
            if lid not in out:
                kind = DEPTH_KIND[min(depth, len(DEPTH_KIND) - 1)]
                out[lid] = Location(id=lid, name=names[depth], kind=kind, parent_id=parent)
            parent = lid
    for lid in sorted(used - set(out)):                    # rows without a path
        out[lid] = Location(id=lid, name=lid, kind=LocationKind.zone)
    return list(out.values())


# ---------------------------------------------------------------------------- dispatch

def is_csv_folder(path: Path) -> bool:
    return path.is_dir() and all((path / t).exists() for t in TABLES)
