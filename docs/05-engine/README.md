# 05 · Interpretation and orchestration

**Status: the interpretation step lives in n8n; the orchestration lives in the local service.** The deterministic checks live in `kraftsync/graph/` (`ProjectGraph.apply` / `detect`), the routing in `kraftsync/engine/resolve.py`; neither calls an LLM. The only LLM call in the repo is plan-time PDF drafting in `kraftsync/ingestion/pdf.py` (a person reviews its output), never in the message pipeline. The LLM node, prompt and schema are specified in [`docs/n8n-workflow.md`](../n8n-workflow.md).

Goal: turn a free-text site message into a structured, honest claim, then run it through the deterministic pipeline.

## Pipeline (`kraftsync/app/api.py`, `process_claim`)

```
for each claim in POST /updates:
  already seen message_id           → duplicate (nothing happens)
  log update_interpreted            (always, even if ignored or rejected later)
  kind irrelevant / question        → ignored
  sender unknown                    → ignored
  task_id null or confidence < CONFIDENCE_MIN
                                    → ask the sender (resolve.ask_clarification); nothing applied
  else: apply → check(before, after) → resolve_all → actions
```
Rejected before any of this with `422`: a `task_id` that is not in the plan, a `new_forecast_finish` more than 60 days from today, duplicate `message_id`s within one request. The raw message is logged once (`message_received`) even if it carries several claims.

## Interpretation design (the n8n LLM node)

- **Shortlist first, then classify.** The LLM never sees "all tasks". `POST /context` returns two short lists: `tasks` (the sender's own tasks active within ±3 days of today, ≤ 15) and `related_tasks` (named locations, neighbours of the sender's tasks, other tasks active now; ≤ 15). The prompt tells the model to prefer `tasks`. Fewer choices → fewer wrong mappings.
- **Constrained output.** Structured output with a JSON schema; `task_id` is an id from `tasks` or `related_tasks`, or null. n8n nulls any id not in those lists; the service rejects unknown ids with `422`.
- **Context in the prompt:** today's date and weekday (from `/context`, to resolve "Donnerstag", "morgen"), the sender (name, trade), the two task lists `tasks` (own) and `related_tasks` (id, name, location, status, planned dates), a glossary (Beplankung, Vorwand, Rohinstallation, Estrich, Abnahme, Top = apartment, OG = floor).
- **Several claims per message:** one claim per task mentioned; each gets its own `message_id` (`<base>#n`).
- **Confidence:** the model reports it; the service enforces `CONFIDENCE_MIN` (default 0.7). Combining it with candidate margin and sender-trade checks (an electrician reporting on a plumbing task) is planned, not built.
- **Dates:** relative dates are resolved by the model; the service sanity-checks them (±60 days from today).
- **Language:** German, Austrian site slang, English, mixed.

## Clarifications

Built: low confidence or no task → one `ask` action to the sender ("Welche Aufgabe meinst du? …"), nothing applied.
Not built: linking the sender's **reply** to that question and re-running the claim. Until then the reply is interpreted as a new message.

## Evaluation (the headline number)

There are two layers:
1. **Deterministic layer — done.** `tests/test_api.py::test_demo_replay_matches_gold` feeds the 10 gold messages (as perfect claims) through the API and requires exactly the expected discrepancy types. This proves detection, not the LLM.
2. **LLM layer — to do.** An n8n *Manual Trigger* path feeds `data/demo/messages.jsonl` through the real LLM node and compares `kind` and `task_id` with the gold labels. Metrics: task_id accuracy, kind accuracy, "asked when it should have" rate, **wrong-and-confident rate** (the dangerous one), latency. Grow the 10 seed messages to 80–150 synthetic ones (paraphrases, slang, typos, languages), with a held-out split.

(The earlier `kraftsync.engine.replay` / `engine.eval` commands do not exist; the test and the n8n path replace them.)

## Stretch

- Fine-tune a small model for interpretation with Adaption credits on the synthetic set (also eligible for "Best use of Adaption").
- Voice notes → transcription → same flow. Photo attachments → location hint as an extra candidate signal.
- Advisory AI step: propose "possibly affected tasks" for semantic changes the graph cannot see ("shower on the other wall"), clearly labelled as suggestions, never changing state.

## Done when

- Replay of `data/demo/messages.jsonl` produces the expected discrepancies end to end. ✅ (test)
- n8n LLM step: wrong-and-confident = 0 on the demo set, numbers in the README Results table. ⬜
- Clarification replies are linked. ⬜
