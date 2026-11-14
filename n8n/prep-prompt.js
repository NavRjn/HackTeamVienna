// n8n Code node "Prep prompt" (Run Once for All Items). Sits between "Context" (HTTP /context) and the LLM.
// The model copies dates from this table instead of doing weekday arithmetic (weak spot of small models).
const ctx = $('Context').first().json;
const start = new Date(ctx.today + 'T00:00:00Z');
const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const calendar = [];
for (let i = 0; i < 15; i++) {
  const d = new Date(start); d.setUTCDate(d.getUTCDate() + i);
  calendar.push(`${days[d.getUTCDay()]} ${d.toISOString().slice(0, 10)}${i === 0 ? ' (today)' : ''}`);
}
const brief = (t) => `${t.id} | ${t.name} | ${t.status} | plan ${t.planned_start}..${t.planned_finish}` +
  (t.forecast_finish ? ` | forecast ${t.forecast_finish}` : '');
return [{ json: {
  calendar: calendar.join('\n'),
  tasks: ctx.tasks.map(brief).join('\n') || '(none)',
  related: ctx.related_tasks.map(brief).join('\n') || '(none)',
  sender: `${ctx.person.name}, ${ctx.person.trade ?? ctx.person.role}, ${ctx.person.company ?? ''}`,
} }];
