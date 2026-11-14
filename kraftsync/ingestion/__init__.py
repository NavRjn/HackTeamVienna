"""Public interface of the ingestion package: anything a project already has -> ProjectPlan."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from kraftsync.contracts import ProjectPlan
from kraftsync.ingestion.csv_loader import (
    IngestionError,
    is_csv_folder,
    load_csv_folder,
    load_flat_csv,
)
from kraftsync.ingestion.excel import load_excel
from kraftsync.ingestion.mspdi import load_mspdi
from kraftsync.ingestion.validate import UNASSIGNED, Problem, errors, validate
from kraftsync.ingestion.xer import load_xer


def load_plan(path: str | Path, project_id: str | None = None, name: str | None = None,
              data_date: date | None = None, reference: str | Path | None = None) -> ProjectPlan:
    """Dispatch on what `path` is: plan.json, a folder of the four CSV tables, one flat CSV export,
    an Excel workbook (.xlsx/.xlsm), an MS Project XML file (.xml) or a Primavera P6 export (.xer)."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        plan = ProjectPlan.model_validate_json(path.read_text(encoding="utf-8"))
        return plan.model_copy(update={k: v for k, v in
                                       {"project_id": project_id, "name": name, "data_date": data_date}.items() if v})
    if is_csv_folder(path):
        return load_csv_folder(path, project_id, name, data_date)
    if path.suffix.lower() == ".csv":
        return load_flat_csv(path, project_id, name, data_date, reference)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return load_excel(path, project_id, name, data_date)
    if path.suffix.lower() == ".xml":
        return load_mspdi(path, project_id, name, data_date, reference)
    if path.suffix.lower() == ".xer":
        return load_xer(path, project_id, name, data_date, reference)
    if path.suffix.lower() == ".pdf":
        raise IngestionError(f"{path}: PDFs are drafted, not loaded. Run "
                             f"`python -m kraftsync.ingestion {path} --draft -o plan.draft.json`, review the "
                             f"draft, then freeze it to plan.json (kraftsync.ingestion.pdf.draft_plan_from_pdf)")
    raise IngestionError(f"{path}: unsupported input (expected .json, .csv, .xlsx, .xml, .xer or a folder with "
                         f"persons/locations/tasks/dependencies.csv)")


__all__ = ["UNASSIGNED", "IngestionError", "Problem", "errors", "load_plan", "validate"]
