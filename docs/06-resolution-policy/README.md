# 06 · Resolution policy

Goal: decide **who needs to know what** for each discrepancy. Deterministic, configured in `kraftsync/policy/policy.yaml`.

## Why not "text the responsible person"

The responsible person of the changed task is usually the one who just reported it. Telling them again is noise. The value is telling the **people whose work moves** and asking the **site manager only for decisions**.

## Rules (MVP)

| Discrepancy | Who | What | Kind |
|---|---|---|---|
| `delay` | responsible persons of the **next affected tasks** (first 1–2 levels downstream, deduplicated) | "Your task X moves from <old> to <new> because <cause>." | notify |
| `delay` (critical path / project finish moves) | + site manager | "Handover moves <old> → <new>. Cause: … Options: accept / accelerate / resequence." | escalate |
| `hold_point_violation` | site manager + inspector + sender | "STOP: <task> started before <inspection> passed. Confirm stop or document exception." | escalate |
| `out_of_sequence` (no hold point) | sender + responsible of the unfinished predecessor | "You started X but Y isn't done — is Y actually done?" | ask |
| `blocked` | site manager (+ supplier/person named in blocker if known) | "X blocked: <blocker>. Downstream impact: …" | escalate |
| `scope_change` | site manager | "Change requested on X: <description>, by <who>. Approve as change order?" | escalate |
| `conflicting_reports` | both reporters | "You reported A, <other> reported B — which is right?" | ask |
| `silent_overdue` | responsible person | "X was due <date>. Done, or new date?" | ask |
| low-confidence interpretation | sender | "Which task do you mean: (1) … (2) …?" | ask |

## Guards

- **Dedup / rate limit:** one message per person per task per discrepancy type per 2 h. Batch multiple notices to the same person into one message.
- **Quiet hours:** 20:00–06:30, except `critical`.
- **Language:** use `Person.language`; templates in de + en.
- **Everything is logged** as `action_sent` events with the full text.
- Text: template first; LLM rewording optional and only cosmetic (must keep all dates/ids from the template).

## Interface

```python
def resolve(d: Discrepancy, graph: ProjectGraph) -> list[Action]
def ask_clarification(msg: InboundMessage, update: TaskUpdate, graph: ProjectGraph) -> Action
```

## Done when

- Demo message 2 → actions to drywall foreman + inspector (notify) and site manager (escalate, handover moved).
- Demo message 5 → escalate to site manager + inspector, ask sender.
- No duplicate messages when the same delay is reported twice.
