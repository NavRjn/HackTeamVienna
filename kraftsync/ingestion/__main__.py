"""uv run python -m kraftsync.ingestion <path> -o plan.json
   uv run python -m kraftsync.ingestion <file.pdf> --draft -o plan.draft.json

Reads a plan (plan.json, a folder of CSV tables, one flat CSV export, Excel, MS Project XML or
P6 XER), prints the validation report, and writes the ProjectPlan JSON only if there are no errors.
Exit code 1 on errors.

PDFs are drafted, not loaded: --draft lets an LLM read the PDF (needs ANTHROPIC_API_KEY or an
`ant auth login` profile), checks the draft against the PDF's own text, prints a review table and
writes a DRAFT. A person reviews it before it is frozen to plan.json.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

from kraftsync.contracts import ProjectPlan
from kraftsync.ingestion import IngestionError, Problem, errors, load_plan, validate
from kraftsync.ingestion.pdf import Drafter, draft_plan_from_pdf


def review_table(plan: ProjectPlan) -> list[str]:
    preds: dict[str, list[str]] = {}
    for d in plan.dependencies:
        lag = "" if d.type.value == "FS" and not d.lag_days else f" {d.type.value}{d.lag_days:+g}"
        preds.setdefault(d.successor_id, []).append(d.predecessor_id + lag)
    rows = [f"  {'id':15} {'start':10} {'finish':10} {'who':8} {'predecessors':32} name"]
    for t in sorted(plan.tasks, key=lambda t: (t.planned_start, t.id)):
        rows.append(f"  {t.id:15} {t.planned_start} {t.planned_finish} {t.responsible_person_id:8} "
                    f"{'; '.join(preds.get(t.id, [])) or '-':32} {t.name}")
    return rows


def main(argv: list[str] | None = None, drafter_factory: Callable[[], Drafter] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles
    ap = argparse.ArgumentParser(prog="python -m kraftsync.ingestion", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path)
    ap.add_argument("-o", "--out", type=Path, help="write the ProjectPlan JSON here")
    ap.add_argument("--project-id")
    ap.add_argument("--name")
    ap.add_argument("--data-date", type=date.fromisoformat, help="YYYY-MM-DD, default: first planned start")
    ap.add_argument("--reference", type=Path, help="folder with persons.csv / locations.csv to enrich a flat CSV")
    ap.add_argument("--draft", action="store_true", help="PDF only: let an LLM draft the plan (plan time, reviewed)")
    a = ap.parse_args(argv)

    grounding: list[Problem] = []
    try:
        if a.path.suffix.lower() == ".pdf" and a.draft:
            drafter = drafter_factory() if drafter_factory else None
            plan, grounding = draft_plan_from_pdf(a.path, drafter, a.project_id, a.name, a.data_date)
        else:
            plan = load_plan(a.path, a.project_id, a.name, a.data_date, a.reference)
    except IngestionError as e:
        print(f"ERROR   {e}")
        return 1

    problems = grounding + validate(plan)
    print(f"{a.path}{' (DRAFT)' if a.draft else ''}: {len(plan.tasks)} tasks, {len(plan.dependencies)} "
          f"dependencies, {len(plan.persons)} persons, {len(plan.locations)} locations")
    for p in problems:
        print(f"  {p}")
    if errors(problems):
        print(f"{len(errors(problems))} error(s): nothing written.")
        return 1
    print("OK" + (f" ({len(problems)} warning(s))" if problems else ""))
    if a.draft:
        print("\nREVIEW THIS DRAFT against the PDF before using it:")
        print("\n".join(review_table(plan)))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        print(f"wrote {a.out}" + (" (DRAFT - review, then freeze it to plan.json)" if a.draft else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
