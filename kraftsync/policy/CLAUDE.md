# policy — local rules
Read `docs/06-resolution-policy/README.md` first.
- The rules are implemented in `kraftsync/engine/resolve.py`; `policy.yaml` supplies the message templates (de + en) and `downstream_levels_to_notify`. The `rules:` block in the YAML is documentation of intent and is **not read**.
- Deterministic: Discrepancy (+ graph queries) → list[Action]. Notify the people whose work MOVES (downstream responsible persons), escalate decisions to the site manager, ask the sender only to clarify.
- One action per person per claim (escalate > ask > notify). Templates in de + en; LLM rewording is optional, not built, and must preserve all dates/ids.
- Not built yet: dedup window, batching, quiet hours. If you add them, update the doc.
