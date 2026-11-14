"""Excel (.xlsx / .xlsm) -> ProjectPlan. Deterministic (openpyxl, read-only, values not formulas).

Same two shapes as CSV, decided by the sheet names:

1. Sheets persons, locations, tasks, dependencies (same columns as data/demo/*.csv)
   -> exactly the plan they describe.

2. A schedule sheet: the first sheet whose header row has the flat-schedule columns (English or
   German: Nr./Vorgang/Art/Ort/Verantwortlich-ID/Dauer/Beginn/Ende/Vorgänger …). Title rows above
   the header are skipped, real date cells and German text dates both work, blank rows are ignored.
   Optional reference sheets with the persons.csv / locations.csv columns, named
   persons|Personen|Projektbeteiligte and locations|Orte|Bauabschnitte, give full people and
   locations; otherwise they are derived like the flat CSV.

Error messages name the workbook, the sheet and the spreadsheet row: "plan.xlsx[Terminplan]:7".
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from kraftsync.contracts import ProjectPlan
from kraftsync.ingestion.csv_loader import (
    FLAT_REQUIRED,
    IngestionError,
    Rows,
    canonical_header,
    plan_from_flat_rows,
    plan_from_tables,
    reference_from_rows,
)

TABLE_SHEETS = ("persons", "locations", "tasks", "dependencies")
PERSON_SHEETS = ("persons", "personen", "projektbeteiligte")
LOCATION_SHEETS = ("locations", "orte", "bauabschnitte")
HEADER_SCAN_ROWS = 15


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _sheet_rows(ws, required: set[str] | None = None) -> Rows | None:
    """Rows of a sheet as {header: text}. The header is the first row (within the first rows) that
    contains `required` (after alias mapping) or, without `required`, an 'id' column.
    Returns None if no such header row exists."""
    grid = [[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
    for h, row in enumerate(grid[:HEADER_SCAN_ROWS]):
        names = {canonical_header(c) for c in row if c}
        if (required and required <= names) or (not required and "id" in names):
            header = [c for c in row]
            out = []
            for n, data in enumerate(grid[h + 1:], h + 2):            # spreadsheet row numbers
                rec = {header[i]: data[i] if i < len(data) else "" for i in range(len(header)) if header[i]}
                if any(rec.values()):
                    out.append((n, rec))
            return out
    return None


def load_excel(path: str | Path, project_id: str | None = None, name: str | None = None,
               data_date: date | None = None) -> ProjectPlan:
    path = Path(path)
    if path.suffix.lower() == ".xls":
        raise IngestionError(f"{path}: old .xls format is not supported - save it as .xlsx in Excel")
    if not path.exists():
        raise IngestionError(f"{path}: file not found")
    try:
        from openpyxl import load_workbook
    except ImportError as e:                                         # pragma: no cover
        raise IngestionError("reading Excel needs openpyxl: uv add openpyxl") from e
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:                                           # zip / xml errors from openpyxl
        raise IngestionError(f"{path}: not a readable .xlsx workbook ({e.__class__.__name__}: {e})") from e

    try:
        sheets = {ws.title.strip().lower(): ws for ws in wb.worksheets}
        label = {k: f"{path.name}[{ws.title}]" for k, ws in sheets.items()}

        if all(t in sheets for t in TABLE_SHEETS):
            tables = {}
            for t in TABLE_SHEETS:
                rows = _sheet_rows(sheets[t])
                if rows is None:
                    raise IngestionError(f"{label[t]}: no header row with an 'id' column")
                tables[t] = rows
            return plan_from_tables(tables, {t: label[t] for t in TABLE_SHEETS}, project_id, name, data_date,
                                    path.stem)

        reference_titles = set(PERSON_SHEETS) | set(LOCATION_SHEETS)
        for key, ws in sheets.items():
            if key in reference_titles:
                continue
            rows = _sheet_rows(ws, FLAT_REQUIRED)
            if rows is not None:
                p_key = next((k for k in PERSON_SHEETS if k in sheets), None)
                l_key = next((k for k in LOCATION_SHEETS if k in sheets), None)
                ref_p, _ = reference_from_rows(_sheet_rows(sheets[p_key]) if p_key else None, None,
                                               label.get(p_key, ""))
                _, ref_l = reference_from_rows(None, _sheet_rows(sheets[l_key]) if l_key else None,
                                               label.get(l_key, ""))
                return plan_from_flat_rows(rows, label[key], project_id, name, data_date, path.stem, ref_p, ref_l)

        raise IngestionError(
            f"{path}: no schedule found - expected sheets {', '.join(TABLE_SHEETS)}, or a sheet with the "
            f"columns {', '.join(sorted(FLAT_REQUIRED))} (German headers like Vorgang/Beginn/Ende work too)")
    finally:
        wb.close()
