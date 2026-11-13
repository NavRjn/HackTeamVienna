# policy — local rules
Read `docs/06-resolution-policy/README.md` first.
- Deterministic: Discrepancy (+ graph queries) → list[Action]. Rules live in `policy.yaml`, code just applies them.
- Notify the people whose work MOVES (downstream responsible persons), escalate decisions to the site manager, ask the sender only to clarify.
- Dedup, batching per person, quiet hours (except critical). Templates in de + en; LLM rewording is optional and must preserve all dates/ids.
