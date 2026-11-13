# Open questions

First raised on the first repo read (Fri 13 Nov 2026); status updated after the n8n pivot and the local service. ✅ = answered by what is now built/decided, ⬜ = still needs a person to decide. Owner = who can answer.

## Scope / process
1. ✅ **Who owns what.** Four areas, one directory each (see README, "Who owns what"): A n8n workflow, B detection engine, C graph + schema, D UI/deploy/demo. ⬜ Are others coding in branches not yet pushed? Push early, merge to `main` often. (owner: team)
2. ⬜ **`data/demo/process.json`** (step template "Interior wall - standard room" with `allowed_roles` and `requires`). Not used by any code. The roles in it (`carpenter`, `plumber`, `electrician`, `inspector`, `drywaller`, `painter`) now match the new `JobTitle` enum on `Person`, so the intent looks like "who may report which step". Candidate use: flag a claim whose sender's `job_title` doesn't fit the task (the planned sender-mismatch check, `04-graph`). Still needs the author's call: use it, or keep it out of the pipeline. (owner: author)
3. ✅ **Source of truth for the event store: JSONL** (`data/events.jsonl`, replayed on start), not SQLite. The n8n "conflict" webhook exists as an optional feature (`N8N_CONFLICT_WEBHOOK_URL`); n8n can also just use the `/updates` response. `.env.example` now lists what is actually read.

## Technical decisions
4. ⬜ **LLM for the interpretation step.** It is now an n8n node, not repo code (`ENGINE_MODEL`/`ANTHROPIC_API_KEY` in the repo are unused). Open for the n8n owner: which model in the node (a fast one for latency, a stronger one for low-confidence retries?) and is an API key/credit available in n8n? (owner: A)
5. ✅/⬜ **`DEMO_TODAY` = 2026-11-09.** Implemented in the service (candidate selection, date sanity check, the `today` returned by `/context` for the prompt; it wins over the message date). Quiet hours are not implemented, so there is no clash on Saturday. ⬜ It still has to be set in `.env` before the scripted demo.
6. ⬜ **Gold label demo:04** maps the scope change to `T6-ELEC-RI`, which demo:01 already marked done. Current behaviour: a `modified` claim on a done task raises `scope_change` and escalates to the site manager; the task is not reopened. Intended, or should it target another task? (owner: team)
7. ✅/⬜ **Demo hosting: decided** — laptop + cloudflared tunnel + n8n Cloud (`07-deployment-demo`). ⬜ Is a real Telegram group with 3 phones available, and do the phones' Telegram names match persons in `persons.csv`?
8. ⬜ **Repo licence/visibility for submission.** The repo has an MIT `LICENSE`; the Talona slide says Talona work goes to a private repo and is not published under MIT. Ask the organisers. (owner: team)

## Resolved
- **Slides** (`docs/problem-slides.pdf`) were read. Constraints beyond `00-overview`: code freeze Sat 15:00; one submission per team through the Discord form; public GitHub repo under MIT in the proximata organisation (Talona and Dorabella excepted: private repo, partner owns the work); README with setup, run, environment, architecture, assumptions; `.env.example` and no secrets; 3-minute demo video of the thing working end to end; Talona track prize $1,000; side prizes for n8n/Adaption/Maritime/Lovable usage.
