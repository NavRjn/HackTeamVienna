"""Ingestion: CSV, Excel, MS Project XML and P6 XER -> ProjectPlan, validation report, CLI."""

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from kraftsync.contracts import Dependency, ProjectPlan, TaskStatus
from kraftsync.graph import EventStore, ProjectGraph
from kraftsync.ingestion import IngestionError, errors, validate
from kraftsync.ingestion import load_plan as _load_plan
from kraftsync.ingestion.__main__ import main as cli
from kraftsync.ingestion.csv_loader import parse_predecessors

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
FLAT = DEMO / "sources" / "schedule_export.csv"
META = {"project_id": "arsenal-f2", "name": "Wohnhaus Arsenal – Ausbau 2. OG"}


def load_plan(*args, **kw) -> ProjectPlan:
    """data/demo/persons.csv carries the Telegram ids typed in for this deployment (a runtime setting read by the
    service, not part of the plan), so they are left out when a loaded plan is compared with plan.json."""
    plan = _load_plan(*args, **kw)
    plan.persons = [p.model_copy(update={"telegram_chat_id": None}) for p in plan.persons]
    return plan


@pytest.fixture(scope="module")
def plan() -> ProjectPlan:
    return ProjectPlan.model_validate_json((DEMO / "plan.json").read_text(encoding="utf-8"))


def _deps(p: ProjectPlan) -> set:
    return {(d.predecessor_id, d.successor_id, d.type.value, d.lag_days) for d in p.dependencies}


# ---------------------------------------------------------------- CSV folder (four tables)

def test_csv_folder_round_trip_is_identical_to_plan_json(plan):
    loaded = load_plan(DEMO, **META)
    assert loaded.model_dump(mode="json") == plan.model_dump(mode="json")
    assert validate(loaded) == []


# ---------------------------------------------------------------- flat schedule export

def test_flat_csv_gives_the_same_schedule(plan):
    loaded = load_plan(FLAT, **META)
    assert validate(loaded) == []
    assert loaded.data_date == plan.data_date                       # = first planned start
    by_id = {t.id: t for t in plan.tasks}
    for t in loaded.tasks:
        assert t == by_id[t.id], t.id                               # every task field identical
    assert _deps(loaded) == _deps(plan)
    assert {loc.id for loc in loaded.locations} - {loc.id for loc in plan.locations} == {"L-WOHNHAUS-ARSENAL"}
    people = {p.id: p for p in plan.persons}
    for p in loaded.persons:
        assert (p.name, p.company, p.phone) == (people[p.id].name, people[p.id].company, people[p.id].phone)


def test_flat_csv_with_reference_has_full_people_and_locations(plan):
    loaded = load_plan(FLAT, reference=DEMO, **META)
    assert sorted(loaded.persons, key=lambda p: p.id) == sorted(plan.persons, key=lambda p: p.id)
    assert loaded.locations == plan.locations


def test_flat_csv_loads_into_the_graph_with_the_planned_dates(plan):
    loaded = load_plan(FLAT, **META)
    pg = ProjectGraph.from_plan(loaded, EventStore(), today=loaded.data_date)
    assert all(t.status == TaskStatus.not_started for t in loaded.tasks)
    for t in plan.tasks:
        assert pg.schedule()[t.id] == (t.planned_start, t.planned_finish), t.id


def test_parse_predecessors():
    assert parse_predecessors("T5-FRAME; T5-SCREED FS+14;X SS-2 ; ", Path("f"), 2) == [
        ("T5-FRAME", "FS", 0.0), ("T5-SCREED", "FS", 14.0), ("X", "SS", -2.0)]
    with pytest.raises(IngestionError, match=r"f:7: cannot read predecessor 'A FS\+'"):
        parse_predecessors("A FS+", Path("f"), 7)


def test_bad_rows_name_file_line_and_column(tmp_path):
    lines = FLAT.read_text(encoding="utf-8").splitlines()
    lines[3] = lines[3].replace("2026-11-05", "5th Nov", 1)
    bad = tmp_path / "bad.csv"
    bad.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(IngestionError, match=r"bad\.csv:4: column 'planned_start'"):
        load_plan(bad)
    (tmp_path / "short.csv").write_text("id,name\nA,a\n", encoding="utf-8")
    with pytest.raises(IngestionError, match="missing column"):
        load_plan(tmp_path / "short.csv")


def test_flat_csv_understands_german_headers_and_values(tmp_path):
    (tmp_path / "terminplan.csv").write_text(
        "Nr.;Vorgang;Art;Gewerk;Ort;Verantwortlich-ID;Dauer;Beginn;Ende;Vorgänger;Haltepunkt\n"
        "A;Wände stellen;Arbeit;Trockenbau;T1;P1;3 d;02.11.2026;04.11.26;–;\n"
        "B;Abnahme;Abnahme;–;T1;P2;1 Tag;05.11.2026;05.11.2026;A;ja\n"
        ";;;;;;;;;;\n"
        "C;Übergabe;Meilenstein;;T1;P1;0;06.11.2026;06.11.2026;B FS+0;\n", encoding="utf-8")
    p = load_plan(tmp_path / "terminplan.csv")
    t = {x.id: x for x in p.tasks}
    assert list(t) == ["A", "B", "C"]
    assert (t["A"].kind.value, t["A"].duration_days, t["A"].planned_finish) == ("work", 3, date(2026, 11, 4))
    assert t["B"].kind.value == "inspection" and t["B"].hold_point and t["B"].trade is None
    assert t["C"].kind.value == "milestone"
    assert _deps(p) == {("A", "B", "FS", 0.0), ("B", "C", "FS", 0.0)}
    assert not errors(validate(p))


# ---------------------------------------------------------------- validation

def _broken(plan: ProjectPlan) -> ProjectPlan:
    change = {"T5-PAINT": {"responsible_person_id": "P-NOBODY"},
              "T5-TILE": {"planned_finish": date(2026, 10, 1)},
              "T6-SAN-FF": {"id": "T6-TILE"}}                                         # duplicate id
    t = [x.model_copy(update=change.get(x.id, {})) for x in plan.tasks]
    deps = plan.dependencies + [
        Dependency(id="X1", predecessor_id="F2-HANDOVER", successor_id="T5-FRAME"),   # cycle
        Dependency(id="X2", predecessor_id="T5-FRAME", successor_id="GHOST")]
    return plan.model_copy(update={"tasks": t, "dependencies": deps})


def test_validate_reports_every_problem_readably(plan):
    msgs = [str(p) for p in errors(validate(_broken(plan)))]
    text = "\n".join(msgs)
    assert "responsible person 'P-NOBODY' does not exist" in text
    assert "planned finish 2026-10-01 is before planned start" in text
    assert "duplicate task id" in text
    assert "dependency cycle" in text
    assert "dependency X2: task 'GHOST' does not exist" in text
    assert all(m.startswith("ERROR") for m in msgs)


def test_validate_warns_on_dates_that_break_a_link_and_on_progress(plan):
    tasks = [t.model_copy(update={"planned_start": date(2026, 11, 3)}) if t.id == "T5-ELEC-RI"
             else t.model_copy(update={"status": TaskStatus.done}) if t.id == "T5-FRAME" else t
             for t in plan.tasks]
    probs = validate(plan.model_copy(update={"tasks": tasks}))
    assert not errors(probs)
    text = "\n".join(map(str, probs))
    assert "T5-ELEC-RI is planned to start 2026-11-03 but T5-FRAME" in text
    assert "already carry progress (T5-FRAME)" in text


# ---------------------------------------------------------------- CLI

def test_cli_writes_plan_and_refuses_on_errors(tmp_path, capsys, plan):
    out = tmp_path / "plan.json"
    assert cli([str(DEMO), "-o", str(out), "--project-id", META["project_id"], "--name", META["name"]]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    for person in written["persons"]:
        person["telegram_chat_id"] = None   # see load_plan() above
    assert written == plan.model_dump(mode="json")

    broken = tmp_path / "broken.json"
    broken.write_text(_broken(plan).model_dump_json(), encoding="utf-8")
    assert cli([str(broken), "-o", str(tmp_path / "never.json")]) == 1
    assert not (tmp_path / "never.json").exists()
    assert "nothing written" in capsys.readouterr().out


# ---------------------------------------------------------------- MS Project XML (MSPDI)

XML = DEMO / "sources" / "schedule.xml"
REAL = ROOT / "tests" / "fixtures" / "mspdi_real_style.xml"


def test_mspdi_export_reads_back_to_the_plan(plan):
    loaded = load_plan(XML, project_id=META["project_id"])
    assert validate(loaded) == []
    assert (loaded.name, loaded.data_date) == (plan.name, plan.data_date)
    by_id = {t.id: t for t in plan.tasks}
    for t in loaded.tasks:
        assert t == by_id[t.id], t.id                               # every task field identical
    assert _deps(loaded) == _deps(plan)
    assert {loc.id: (loc.kind, loc.parent_id) for loc in loaded.locations} == \
           {loc.id: (loc.kind, loc.parent_id) for loc in plan.locations}
    assert [p.id for p in loaded.persons] and {p.id for p in loaded.persons} <= {p.id for p in plan.persons}


def test_mspdi_with_reference_is_complete_and_graph_reproduces_dates(plan):
    loaded = load_plan(XML, project_id=META["project_id"], reference=DEMO)
    assert loaded.locations == plan.locations
    pg = ProjectGraph.from_plan(loaded, EventStore(), today=loaded.data_date)
    for t in plan.tasks:
        assert pg.schedule()[t.id] == (t.planned_start, t.planned_finish), t.id


def test_mspdi_real_style_export_uses_the_fallbacks():
    p = load_plan(REAL)
    t = {x.id: x for x in p.tasks}
    assert (p.project_id, p.name, p.data_date) == ("mspdi_real_style", "Haus A – Erdgeschoss", date(2026, 3, 2))
    assert list(t) == ["T3", "T4", "T5", "T7", "T8", "T9", "T10"]          # summaries + blank row skipped
    assert t["T3"].duration_days == 5 and t["T7"].duration_days == 5       # calendar days from the dates
    assert t["T3"].status == TaskStatus.not_started and t["T3"].actual_start is None   # progress not imported
    assert t["T5"].kind.value == "inspection" and t["T5"].hold_point          # by name: "Abnahme"
    assert t["T8"].kind.value == "delivery"
    assert t["T10"].kind.value == "milestone" and t["T10"].planned_start == date(2026, 3, 26)  # 17:00 -> next morning
    assert (t["T3"].responsible_person_id, t["T4"].responsible_person_id) == ("BM", "R2")
    assert t["T5"].responsible_person_id == t["T9"].responsible_person_id == "P-UNASSIGNED"  # none / material only
    lag = {(d.predecessor_id, d.successor_id): d.lag_days for d in p.dependencies}
    assert lag[("T5", "T7")] == 2.0 and lag[("T8", "T9")] == 3.0              # working days / elapsed days
    assert {loc.id: loc.kind.value for loc in p.locations} == {"L-HAUS-A": "building", "L-ERDGESCHOSS": "floor"}
    assert t["T10"].location_id == "L-HAUS-A" and t["T3"].location_id == "L-ERDGESCHOSS"

    probs = validate(p)
    assert not errors(probs)
    assert sum("nobody is responsible" in str(x) for x in probs) == 2

    pg = ProjectGraph.from_plan(p, EventStore(), today=p.data_date)
    for x in p.tasks:
        assert pg.schedule()[x.id] == (x.planned_start, x.planned_finish), x.id


@pytest.mark.parametrize("content, message", [
    ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><Project/>', "DOCTYPE/ENTITY"),
    ("<Workbook/>", "not an MS Project XML file"),
    ("<Project><Tasks>", "not valid XML"),
    (("<Project><Tasks><Task><UID>1</UID><Name>S</Name><OutlineLevel>1</OutlineLevel><Summary>1</Summary></Task>"
     "</Tasks></Project>"), "no tasks"),
    (("<Project><Tasks><Task><UID>1</UID><Name>S</Name><OutlineLevel>1</OutlineLevel><Summary>1</Summary></Task>"
     "<Task><UID>2</UID><Name>A</Name><OutlineLevel>2</OutlineLevel><Start>2026-01-01T08:00:00</Start>"
     "<Finish>2026-01-01T17:00:00</Finish><PredecessorLink><PredecessorUID>1</PredecessorUID></PredecessorLink>"
     "</Task></Tasks></Project>"), "summary task UID 1"),
])
def test_mspdi_rejects_bad_files(tmp_path, content, message):
    f = tmp_path / "bad.xml"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(IngestionError, match=message):
        load_plan(f)


# ---------------------------------------------------------------- Excel

XLSX = DEMO / "sources" / "schedule.xlsx"


def _by_id(items) -> list:
    return sorted(items, key=lambda x: x.id)


def test_excel_schedule_with_reference_sheets_is_the_plan(plan):
    loaded = load_plan(XLSX, **META)
    assert validate(loaded) == []
    assert (loaded.name, loaded.data_date) == (plan.name, plan.data_date)
    assert _by_id(loaded.tasks) == _by_id(plan.tasks)
    assert _by_id(loaded.persons) == _by_id(plan.persons)          # from sheet "Personen"
    assert loaded.locations == plan.locations                        # from sheet "Orte"
    assert _deps(loaded) == _deps(plan)


def test_excel_workbook_with_the_four_tables_is_identical_to_plan_json(tmp_path, plan):
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name in ("persons", "locations", "tasks", "dependencies"):
        ws = wb.create_sheet(name)
        lines = (DEMO / f"{name}.csv").read_text(encoding="utf-8").splitlines()
        for row in csv.reader(lines):
            ws.append(row)
    wb.save(tmp_path / "tables.xlsx")
    loaded = load_plan(tmp_path / "tables.xlsx", **META)
    assert loaded.model_dump(mode="json") == plan.model_dump(mode="json")


def test_excel_without_reference_sheets_derives_people_and_locations(tmp_path, plan):
    from openpyxl import load_workbook

    wb = load_workbook(XLSX)
    del wb["Personen"], wb["Orte"]
    wb.save(tmp_path / "only_schedule.xlsx")
    loaded = load_plan(tmp_path / "only_schedule.xlsx", **META)
    assert _by_id(loaded.tasks) == _by_id(plan.tasks) and _deps(loaded) == _deps(plan)
    assert {p.id for p in loaded.persons} == {p.id for p in plan.persons}
    assert "L-WOHNHAUS-ARSENAL" in {loc.id for loc in loaded.locations}


def test_excel_errors_name_workbook_sheet_and_row(tmp_path):
    from openpyxl import Workbook, load_workbook

    wb = load_workbook(XLSX)
    wb["Terminplan"]["L7"] = "5th Nov"                    # column L = Beginn, row 7 = third task
    wb.save(tmp_path / "bad.xlsx")
    with pytest.raises(IngestionError, match=r"bad\.xlsx\[Terminplan\]:7: column 'planned_start'"):
        load_plan(tmp_path / "bad.xlsx")

    empty = Workbook()
    empty.active["A1"] = "nothing here"
    empty.save(tmp_path / "empty.xlsx")
    with pytest.raises(IngestionError, match="no schedule found"):
        load_plan(tmp_path / "empty.xlsx")

    (tmp_path / "fake.xlsx").write_text("not a zip", encoding="utf-8")
    with pytest.raises(IngestionError, match="not a readable .xlsx"):
        load_plan(tmp_path / "fake.xlsx")
    (tmp_path / "old.xls").write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(IngestionError, match="save it as .xlsx"):
        load_plan(tmp_path / "old.xls")


# ---------------------------------------------------------------- Primavera P6 XER

XER = DEMO / "sources" / "schedule.xer"
XER_REAL = ROOT / "tests" / "fixtures" / "xer_real_style.xer"


def test_xer_export_reads_back_to_the_plan(plan):
    loaded = load_plan(XER)                                         # project id + name come from the file
    assert validate(loaded) == []
    assert (loaded.project_id, loaded.name, loaded.data_date) == (plan.project_id, plan.name, plan.data_date)
    assert _by_id(loaded.tasks) == _by_id(plan.tasks)
    assert _deps(loaded) == _deps(plan)
    loc = lambda p: sorted((x.id, x.name, x.kind, x.parent_id) for x in p.locations)
    assert loc(loaded) == loc(plan)                                 # rooms named by the "Ort" activity code
    pg = ProjectGraph.from_plan(loaded, EventStore(), today=loaded.data_date)
    for t in plan.tasks:
        assert pg.schedule()[t.id] == (t.planned_start, t.planned_finish), t.id


def test_xer_with_reference_has_full_people(plan):
    loaded = load_plan(XER, reference=DEMO)
    assert _by_id(loaded.persons) == _by_id(plan.persons)


def test_xer_real_style_export_uses_the_fallbacks():
    p = load_plan(XER_REAL)                                         # cp1252, Mon-Fri calendar, no codes/UDFs
    t = {x.id: x for x in p.tasks}
    assert (p.project_id, p.name, p.data_date) == ("HAUS-B", "Haus B – Erdgeschoss", date(2026, 3, 2))
    assert list(t) == ["A1000", "A1010", "A1020", "A1040", "A1050", "A1060"]       # LOE A1030 skipped
    assert t["A1000"].status == TaskStatus.not_started and t["A1000"].duration_days == 5   # progress ignored
    assert t["A1020"].kind.value == "inspection" and t["A1020"].hold_point
    assert t["A1050"].kind.value == "delivery"
    assert t["A1060"].kind.value == "milestone" and t["A1060"].planned_start == date(2026, 3, 19)  # 17:00 -> next day
    assert t["A1010"].responsible_person_id == "R2" and t["A1000"].responsible_person_id == "BM"
    assert t["A1020"].responsible_person_id == t["A1040"].responsible_person_id == "P-UNASSIGNED"  # none / material
    links = {(d.predecessor_id, d.successor_id): (d.type.value, d.lag_days) for d in p.dependencies}
    assert links[("A1020", "A1040")] == ("FS", 2.0) and links[("A1050", "A1040")] == ("SS", 1.0)   # hours / 8
    assert not any("A1030" in k for k in links)                     # link to the LOE dropped
    assert {x.id: (x.name, x.parent_id) for x in p.locations} == {"EG": ("Erdgeschoss", None), "BAD": ("Bad EG", "EG")}
    assert t["A1040"].location_id == "BAD"
    assert sum("nobody is responsible" in str(x) for x in validate(p)) == 2
    pg = ProjectGraph.from_plan(p, EventStore(), today=p.data_date)
    for x in p.tasks:
        assert pg.schedule()[x.id] == (x.planned_start, x.planned_finish), x.id


def _xer(*lines: str) -> str:
    return "\r\n".join(["ERMHDR\t19.12", *lines, "%E"]) + "\r\n"


TWO_PROJECTS = _xer(
    "%T\tPROJECT", "%F\tproj_id\tproj_short_name", "%R\t1\tP-ONE", "%R\t2\tP-TWO",
    "%T\tTASK", "%F\ttask_id\tproj_id\ttask_code\ttask_name\ttarget_start_date\ttarget_end_date",
    "%R\t11\t1\tA\tEins\t2026-01-05 08:00\t2026-01-06 17:00",
    "%R\t21\t2\tB\tZwei\t2026-02-02 08:00\t2026-02-02 17:00")


def test_xer_with_several_projects_needs_a_choice(tmp_path):
    f = tmp_path / "two.xer"
    f.write_text(TWO_PROJECTS, encoding="utf-8")
    with pytest.raises(IngestionError, match=r"2 projects in this file \(P-ONE, P-TWO\)"):
        load_plan(f)
    two = load_plan(f, project_id="P-TWO")
    assert [x.id for x in two.tasks] == ["B"] and two.data_date == date(2026, 2, 2)


@pytest.mark.parametrize("content, message", [
    ("%T\tTASK\n", "not a Primavera XER file"),
    (_xer("%R\t1\t2"), "data row before a %T/%F header"),
    (_xer("%T\tTASK", "%F\ttask_id"), "no PROJECT table"),
    (_xer("%T\tPROJECT", "%F\tproj_id\tproj_short_name", "%R\t1\tP",
          "%T\tTASK", "%F\ttask_id\tproj_id\ttask_code\ttask_name\ttarget_start_date\ttarget_end_date",
          "%R\t11\t1\tA\tEins\t2026-01-05 08:00\t2026-01-05 17:00",
          "%T\tTASKPRED", "%F\ttask_pred_id\ttask_id\tpred_task_id\tproj_id\tpred_type",
          "%R\t1\t11\t999\t1\tPR_FS"), "links activity 999 -> 11"),
])
def test_xer_rejects_bad_files(tmp_path, content, message):
    f = tmp_path / "bad.xer"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(IngestionError, match=message):
        load_plan(f)
