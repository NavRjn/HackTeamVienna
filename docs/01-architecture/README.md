# 01 · Architecture

## Data flow

```
                 ┌──────────────── PLAN TIME ────────────────┐
 CSV / PDF /     │  ingestion  ──ProjectPlan──▶  graph       │
 MS Project/P6 ──┘                              (load +      │
 (or data/demo/plan.json)                        baseline)   │
                                                             │
                 ┌──────────────── LIVE ──────────────────────┘
 Telegram ─┐
 WhatsApp ─┼─▶ integrations ──InboundMessage──▶ engine.interpret()   ◀── the ONLY AI step
 Email ────┘   (sender → Person)                   │
                                                   ▼ TaskUpdate (+confidence)
                                     confidence < τ ? ──yes──▶ policy: ask sender
                                                   │ no
                                                   ▼
                                     graph.apply(update)  → GraphEvent(s)
                                     graph.detect()       → Discrepancy[] (+Impact via CPM)
                                                   │
                                                   ▼
                                     policy.resolve(discrepancy) → Action[]
                                                   │
                     integrations.send(action) ◀───┘   (+ GraphEvent action_sent)

 app/: reads graph state + event log → graph view, critical path, discrepancy feed
```

## The determinism rule

| Step | Deterministic? | Why |
|---|---|---|
| Ingestion of structured files (CSV, MSPDI XML, XER) | yes | parsers |
| Ingestion of PDFs | AI allowed, but output reviewed and frozen into `plan.json` before use | plan time, not live |
| Sender resolution | yes | lookup in persons list |
| **Message → TaskUpdate** | **no (LLM)** | natural language, slang, multilingual |
| Apply update, CPM, detect discrepancies | yes | testable, explainable |
| Resolution policy (who gets what) | yes | YAML rules |
| Wording of outbound text | LLM optional, template fallback | cosmetic |

Consequence: discrepancies are explainable ("T6-BOARD started while predecessor T6-INSP is not_started") and unit-testable. The demo never depends on the LLM "noticing" an anomaly.

## Ontology (summary — details in `04-graph`)

- **Node = Task** (work, inspection, delivery, milestone). Has location, trade, responsible person, planned/actual/forecast dates, status, hold-point flag, optional BoQ/billing ref.
- **Edge = Dependency** (FS/SS/FF/SF + lag + reason, e.g. "same crew", "screed drying").
- **Person** and **Location** are reference entities referenced by id (not graph nodes in the MVP).
- **Event** = append-only record of a change (`GraphEvent`). Events are not nodes.
- **Discrepancy** = derived from state after an event; carries an **Impact** (delay days, affected downstream tasks, critical-path flag, project finish before/after).
- **Action** = what the policy wants sent to whom.

## Contracts

`kraftsync/contracts/models.py` is the single source of truth. Module boundaries:

| Producer → Consumer | Model |
|---|---|
| ingestion → graph | `ProjectPlan` |
| integrations → engine | `InboundMessage` |
| engine → graph | `TaskUpdate` |
| graph → (log, app) | `GraphEvent` |
| graph → policy | `Discrepancy` |
| policy → integrations | `Action` |

## Public interfaces (target signatures)

```python
# ingestion
def load_plan(path: Path) -> ProjectPlan

# graph
class ProjectGraph:
    @classmethod
    def from_plan(cls, plan: ProjectPlan, store: EventStore) -> "ProjectGraph"
    def candidates_for(self, person_id: str | None, today: date) -> list[Task]   # for the engine prompt
    def apply(self, update: TaskUpdate, actor: str) -> list[GraphEvent]
    def detect(self, events: list[GraphEvent]) -> list[Discrepancy]
    def schedule(self) -> dict[str, tuple[date, date]]      # CPM forecast start/finish
    def critical_path(self) -> list[str]
    def downstream(self, task_id: str) -> list[str]

# engine
def interpret(msg: InboundMessage, candidates: list[Task], persons: list[Person], today: date) -> TaskUpdate

# policy
def resolve(d: Discrepancy, graph: ProjectGraph) -> list[Action]
def ask_clarification(msg: InboundMessage, update: TaskUpdate, graph: ProjectGraph) -> Action

# integrations
def resolve_sender(sender_ref: str, persons: list[Person]) -> str | None
async def send(action: Action, persons: list[Person]) -> Action        # returns with status
```

## Runtime

One Python process for the MVP: FastAPI app + Telegram bot (polling) + the pipeline in-process. SQLite for the event store. A cron-like task (every few minutes) runs `silent_overdue` detection. Deployment options in `07-deployment-demo`.
