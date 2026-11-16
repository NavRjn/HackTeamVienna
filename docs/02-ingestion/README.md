# 02 · Ingestion

**Status: built.** CSV, Excel, MS Project XML, P6 XER importers and the PDF draft path live in `kraftsync/ingestion/` with tests (`tests/test_ingestion.py`, `test_sources.py`, `test_pdf.py`). The live service still loads `PLAN_PATH` (default `data/demo/plan.json`) directly, so the demo does not depend on ingestion. The sections below are the original design.

Goal: anything a real project already has → a valid `ProjectPlan`.

## Principle

The demo runs on `data/demo/plan.json` (generated, consistent). Ingestion must **produce the same shape** and is shown working in the demo ("we import your existing schedule"), but the live pipeline does not depend on its output. Less error surface.

## Demo source files (`data/demo/sources/`)

Realistic *inputs* for ingestion, generated from `data/demo/plan.json` by `scripts/generate_demo_sources.py`:

| File | What it looks like | Ingestion target |
|---|---|---|
| `schedule_export.csv` | ONE flat file, one row per task, like a scheduling-tool export: predecessors in one column (`T5-FRAME; T5-SCREED FS+14`), person and location written out | parse deterministically into a `ProjectPlan` equal to `plan.json` |
| `bauzeitplan.pdf` | 4-page German construction schedule (Bauzeitplan): participants and locations, schedule table, bar chart, dependency list | LLM draft into a `ProjectPlan`, then `validate()`, review and freeze (plan time only) |
| `schedule.xlsx` | Excel workbook: sheet `Terminplan` with German headers (Nr., Vorgang, Art, Ort, Verantwortlich-ID, Dauer, Beginn, Ende, Vorgänger, Haltepunkt …), a title block above the header and real date cells; sheets `Personen` and `Orte` with the persons.csv / locations.csv columns | `kraftsync/ingestion/excel.py`, deterministic |
| `schedule.xer` | Primavera P6 XER: WBS per location, 7-day calendar, activities (`task_code` = our id), relationships with lag in hours, resources and assignments, activity codes `Gewerk`/`Ort`, user fields `LV-Position`/`Haltepunkt` | `kraftsync/ingestion/xer.py`, deterministic |
| `schedule.xml` | MS Project XML (MSPDI): one summary task per location, 7-day calendar, links with lag, resources and assignments, custom fields `KraftSync-ID`, `Ort`, `Gewerk`, `LV-Position`, `Haltepunkt` | `kraftsync/ingestion/mspdi.py`, deterministic |

`tests/test_sources.py` keeps them connected: they must match a fresh build from `plan.json`, the CSV must read back to the same tasks, people and dependencies, and the PDF must contain every task, dependency, person and location. After changing the plan, run `generate_demo_plan.py`, then `generate_demo_sources.py`.

## Sources, in priority order

| # | Source | How | Notes |
|---|---|---|---|
| 1 | CSV set (`persons.csv`, `locations.csv`, `tasks.csv`, `dependencies.csv`) | pandas/csv → Pydantic | Same columns as `data/demo/*.csv` (persons now include `job_title`). Must work. |
| 2 | MS Project XML (MSPDI) / Primavera P6 XER | **MPXJ** (`pip install mpxj`, needs a Java runtime via JPype) | The credibility win: real schedulers export these. Map Task ↔ Task, Relation ↔ Dependency (type + lag), Resource ↔ Person. |
| 3 | Excel schedules | openpyxl → same as CSV | Many small contractors live in Excel. |
| 4 | PDF (Bauzeitplan, persons/contact lists, plans) | LLM/vision extraction → **draft** `ProjectPlan` → human review → freeze to JSON | Plan-time only. Never live. Show it, don't depend on it. |

Get a sample MSPDI/XER file early (GanttProject can export MS Project XML; MPXJ's repo has test files). If Java is painful, drop to CSV + Excel.

## Validation (must reject bad plans loudly)

- Unique ids; every `responsible_person_id`, `location_id`, dependency endpoint exists.
- Graph is acyclic.
- `planned_finish >= planned_start`; milestones have duration 0.
- Warn (don't fail) on tasks with no predecessors and no successors.
- Output a validation report, not just an exception.

## Interface

```python
def load_plan(path, project_id=None, name=None, data_date=None, reference=None) -> ProjectPlan
    # plan.json | folder with the 4 CSV tables | one flat CSV export
def validate(plan: ProjectPlan) -> list[Problem]   # Problem(level="error"|"warning", message); [] = ok
def errors(problems) -> list[Problem]
```
CLI: `uv run python -m kraftsync.ingestion <path> -o plan.json [--project-id ID] [--name NAME] [--data-date YYYY-MM-DD] [--reference FOLDER]`
It prints the report and writes only if there are no errors (exit code 1 otherwise).

## Implementation (status)

| File | What |
|---|---|
| `kraftsync/ingestion/csv_loader.py` | **Folder** of `persons/locations/tasks/dependencies.csv`: exactly the plan they describe. **Flat export** (`schedule_export.csv`): tasks and dependencies parsed from the predecessor column (`ID`, `ID FS+14`, `ID SS-2`). Missing parts are filled deterministically: people from the responsible columns (role foreman), locations from the `A > B > C` path (known ids reused, otherwise `L-<SLUG>`, kind from depth), dependency ids `D001…` in file order, no reasons, everything `not_started`. `--reference FOLDER` takes people/locations from a full persons/locations list instead. Errors name file:line and the column. Handles a UTF-8 BOM and `;` as separator (Excel). German headers and values work too: `Vorgang`, `Beginn`, `Ende`, `Dauer`, `Vorgänger`, `Gewerk`, `Ort`, `Haltepunkt`, `LV-Position`; `Arbeit`/`Abnahme`/`Lieferung`/`Meilenstein`; `3 d`, `1 Tag`; `05.11.2026` / `5.11.26`; `ja`; `–` for empty. The table logic works on plain rows, so Excel reuses it. |
| `kraftsync/ingestion/excel.py` | **Excel** `.xlsx`/`.xlsm` (openpyxl, cached values, not formulas). Sheets `persons`/`locations`/`tasks`/`dependencies` give exactly the plan. Otherwise the first sheet with a flat-schedule header (English or German, found within the first 15 rows, so title rows are fine) is the schedule. Reference sheets `Personen`/`persons`/`Projektbeteiligte` and `Orte`/`locations`/`Bauabschnitte` add full people and locations. Date cells and text dates both work; blank rows are skipped. Errors name `file.xlsx[Sheet]:row` and the column. `.xls` is refused with "save as .xlsx". |
| `kraftsync/ingestion/mspdi.py` | **MS Project XML**, standard library only (no MPXJ, no Java). Normal tasks become Tasks; summary tasks become the location tree; the project summary and blank rows are skipped. Dates: the date part of Start/Finish, and **duration = finish - start + 1 calendar days**, so our CPM reproduces the file even when MS Project used a Mon-Fri calendar. A milestone at or after the end of the working day (17:00) is dated the next morning. Links: Type 0/1/2/3 = FF/FS/SF/SS; LinkLag is tenths of a minute, counted as 24 h days for elapsed formats (`ed` etc.), as `MinutesPerDay` for working formats, or as % of the predecessor. Custom fields are matched by **alias**: `KraftSync-ID`/`ID`/`Code` gives the task id (fallback `T<UID>`), `Ort`/`Location` the location id (fallback: enclosing summary), `Gewerk`/`Trade`, `LV-Position`/`BoQ`, `Haltepunkt`/`Hold point` (fallback: inspection by name, e.g. "Abnahme"). Kind comes from Milestone, or from the name ("Abnahme" = inspection, "Lieferung" = delivery). Responsible person = first assigned *work* resource (id from Initials, fallback `R<UID>`); none gives `P-UNASSIGNED` and a warning. Progress (% complete, actual dates) is not imported. DOCTYPE/ENTITY declarations are rejected. |
| `kraftsync/ingestion/xer.py` | **Primavera P6 XER**, standard library only (no MPXJ, no Java). Tab-separated tables (`%T`/`%F`/`%R`), UTF-8 or Windows cp1252. PROJECT gives `project_id` (`proj_short_name`) and the plan date (`plan_start_date`); the project's root WBS node gives the plan name. Other WBS nodes become the location tree (`wbs_short_name` = id, made unique with a dotted path if needed). TASK becomes a Task: `task_code` = id, planned dates from `target_*` (fallback `early_*`), duration = calendar days. `TT_Mile`/`TT_FinMile` are milestones (a finish milestone at 17:00 is dated the next morning); `TT_LOE`/`TT_WBS` are skipped and links to them dropped. TASKPRED: `PR_FS/SS/FF/SF`; lag = `lag_hr_cnt` / the successor calendar's `day_hr_cnt`. Responsible = first labour/equipment resource (`rsrc_short_name`); material is ignored; none gives `P-UNASSIGNED`. Activity codes and user fields on activities are matched by name like the MS Project custom fields (`Ort`, `Gewerk`, `LV-Position`, `Haltepunkt`, `KraftSync-ID`); an `Ort` code's description names the location. Progress is not imported. Several projects in one file: `--project-id` picks one. A link to an activity outside the project is an error. |
| `kraftsync/ingestion/pdf.py` | **PDF to DRAFT plan** (plan time only). 1) `ClaudeDrafter`: Claude (`INGEST_MODEL`, default `claude-opus-5-5`, effort `high`, server-side refusal fallback on) reads the PDF as a native document and fills a fixed JSON schema (structured outputs, every object strict). The prompt says copy, never invent. 2) `plan_from_draft`: deterministic, JSON to `ProjectPlan`. 3) `grounding_problems`: deterministic, against the PDF's own text (pypdf). A task id or person that does not appear in the PDF is an **error** (invented), and so is a dependency to a task missing from the draft. A date not on the task's own row, or a name not found, is a warning. 4) A person reviews the draft and freezes it. `load_plan()` refuses `.pdf`; the only way in is `python -m kraftsync.ingestion file.pdf --draft -o plan.draft.json`, which prints the problems and a review table and refuses to write on errors. The LLM sits behind the `Drafter` protocol: tests use a fake drafter and a fake client (no network, no key). `KRAFTSYNC_LIVE_PDF=1` runs one real call. |
| `kraftsync/ingestion/validate.py` | Errors: duplicate ids, dangling person/location/task/parent ids, self-dependency, cycle, finish < start, milestone with duration. Warnings: isolated tasks, duration 0 on non-milestones, planned dates that break an FS link, progress already in the plan (a baseline should be clean). |
| `kraftsync/ingestion/__main__.py` | the CLI |
| `tests/test_ingestion.py` | folder round trip == `plan.json` byte for byte; flat CSV gives identical tasks and dependencies, and the graph reproduces every planned date from it; with `--reference` people and locations are identical too; validation messages; CLI refuses to write on errors |

The project id and name are not in the CSVs: pass them (`--project-id`, `--name`), otherwise the file or folder name is used. `data_date` defaults to the first planned start (the baseline).

Tested on our own export (`schedule.xml`: identical tasks and dependencies, and the graph reproduces every planned date) and on `tests/fixtures/mspdi_real_style.xml`, shaped like a typical MS Project export (Mon-Fri calendar, no custom fields, blank row, progress, material resource, unassigned task, working-day and elapsed lags, 17:00 milestone).

Also tested on `tests/fixtures/xer_real_style.xer`, shaped like a classic P6 export: cp1252, Mon-Fri calendar, no codes or user fields, a level-of-effort activity, a completed activity, a material resource, an unassigned activity, an SS link and a lag in hours, and a 17:00 finish milestone.

Still to do: try files actually saved by MS Project / GanttProject / P6 and a real contractor Excel schedule; run the live PDF test once (`KRAFTSYNC_LIVE_PDF=1 uv run pytest tests/test_pdf.py -k live`); scanned PDFs (images only) have no text layer, so grounding would fail them all - they need a person to type the ids or an OCR step.

## Done when

- CSV folder → `ProjectPlan` → identical to `data/demo/plan.json` (round-trip test).
- One MS Project XML or XER file imported and loaded by the graph.
