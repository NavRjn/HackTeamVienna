// n8n Code node "Build request" (Run Once for All Items). Sits between the LLM and HTTP /updates.
// Adds what the LLM must not decide (ids, dates arithmetic, model tag) and enforces the guards.
const norm = $('Normalize').first().json;      // sender_id, received_at, text, chat_ref, base_id
const ctx = $('Context').first().json;          // today, tasks, related_tasks
const raw = $input.first().json;
const claims = (raw.output ?? raw).claims ?? [];

const known = new Set([...ctx.tasks, ...ctx.related_tasks].map((t) => t.id));
const addDays = (iso, n) => {
  const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};

// 1. guard: unknown task id -> null + confidence 0 (service then asks the sender)
let list = claims.map((c) => known.has(c.task_id) ? c : { ...c, task_id: null, confidence: 0 });
// 2. one claim per task: keep the most confident (small models sometimes emit duplicates)
const best = new Map();
for (const c of list.filter((c) => c.task_id)) {
  if (!best.has(c.task_id) || c.confidence > best.get(c.task_id).confidence) best.set(c.task_id, c);
}
// a task-less claim next to a good one would make the service ask the sender needlessly: drop it
list = best.size ? [...best.values()] : list.slice(0, 1);
// 3. nothing usable -> still log the message
if (!list.length) list = [{ task_id: null, kind: 'irrelevant', confidence: 1, finish: null, days_left: null, detail: null, why: 'no task content' }];

const updates = list.map((c, i) => {
  const finish = c.finish ?? (c.days_left != null ? addDays(ctx.today, c.days_left) : null);
  return {
    message_id: `${norm.base_id}#${i + 1}`,
    task_id: c.task_id,
    kind: c.kind,
    confidence: c.confidence,
    reported_on: ctx.today,
    new_forecast_finish: ['delayed', 'progress', 'started'].includes(c.kind) ? finish : null,
    blocker: ['delayed', 'blocked', 'unblocked'].includes(c.kind) ? c.detail : null,
    change_description: c.kind === 'modified' ? c.detail : null,
    rationale: c.why,
    model: 'n8n-gpt5mini-v2',
  };
});
return [{ json: { sender_id: norm.sender_id, text: norm.text, received_at: norm.received_at,
                  channel: 'telegram', chat_ref: norm.chat_ref, updates } }];
