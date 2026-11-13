# 06 · Resolution policy

**Status: v0 built** in `kraftsync/engine/resolve.py` (code) with texts and one setting in `kraftsync/policy/policy.yaml`. Deterministic: discrepancies in, ready-to-send `Action`s out. n8n only delivers them.

Goal: decide **who needs to know what** for each discrepancy.

## Why not "text the responsible person"

The responsible person of the changed task is usually the one who just reported it. Telling them again is noise. The value is telling the **people whose work moves** and asking the **site manager only for decisions**.

## Rules

| Discrepancy | Who | What | Kind | Built |
|---|---|---|---|---|
| `delay` | responsible persons of the affected tasks within `downstream_levels_to_notify` (2) steps downstream; each person once, with the dates of their closest affected task; never the sender or the site manager | "Your task X moves from <old> to <new>. Cause: …" | notify | yes |
| `delay` where the **project finish moves** | all site managers | "Milestone '…' moves <old> → <new> (+N days). Cause: … Decision needed." | escalate | yes |
| `hold_point_violation` | all site managers + all inspectors; the sender is asked | "STOP: <task> started before <inspection> passed. Please confirm." | escalate / ask | yes |
| `out_of_sequence` | the sender + responsible persons of the unfinished predecessors | "<task> was started while still open: <predecessors>. Is that right?" | ask | yes |
| `blocked` | all site managers | "<task> is blocked: <blocker>. Decision needed." | escalate | yes |
| `scope_change` | all site managers | "Change requested on <task>: <description> (by <who>). Approve as change order?" | escalate | yes |
| low-confidence / no task | the sender (up to 5 options) | "Which task do you mean? …" | ask | yes |
| `conflicting_reports` | both reporters | "You reported A, <other> reported B — which is right?" | ask | planned |
| `silent_overdue` | responsible person (site manager after 48 h) | "X was due <date>. Done, or new date?" (template exists) | ask | planned |

`escalate` and `ask` actions have `reply_expected = true`; `notify` does not.

## Guards

- **One action per person per claim.** If a person qualifies for several (e.g. the inspector for a hold-point escalation and an out-of-sequence question), the most urgent wins: `escalate` > `ask` > `notify`. Re-reporting the same news produces no new discrepancy and therefore no new messages (`tests/test_api.py::test_same_delay_reported_again_does_not_notify_again`).
- **Language:** `Person.language`, templates in de + en (falls back to de).
- **Stage mode:** with `DEMO_REDIRECT_CHAT_ID` every action goes to one chat, prefixed `[an <name>]`.
- Text is template-only; LLM rewording is optional and not built (it must keep all dates/ids from the template).
- **Not built:** the 2 h dedup window and batching across messages, quiet hours (20:00–06:30 except critical), `action_sent` events (the service does not know whether n8n delivered).

## What `policy.yaml` actually does today

Read by the code: `templates` (de/en) and `downstream_levels_to_notify`. **Not read:** the `rules:` block, `confidence_min` (the service uses the env var `CONFIDENCE_MIN`), `quiet_hours`, `dedup_window_hours`. The `rules:` block documents intent; the table above is what `resolve.py` does. If you change who gets told, change the code and this table; if you only change wording, edit the templates.

Template keys: `delay_notify`, `delay_escalate`, `hold_point`, `scope_change`, `clarify`, `silent_overdue`, `out_of_sequence`, `blocked`.

## Interface

```python
# kraftsync/engine/resolve.py
resolve_all(discrepancies, before, after, deps, persons, update, sender, now) -> list[Action]
ask_clarification(sender: Person, options: list[Task], message_id: str, now) -> Action
```
`Action.id` is `<discrepancy id>:<person id>` (deterministic). The API attaches `recipients` (name + Telegram chat id) so n8n can send without another lookup.

## Done when

- Demo message 02 → notify drywaller + inspector, escalate to the site manager (handover moved), nothing for the plumber → `test_ripple_notifies_affected_trades_and_escalates`. ✅
- Demo message 05 → escalate to site manager + inspector, ask the sender, one message each → `test_hold_point_violation_escalates_immediately`. ✅
- No duplicate messages when the same delay is reported twice. ✅
- Quiet hours, batching, `conflicting_reports` / `silent_overdue` messages. ⬜
