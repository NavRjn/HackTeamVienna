"""Build the demo *source documents* from data/demo/plan.json (the single source of truth).

Writes data/demo/sources/:
  schedule_export.csv   one flat file, one row per task, like an export from a scheduling tool
                        (predecessors in one column, person and location denormalised)
  bauzeitplan.pdf       the printed schedule a site manager would hand out: project participants,
                        locations, schedule table, bar chart, dependency list
  schedule.xlsx         Excel workbook: sheet "Terminplan" with German headers, a title block above the
                        header row and real date cells; sheets "Personen" and "Orte" with the full
                        people and locations (persons.csv / locations.csv columns)
  schedule.xml          MS Project XML (MSPDI, "Save as XML"): summary tasks per location, 7-day
                        calendar, links with lag, resources + assignments, custom fields
                        (KraftSync-ID, Ort, Gewerk, LV-Position, Haltepunkt)
  schedule.xer          Primavera P6 XER: WBS per location, 7-day calendar, activities (task_code =
                        our id), relationships with lag in hours, resources + assignments, activity
                        codes "Gewerk"/"Ort", user fields "LV-Position"/"Haltepunkt"

These are the INPUTS ingestion is meant to read; tests/test_sources.py checks they stay in sync
with plan.json. Output is deterministic (no timestamps), so the files only change when the plan does.

Run after scripts/generate_demo_plan.py:
    uv run python scripts/generate_demo_sources.py
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from pathlib import Path

from kraftsync.contracts import Dependency, ProjectPlan, Task

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "data" / "demo" / "plan.json"
OUT = ROOT / "data" / "demo" / "sources"

CSV_COLUMNS = ["id", "name", "kind", "trade", "location_id", "location", "responsible_person_id",
               "responsible", "company", "phone", "duration_days", "planned_start", "planned_finish",
               "predecessors", "hold_point", "boq_ref"]


# ============================================================================ shared helpers

def predecessor_ref(d: Dependency) -> str:
    """'T5-FRAME' for plain finish-to-start, otherwise 'T5-SCREED FS+14' / 'X SS-2'."""
    if d.type.value == "FS" and not d.lag_days:
        return d.predecessor_id
    lag = int(d.lag_days) if float(d.lag_days).is_integer() else d.lag_days
    return f"{d.predecessor_id} {d.type.value}{'+' if lag >= 0 else ''}{lag}"


def predecessors_of(plan: ProjectPlan) -> dict[str, list[Dependency]]:
    out: dict[str, list[Dependency]] = {t.id: [] for t in plan.tasks}
    for d in plan.dependencies:            # keep plan order -> deterministic output
        out[d.successor_id].append(d)
    return out


def location_path(plan: ProjectPlan, loc_id: str) -> str:
    locs = {loc.id: loc for loc in plan.locations}
    parts, cur = [], locs.get(loc_id)
    while cur:
        parts.append(cur.name)
        cur = locs.get(cur.parent_id) if cur.parent_id else None
    return " > ".join(reversed(parts))


def de(d: date) -> str:
    return d.strftime("%d.%m.%Y")


# ============================================================================ CSV

def build_csv(plan: ProjectPlan) -> str:
    persons = {p.id: p for p in plan.persons}
    preds = predecessors_of(plan)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    for t in sorted(plan.tasks, key=lambda t: (t.planned_start, t.id)):
        p = persons[t.responsible_person_id]
        w.writerow({
            "id": t.id, "name": t.name, "kind": t.kind.value, "trade": t.trade or "",
            "location_id": t.location_id, "location": location_path(plan, t.location_id),
            "responsible_person_id": p.id, "responsible": p.name, "company": p.company or "",
            "phone": p.phone or "", "duration_days": f"{t.duration_days:g}",
            "planned_start": t.planned_start.isoformat(), "planned_finish": t.planned_finish.isoformat(),
            "predecessors": "; ".join(predecessor_ref(d) for d in preds[t.id]),
            "hold_point": "yes" if t.hold_point else "no", "boq_ref": t.boq_ref or "",
        })
    return buf.getvalue()


# ============================================================================ minimal PDF writer
# Standard Helvetica, WinAnsi encoding plus three extra glyphs (ć č ı) mapped onto unused codes.
# No compression, so the text stays greppable (tests check task ids appear in the PDF).

EXTRA = {"ć": (0x81, "cacute"), "č": (0x8D, "ccaron"), "ı": (0x8F, "dotlessi")}
W, H, M = 842, 595, 36                     # A4 landscape, margin
NL = "\n"                                  # line break inside a table cell


def _enc(s: str) -> bytes:
    out = bytearray()
    for ch in s:
        if ch in EXTRA:
            out.append(EXTRA[ch][0])
        else:
            out += ch.encode("cp1252", errors="replace")
    return bytes(out).replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _width(s: str, size: float) -> float:
    """Approximate Helvetica advance width (good enough for column fitting)."""
    w = 0.0
    for ch in s:
        if ch in "il.,:;|'!I1 ":
            w += 0.28
        elif ch in "mwMWOQGD@%–":
            w += 0.8
        elif ch.isupper() or ch.isdigit():
            w += 0.64
        else:
            w += 0.52
    return w * size


def _fit(s: str, size: float, max_w: float) -> str:
    if _width(s, size) <= max_w:
        return s
    while s and _width(s + "…", size) > max_w:
        s = s[:-1]
    return s + "…"


class Page:
    def __init__(self) -> None:
        self.ops: list[bytes] = []

    def text(self, x: float, y: float, s: str, size: float = 8, bold: bool = False, max_w: float | None = None,
             rgb: tuple[float, float, float] = (0.1, 0.1, 0.1)) -> None:
        if max_w:
            s = _fit(s, size, max_w)
        r, g, b = rgb
        self.ops.append(b"BT %.3f %.3f %.3f rg /%s %.1f Tf %.1f %.1f Td (" % (r, g, b, b"F2" if bold else b"F1", size, x, y)
                        + _enc(s) + b") Tj ET")

    def line(self, x1: float, y1: float, x2: float, y2: float, w: float = 0.4, gray: float = 0.7) -> None:
        self.ops.append(b"%.2f G %.2f w %.1f %.1f m %.1f %.1f l S" % (gray, w, x1, y1, x2, y2))

    def rect(self, x: float, y: float, w: float, h: float, rgb: tuple[float, float, float]) -> None:
        self.ops.append(b"%.3f %.3f %.3f rg %.1f %.1f %.1f %.1f re f" % (*rgb, x, y, w, h))

    def diamond(self, cx: float, cy: float, r: float, rgb: tuple[float, float, float]) -> None:
        self.ops.append(b"%.3f %.3f %.3f rg %.1f %.1f m %.1f %.1f l %.1f %.1f l %.1f %.1f l f"
                        % (*rgb, cx, cy + r, cx + r, cy, cx, cy - r, cx - r, cy))


def write_pdf(pages: list[Page], title: str) -> bytes:
    diffs = " ".join(f"{code} /{name}" for code, name in sorted(EXTRA.values()))
    enc = f"<< /Type /Encoding /BaseEncoding /WinAnsiEncoding /Differences [{diffs}] >>"
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # pages, filled below
        f"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding {enc} >>".encode(),
        f"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding {enc} >>".encode(),
        b"<< /Title (" + _enc(title) + b") /Producer (KraftSync generate_demo_sources) >>",
    ]
    kids = []
    for pg in pages:
        stream = b"\n".join(pg.ops)
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
                    b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>" % (W, H, len(objs)))
        kids.append(len(objs))
    objs[1] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


# ============================================================================ PDF content

KIND_DE = {"work": "Arbeit", "inspection": "Abnahme", "delivery": "Lieferung", "milestone": "Meilenstein"}
ROLE_DE = {"site_manager": "Bauleitung", "foreman": "Polier/Vorarbeiter", "worker": "Arbeiter",
           "inspector": "Prüfer (ÖBA)", "client_rep": "Bauherrenvertretung", "supplier": "Lieferant"}
LOC_DE = {"building": "Gebäude", "floor": "Geschoss", "unit": "Wohnung", "room": "Raum", "zone": "Zone"}
KIND_RGB = {"work": (0.55, 0.55, 0.55), "inspection": (0.85, 0.55, 0.1), "delivery": (0.1, 0.55, 0.55),
            "milestone": (0.45, 0.3, 0.7)}


def _header(pg: Page, plan: ProjectPlan, subtitle: str, n: int, total: int) -> float:
    pg.text(M, H - M - 4, f"Bauzeitplan – {plan.name}", 15, bold=True)
    pg.text(M, H - M - 22, f"{subtitle}   ·   Projekt {plan.project_id}   ·   Stand {de(plan.data_date)}   ·   "
                           f"Kalendertage", 8.5, rgb=(0.35, 0.35, 0.35))
    pg.text(W - M - 40, M - 18, f"Seite {n}/{total}", 7.5, rgb=(0.45, 0.45, 0.45))
    pg.line(M, H - M - 30, W - M, H - M - 30, 0.8, 0.3)
    return H - M - 48


def _table(pg: Page, y: float, cols: list[tuple[str, float]], rows: list[list[str]], size: float = 7.5,
           row_h: float = 13) -> float:
    x = M
    for head, w in cols:
        pg.text(x + 2, y, head, size, bold=True, max_w=w - 4)
        x += w
    pg.line(M, y - 4, M + sum(w for _, w in cols), y - 4, 0.6, 0.4)
    y -= row_h
    line_h = size * 1.2
    for i, row in enumerate(rows):
        n_lines = max(cell.count(NL) + 1 for cell in row)
        h = row_h + (n_lines - 1) * line_h
        if i % 2:
            pg.rect(M, y - h + row_h - 4, sum(w for _, w in cols), h, (0.95, 0.95, 0.93))
        x = M
        for (_, w), cell in zip(cols, row):
            for k, part in enumerate(cell.split(NL)):
                pg.text(x + 2, y - k * line_h, part, size, max_w=w - 4)
            x += w
        y -= h
    return y


def _wrap_refs(deps: list[Dependency], per_line: int = 2) -> str:
    refs = [predecessor_ref(d) for d in deps]
    return NL.join("; ".join(refs[i:i + per_line]) for i in range(0, len(refs), per_line)) or "–"


def build_pdf(plan: ProjectPlan) -> bytes:
    persons = {p.id: p for p in plan.persons}
    locs = {loc.id: loc for loc in plan.locations}
    preds = predecessors_of(plan)
    tasks: list[Task] = sorted(plan.tasks, key=lambda t: (t.planned_start, t.id))
    total = 4
    pages = []

    # 1 - participants + locations
    pg = Page()
    y = _header(pg, plan, "Projektbeteiligte und Bauabschnitte", 1, total)
    pg.text(M, y, "Projektbeteiligte", 10.5, bold=True)
    y = _table(pg, y - 16, [("ID", 50), ("Name", 110), ("Funktion", 100), ("Tätigkeit", 75), ("Firma", 140),
                            ("Gewerk", 75), ("Telefon", 95), ("Sprache", 45)],
               [[p.id, p.name, ROLE_DE.get(p.role.value, p.role.value), p.job_title.value if p.job_title else "–",
                 p.company or "", p.trade or "–", p.phone or "", p.language] for p in plan.persons])
    pg.text(M, y - 14, "Bauabschnitte / Orte", 10.5, bold=True)
    _table(pg, y - 30, [("ID", 60), ("Bezeichnung", 140), ("Typ", 70), ("Teil von", 60), ("Pfad", 320)],
           [[loc.id, loc.name, LOC_DE.get(loc.kind.value, loc.kind.value), loc.parent_id or "–",
             location_path(plan, loc.id)] for loc in plan.locations])
    pages.append(pg)

    # 2 - schedule table
    pg = Page()
    y = _header(pg, plan, f"Terminplan ({len(tasks)} Vorgänge)", 2, total)
    _table(pg, y, [("ID", 72), ("Vorgang", 150), ("Art", 50), ("Gewerk", 52), ("Ort", 46), ("Verantw.", 76),
                   ("Dauer", 30), ("Beginn", 48), ("Ende", 48), ("Vorgänger", 140), ("HP", 20), ("LV-Pos.", 38)],
           [[t.id, t.name, KIND_DE[t.kind.value], t.trade or "–", t.location_id,
             persons[t.responsible_person_id].name, f"{t.duration_days:g} d", de(t.planned_start)[:6] + de(t.planned_start)[8:],
             de(t.planned_finish)[:6] + de(t.planned_finish)[8:], _wrap_refs(preds[t.id]),
             "ja" if t.hold_point else "", t.boq_ref or ""] for t in tasks], size=7, row_h=15.5)
    pg.text(M, M, "HP = Haltepunkt: nachfolgende Arbeiten (Beplankung) erst nach Abnahme.   "
                  "Vorgänger ohne Zusatz = Ende-Anfang ohne Abstand; FS+14 = 14 Tage Wartezeit (z. B. Estrichtrocknung).",
            6.8, rgb=(0.35, 0.35, 0.35))
    pages.append(pg)

    # 3 - bar chart
    pg = Page()
    y = _header(pg, plan, "Balkenplan", 3, total)
    d0 = min(t.planned_start for t in tasks)
    d1 = max(t.planned_finish for t in tasks)
    days = (d1 - d0).days + 1
    x0, x1 = M + 175, W - M
    px = (x1 - x0) / days
    for i in range(days):
        d = d0 + timedelta(days=i)
        if d.weekday() >= 5:
            pg.rect(x0 + i * px, M + 10, px, y - M - 14, (0.96, 0.96, 0.96))
        if d.weekday() == 0:
            pg.line(x0 + i * px, M + 10, x0 + i * px, y + 2, 0.4, 0.75)
            pg.text(x0 + i * px + 2, y, d.strftime("%d.%m."), 7, bold=True, rgb=(0.3, 0.3, 0.3))
    row_h = (y - M - 24) / len(tasks)
    for i, t in enumerate(tasks):
        ry = y - 16 - (i + 1) * row_h + 4
        pg.text(M, ry + 2, f"{t.id}", 7, bold=True, max_w=70)
        pg.text(M + 72, ry + 2, t.name, 6.5, max_w=100, rgb=(0.35, 0.35, 0.35))
        start = (t.planned_start - d0).days
        rgb = KIND_RGB[t.kind.value]
        if t.duration_days == 0:
            pg.diamond(x0 + start * px + px / 2, ry + 4.5, 5, rgb)
        else:
            pg.rect(x0 + start * px + 0.5, ry, (t.planned_finish - t.planned_start).days * px + px - 1, 9, rgb)
    lx = M
    for kind, rgb in KIND_RGB.items():
        pg.rect(lx, M - 12, 9, 7, rgb)
        pg.text(lx + 12, M - 12, KIND_DE[kind], 7)
        lx += 80
    pg.text(lx, M - 12, "grau hinterlegt = Wochenende (geplant wird in Kalendertagen)", 7, rgb=(0.4, 0.4, 0.4))
    pages.append(pg)

    # 4 - dependencies
    pg = Page()
    y = _header(pg, plan, f"Abhängigkeiten ({len(plan.dependencies)})", 4, total)
    _table(pg, y, [("Nr.", 40), ("Vorgänger", 95), ("Nachfolger", 95), ("Typ", 35), ("Abstand", 50), ("Grund", 455)],
           [[d.id, d.predecessor_id, d.successor_id, d.type.value, f"{d.lag_days:g} d", d.reason or ""]
            for d in plan.dependencies], size=7, row_h=12)
    pages.append(pg)

    assert len(pages) == total and all(loc in locs for loc in (t.location_id for t in tasks))
    return write_pdf(pages, f"Bauzeitplan – {plan.name}")


# ============================================================================ Excel

XLSX_HEADERS = ["Nr.", "Vorgang", "Art", "Gewerk", "Ort", "Ortspfad", "Verantwortlich-ID", "Verantwortlich",
                "Firma", "Telefon", "Dauer", "Beginn", "Ende", "Vorgänger", "Haltepunkt", "LV-Position"]
XLSX_WIDTHS = [15, 40, 12, 12, 9, 44, 17, 17, 20, 15, 7, 11, 11, 34, 11, 12]
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def _deterministic_zip(data: bytes) -> bytes:
    """openpyxl stamps the save time into the zip entries and docProps/core.xml; pin both."""
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            body = src.read(info.filename)
            if info.filename == "docProps/core.xml":
                body = re.sub(rb"(<dcterms:(created|modified)[^>]*>)[^<]*", rb"\g<1>2026-01-01T00:00:00Z", body)
            dst.writestr(zipfile.ZipInfo(info.filename, date_time=FIXED_TIME), body, zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def build_xlsx(plan: ProjectPlan) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    persons = {p.id: p for p in plan.persons}
    preds = predecessors_of(plan)
    wb = Workbook()
    ws = wb.active
    ws.title = "Terminplan"
    ws["A1"] = f"Bauzeitplan – {plan.name}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Projekt {plan.project_id} · Stand {de(plan.data_date)} · Kalendertage"
    ws["A2"].font = Font(italic=True, color="666666")
    header_row = 4
    for col, (head, width) in enumerate(zip(XLSX_HEADERS, XLSX_WIDTHS), 1):
        c = ws.cell(row=header_row, column=col, value=head)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="44546A")
        c.alignment = Alignment(vertical="center")
        ws.column_dimensions[c.column_letter].width = width
    for r, t in enumerate(sorted(plan.tasks, key=lambda t: (t.planned_start, t.id)), header_row + 1):
        p = persons[t.responsible_person_id]
        values = [t.id, t.name, KIND_DE[t.kind.value], t.trade or "", t.location_id,
                  location_path(plan, t.location_id), p.id, p.name, p.company or "", p.phone or "",
                  t.duration_days, t.planned_start, t.planned_finish,
                  "; ".join(predecessor_ref(d) for d in preds[t.id]), "ja" if t.hold_point else "", t.boq_ref or ""]
        for col, v in enumerate(values, 1):
            c = ws.cell(row=r, column=col, value=v if v != "" else None)
            if isinstance(v, date):
                c.number_format = "DD.MM.YYYY"
            elif col == 11:
                c.number_format = '0" d"'
    ws.freeze_panes = ws.cell(row=header_row + 1, column=3)
    ws.auto_filter.ref = f"A{header_row}:{ws.cell(row=header_row, column=len(XLSX_HEADERS)).column_letter}" \
                         f"{header_row + len(plan.tasks)}"

    for title, rows in (("Personen", plan.persons), ("Orte", plan.locations)):
        sheet = wb.create_sheet(title)
        dicts = [json.loads(x.model_dump_json()) for x in rows]
        cols = list(dicts[0].keys())
        for col, head in enumerate(cols, 1):
            sheet.cell(row=1, column=col, value=head).font = Font(bold=True)
            sheet.column_dimensions[sheet.cell(row=1, column=col).column_letter].width = 18
        for r, d in enumerate(dicts, 2):
            for col, key in enumerate(cols, 1):
                if d[key] not in (None, "", []):
                    sheet.cell(row=r, column=col, value=d[key])

    wb.properties.creator = "KraftSync generate_demo_sources"
    buf = io.BytesIO()
    wb.save(buf)
    return _deterministic_zip(buf.getvalue())


# ============================================================================ MS Project XML (MSPDI)

NS = "http://schemas.microsoft.com/project"
# Custom field ids as MS Project numbers them (Text1..Text4, Flag1); the reader matches by alias.
CUSTOM = [("188743731", "Text1", "KraftSync-ID"), ("188743734", "Text2", "Ort"),
          ("188743737", "Text3", "Gewerk"), ("188743740", "Text4", "LV-Position"),
          ("188743752", "Flag1", "Haltepunkt")]
LINK_CODE = {"FF": "0", "FS": "1", "SF": "2", "SS": "3"}


def _sub(parent: ET.Element, tag: str, text: object | None = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def _subs(parent: ET.Element, pairs: tuple[tuple[str, object], ...]) -> None:
    for tag, val in pairs:
        _sub(parent, tag, val)


def build_mspdi(plan: ProjectPlan) -> bytes:
    """MS Project XML export of the plan. 7-day calendar (8 h/day) so MS Project shows the same
    calendar-day dates; lags are elapsed days (LagFormat 8). One summary task per location
    (rooms roll up into their unit); the exact location id travels in the 'Ort' custom field."""
    locs = {loc.id: loc for loc in plan.locations}
    children: dict[str | None, list[str]] = {}
    for loc in plan.locations:
        children.setdefault(loc.parent_id, []).append(loc.id)
    tasks_at: dict[str, list[Task]] = {}
    for t in sorted(plan.tasks, key=lambda t: (t.planned_start, t.id)):
        anc = t.location_id
        while locs[anc].kind.value == "room" and locs[anc].parent_id:
            anc = locs[anc].parent_id
        tasks_at.setdefault(anc, []).append(t)

    root = ET.Element("Project", xmlns=NS)
    _subs(root, (("SaveVersion", 14), ("Name", f"{plan.project_id}.xml"), ("Title", plan.name),
                 ("ScheduleFromStart", 1), ("StartDate", f"{plan.data_date}T08:00:00"),
                 ("FinishDate", f"{max(t.planned_finish for t in plan.tasks)}T17:00:00"),
                 ("CalendarUID", 1), ("DefaultStartTime", "08:00:00"), ("DefaultFinishTime", "17:00:00"),
                 ("MinutesPerDay", 480), ("MinutesPerWeek", 3360), ("DaysPerMonth", 30),
                 ("DurationFormat", 7), ("WorkFormat", 2)))
    eas = _sub(root, "ExtendedAttributes")
    for fid, fname, alias in CUSTOM:
        _subs(_sub(eas, "ExtendedAttribute"), (("FieldID", fid), ("FieldName", fname), ("Alias", alias)))

    cal = _sub(_sub(root, "Calendars"), "Calendar")
    _subs(cal, (("UID", 1), ("Name", "7-Tage-Woche"), ("IsBaseCalendar", 1)))
    weekdays = _sub(cal, "WeekDays")
    for day_type in range(1, 8):
        wd = _sub(weekdays, "WeekDay")
        _subs(wd, (("DayType", day_type), ("DayWorking", 1)))
        times = _sub(wd, "WorkingTimes")
        for a, b in (("08:00:00", "12:00:00"), ("13:00:00", "17:00:00")):
            _subs(_sub(times, "WorkingTime"), (("FromTime", a), ("ToTime", b)))

    rows: list[tuple[str, object, int]] = []        # ("summary", Location, level) | ("task", Task, level)

    def walk(lid: str, level: int) -> None:
        rows.append(("summary", locs[lid], level))
        rows.extend(("task", t, level + 1) for t in tasks_at.get(lid, []))
        for child in children.get(lid, []):
            if locs[child].kind.value != "room":
                walk(child, level + 1)

    for top in children.get(None, []):
        walk(top, 1)
    uid_of = {obj.id: i for i, (what, obj, _) in enumerate(rows, 1) if what == "task"}

    deps_of = predecessors_of(plan)
    tasks_el = _sub(root, "Tasks")
    _subs(_sub(tasks_el, "Task"), (("UID", 0), ("ID", 0), ("Name", plan.name), ("OutlineLevel", 0),
                                   ("Summary", 1)))      # project summary task, as MS Project writes it
    for i, (what, obj, level) in enumerate(rows, 1):
        el = _sub(tasks_el, "Task")
        _subs(el, (("UID", i), ("ID", i), ("Name", obj.name), ("Type", 1), ("IsNull", 0), ("OutlineLevel", level)))
        if what == "summary":
            _subs(el, (("Summary", 1), ("Milestone", 0)))
            _subs(_sub(el, "ExtendedAttribute"), (("FieldID", CUSTOM[1][0]), ("Value", obj.id)))
            continue
        t: Task = obj
        d = round(t.duration_days)
        _subs(el, (("Summary", 0), ("Milestone", 1 if d == 0 else 0),
                   ("Start", f"{t.planned_start}T08:00:00"),
                   ("Finish", f"{t.planned_finish}T{'08:00:00' if d == 0 else '17:00:00'}"),
                   ("Duration", f"PT{8 * d}H0M0S"), ("DurationFormat", 7)))
        for dep in deps_of[t.id]:
            _subs(_sub(el, "PredecessorLink"), (
                ("PredecessorUID", uid_of[dep.predecessor_id]), ("Type", LINK_CODE[dep.type.value]),
                ("LinkLag", round(dep.lag_days * 1440 * 10)), ("LagFormat", 8)))
        values = (t.id, t.location_id, t.trade or "", t.boq_ref or "", 1 if t.hold_point else 0)
        for (fid, _, _), val in zip(CUSTOM, values):
            if val != "":
                _subs(_sub(el, "ExtendedAttribute"), (("FieldID", fid), ("Value", val)))

    res_el = _sub(root, "Resources")
    res_uid = {}
    for i, p in enumerate(plan.persons, 1):
        res_uid[p.id] = i
        _subs(_sub(res_el, "Resource"), (("UID", i), ("ID", i), ("Name", p.name), ("Type", 1), ("IsNull", 0),
                                         ("Initials", p.id), ("Group", p.company or "")))
    asg = _sub(root, "Assignments")
    for i, t in enumerate(sorted(plan.tasks, key=lambda t: uid_of[t.id]), 1):
        _subs(_sub(asg, "Assignment"), (("UID", i), ("TaskUID", uid_of[t.id]),
                                        ("ResourceUID", res_uid[t.responsible_person_id]), ("Units", 1)))

    ET.indent(root, "  ")
    header = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    return header + ET.tostring(root, encoding="utf-8", xml_declaration=False) + b"\n"


# ============================================================================ Primavera P6 XER

def build_xer(plan: ProjectPlan) -> bytes:
    """P6 XER export of the plan: 7-day calendar with 8 h days, so lags in hours / 8 = calendar days."""
    locs = {loc.id: loc for loc in plan.locations}
    out: list[str] = ["ERMHDR\t19.12\t2026-01-01\tProject\tadmin\tKraftSync\tdbxDatabaseNoName\tProject Management\tEUR"]

    def table(name: str, fields: list[str], rows: list[list[object]]) -> None:
        out.append(f"%T\t{name}")
        out.append("%F\t" + "\t".join(fields))
        out.extend("%R\t" + "\t".join("" if v is None else str(v) for v in row) for row in rows)

    def ts(d: date, time: str = "08:00") -> str:
        return f"{d.isoformat()} {time}"

    pid = 1
    table("CALENDAR", ["clndr_id", "default_flag", "clndr_name", "clndr_type", "day_hr_cnt", "week_hr_cnt"],
          [[1, "Y", "7-Tage-Woche", "CA_Base", 8, 56]])
    table("PROJECT", ["proj_id", "proj_short_name", "plan_start_date", "scd_end_date", "clndr_id"],
          [[pid, plan.project_id, ts(plan.data_date), ts(max(t.planned_finish for t in plan.tasks), "17:00"), 1]])

    wbs_id = {loc.id: 101 + i for i, loc in enumerate(x for x in plan.locations if x.kind.value != "room")}
    wbs_rows = [[100, pid, 1, "Y", plan.project_id, plan.name, ""]]
    for loc in plan.locations:
        if loc.id in wbs_id:
            parent = wbs_id.get(loc.parent_id, 100) if loc.parent_id else 100
            wbs_rows.append([wbs_id[loc.id], pid, len(wbs_rows) + 1, "N", loc.id, loc.name, parent])
    table("PROJWBS", ["wbs_id", "proj_id", "seq_num", "proj_node_flag", "wbs_short_name", "wbs_name", "parent_wbs_id"],
          wbs_rows)

    rsrc_id = {p.id: 1 + i for i, p in enumerate(plan.persons)}
    table("RSRC", ["rsrc_id", "rsrc_name", "rsrc_short_name", "rsrc_type", "email_addr"],
          [[rsrc_id[p.id], p.name, p.id, "RT_Labor", p.email or ""] for p in plan.persons])

    trades = sorted({t.trade for t in plan.tasks if t.trade})
    code_id = {("Gewerk", v): 1 + i for i, v in enumerate(trades)}
    code_id |= {("Ort", loc.id): 100 + i for i, loc in enumerate(plan.locations)}
    table("ACTVTYPE", ["actv_code_type_id", "seq_num", "actv_code_type", "proj_id", "actv_code_type_scope"],
          [[1, 1, "Gewerk", pid, "AS_Project"], [2, 2, "Ort", pid, "AS_Project"]])
    table("ACTVCODE", ["actv_code_id", "actv_code_type_id", "short_name", "actv_code_name", "seq_num"],
          [[cid, 1 if typ == "Gewerk" else 2, val, val if typ == "Gewerk" else locs[val].name, cid]
           for (typ, val), cid in code_id.items()])

    ordered = sorted(plan.tasks, key=lambda t: (t.planned_start, t.id))
    task_id = {t.id: 1001 + i for i, t in enumerate(ordered)}
    task_rows = []
    for t in ordered:
        anc = t.location_id
        while locs[anc].kind.value == "room" and locs[anc].parent_id:
            anc = locs[anc].parent_id
        d = round(t.duration_days)
        end = ts(t.planned_finish, "08:00" if d == 0 else "17:00")
        task_rows.append([task_id[t.id], pid, wbs_id[anc], 1, t.id, t.name, "TT_FinMile" if d == 0 else "TT_Task",
                          "TK_NotStart", 8 * d, 8 * d, ts(t.planned_start), end, ts(t.planned_start), end, "", ""])
    table("TASK", ["task_id", "proj_id", "wbs_id", "clndr_id", "task_code", "task_name", "task_type", "status_code",
                   "target_drtn_hr_cnt", "remain_drtn_hr_cnt", "target_start_date", "target_end_date",
                   "early_start_date", "early_end_date", "act_start_date", "act_end_date"], task_rows)

    actv = []
    for t in ordered:
        if t.trade:
            actv.append([task_id[t.id], 1, code_id[("Gewerk", t.trade)], pid])
        actv.append([task_id[t.id], 2, code_id[("Ort", t.location_id)], pid])
    table("TASKACTV", ["task_id", "actv_code_type_id", "actv_code_id", "proj_id"], actv)

    deps_of = predecessors_of(plan)
    preds = [[0, task_id[t.id], task_id[d.predecessor_id], pid, pid, f"PR_{d.type.value}", f"{d.lag_days * 8:g}"]
             for t in ordered for d in deps_of[t.id]]
    for i, row in enumerate(preds, 1):
        row[0] = i
    table("TASKPRED", ["task_pred_id", "task_id", "pred_task_id", "proj_id", "pred_proj_id", "pred_type",
                       "lag_hr_cnt"], preds)
    table("TASKRSRC", ["taskrsrc_id", "task_id", "proj_id", "rsrc_id"],
          [[i, task_id[t.id], pid, rsrc_id[t.responsible_person_id]] for i, t in enumerate(ordered, 1)])
    table("UDFTYPE", ["udf_type_id", "table_name", "udf_type_name", "udf_type_label", "logical_data_type"],
          [[1, "TASK", "user_field_1", "LV-Position", "FT_TEXT"], [2, "TASK", "user_field_2", "Haltepunkt", "FT_TEXT"]])
    udf = [[1, task_id[t.id], pid, t.boq_ref] for t in ordered if t.boq_ref]
    udf += [[2, task_id[t.id], pid, "Y" if t.hold_point else "N"] for t in ordered]
    table("UDFVALUE", ["udf_type_id", "fk_id", "proj_id", "udf_text"], udf)
    out.append("%E")
    return ("\r\n".join(out) + "\r\n").encode("utf-8")


# ============================================================================ main

def main(out: Path = OUT) -> None:
    plan = ProjectPlan.model_validate_json(PLAN.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    files = {"schedule_export.csv": build_csv(plan).encode("utf-8"), "bauzeitplan.pdf": build_pdf(plan),
             "schedule.xlsx": build_xlsx(plan), "schedule.xml": build_mspdi(plan), "schedule.xer": build_xer(plan)}
    for fname, content in files.items():
        target = out / fname
        if target.exists() and target.read_bytes() == content:
            print(f"  unchanged {fname}")            # don't touch it (it may be open in Excel / a viewer)
            continue
        target.write_bytes(content)
        print(f"  wrote     {fname}")
    print(f"sources in {out} are in sync with {PLAN.name}")


if __name__ == "__main__":
    main()
