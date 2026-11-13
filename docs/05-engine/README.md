# 05 · Engine

Goal: turn a free-text site message into a structured `TaskUpdate` — reliably, with honest confidence — and run the live loop.

## Pipeline (`kraftsync/engine/pipeline.py`)

```
handle(msg: InboundMessage):
  log message_received
  if msg answers a pending clarification → merge and continue
  candidates = graph.candidates_for(msg.sender_person_id, today)      # deterministic shortlist
  update     = interpret(msg, candidates, persons, today)             # LLM, structured output
  log update_interpreted (always, even if not applied)
  if update.kind in {irrelevant}                    → stop
  if update.kind == question                        → policy.answer_from_graph(...)  (stretch)
  if update.confidence < CONFIDENCE_MIN or task_id is None → policy.ask_clarification(...)
  else: events = graph.apply(update); discrepancies = graph.detect(events)
        for d in discrepancies: actions += policy.resolve(d, graph)
  for a in actions: integrations.send(a)
```

## Interpretation design

- **Shortlist first, then classify.** The LLM never sees "all tasks". It gets ≤ ~15 candidates: the sender's own open tasks (responsible person / same trade), tasks mentioned by location ("Top 6", "Bad"), and anything due ±7 days. Fewer choices → fewer wrong mappings.
- **Constrained output.** Structured output / tool call with JSON schema of `TaskUpdate`; `task_id` must be one of the candidate ids or null (validate after the call; reject otherwise).
- **Context in the prompt:** today's date and weekday (to resolve "Donnerstag", "next Monday"), sender's name/trade/company, candidate tasks with id, name, location, status, planned dates, the last 3 messages in the same chat (for "fertig" style follow-ups).
- **Language:** German, Austrian site slang, English, mixed. Glossary in the prompt (Beplankung, Vorwand, Rohinstallation, Estrich, Abnahme, Top = apartment, OG = floor).
- **Confidence:** model-reported confidence is not enough. Combine: model confidence × candidate margin × rule checks (e.g. sender's trade ≠ task's trade → lower). Threshold `CONFIDENCE_MIN` (env, default 0.7).
- **Dates:** relative dates resolved by the model, then sanity-checked in code (not in the past for forecasts, within ±60 days).
- **Fallback without API key:** a keyword/regex interpreter so tests and the demo replay work offline.

## Evaluation (the number we show judges)

`uv run python -m kraftsync.engine.eval data/demo/messages.jsonl`
- Metrics: task_id accuracy, kind accuracy, "asked when it should have" rate, **wrong-and-confident rate** (the dangerous one), latency.
- Seed: 10 demo messages with gold labels. Grow to 80–150 synthetic messages (paraphrases, slang, typos, languages, voice-note transcripts) generated from the plan, labelled automatically from the generator. Keep a held-out split.

## Stretch

- Fine-tune a small model for interpretation with Adaption credits on the synthetic set (also eligible for "Best use of Adaption").
- Voice notes → transcription → same pipeline.
- Photo attachments → location hint (room recognition) as an extra candidate signal.

## Done when

- Replay of `data/demo/messages.jsonl` produces the expected update kinds and discrepancies end-to-end.
- Eval prints numbers; wrong-and-confident = 0 on the demo set.
- Works offline with the fallback interpreter.
