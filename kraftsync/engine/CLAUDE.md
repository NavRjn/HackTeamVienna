# engine — local rules
Read `docs/05-engine/README.md` and `docs/04-graph/README.md` first.
- This package is **deterministic**: `resolve.py` (who gets told). No LLM, no network, no I/O. It never computes dates or detects conflicts: schedule, ripple and discrepancies come from `kraftsync.graph.ProjectGraph`, the single point of control (do not add a second forecast or detection here). The LLM step lives in n8n (`docs/n8n-workflow.md`); do not add an LLM call to this repo's Python.
- The orchestration (`process_claim`) is in `kraftsync/app/api.py`; keep the pipeline order in `docs/05-engine` true.
- Detection lives in the graph (`ProjectGraph.detect`): it raises only what is newly wrong, so a repeated message is silent.
- Below `CONFIDENCE_MIN` or no task → ask the sender, never apply. Always log `update_interpreted`, including ignored/low-confidence ones.
- Every Discrepancy needs a human-readable `description` and, for delays, an `Impact`.
- Tests are the spec: `tests/test_api.py` (gold replay) and `tests/test_graph.py`. A new rule needs a test on demo data.
