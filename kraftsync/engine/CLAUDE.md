# engine — local rules
Read `docs/05-engine/README.md` first.
- This is the ONLY package allowed to call an LLM for interpretation.
- The LLM chooses `task_id` only from the deterministic candidate shortlist; validate the output and reject unknown ids.
- Below `CONFIDENCE_MIN` or no task → ask the sender (via policy), never apply.
- Always log `update_interpreted`, including rejected/low-confidence ones (needed for eval and the demo feed).
- Must work offline: keyword fallback interpreter when no API key (tests use it).
- `eval` prints: task accuracy, kind accuracy, ask-rate, wrong-and-confident rate, p50/p95 latency.
- Prompt and model version go into `TaskUpdate.model`.
