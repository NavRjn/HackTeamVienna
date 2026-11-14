export const day = 86400000;
export const ordinal = value => Date.parse(value + 'T00:00:00Z') / day;
export const dayDiff = (first, second) => ordinal(second) - ordinal(first);
export const human = value => value?.replaceAll('_', ' ') ?? '—';
export const statusNames = {
  not_started: 'Not started', in_progress: 'In progress', done: 'Completed',
  blocked: 'Blocked', cancelled: 'Cancelled',
};
export const statusText = status => statusNames[status] ?? human(status);
export const isDelayed = task => task.status !== 'done' && task.status !== 'cancelled'
  && task.cpm_finish && task.cpm_finish > task.planned_finish;
export const locationGroup = id => /^(T\d+)(?:-|$)/.exec(id)?.[1] ?? id;

const record = value => value && typeof value === 'object' && !Array.isArray(value);
const string = value => typeof value === 'string';
const strings = value => Array.isArray(value) && value.every(string);
const date = value => string(value) && /^\d{4}-\d{2}-\d{2}$/.test(value)
  && Number.isFinite(ordinal(value)) && new Date(ordinal(value) * day).toISOString().slice(0, 10) === value;
const timestamp = value => string(value) && Number.isFinite(Date.parse(value));
const nullable = (validator, value) => value === null || validator(value);

export function readState(value) {
  if (!record(value) || !string(value.project_id) || !string(value.name) || !date(value.today) ||
      !nullable(date, value.project_finish_planned) || !nullable(date, value.project_finish_forecast) ||
      !Array.isArray(value.tasks) || !Array.isArray(value.dependencies) || !Array.isArray(value.open_discrepancies)) {
    throw new Error('Unexpected project state from the backend.');
  }
  const taskIds = new Set();
  for (const task of value.tasks) {
    if (!record(task) || ![task.id, task.name, task.location_id, task.responsible_person_id, task.kind].every(string)
        || !nullable(string, task.trade) || !Object.hasOwn(statusNames, task.status)
        || !date(task.planned_start) || !date(task.planned_finish)
        || ![task.actual_start, task.actual_finish, task.forecast_finish, task.cpm_start, task.cpm_finish].every(d => nullable(date, d))
        || !Number.isInteger(task.percent_complete) || task.percent_complete < 0 || task.percent_complete > 100
        || typeof task.hold_point !== 'boolean' || !strings(task.drawing_refs) || taskIds.has(task.id)) {
      throw new Error('Unexpected task data from the backend.');
    }
    taskIds.add(task.id);
  }
  for (const edge of value.dependencies) {
    if (!record(edge) || !string(edge.id) || !taskIds.has(edge.predecessor_id) || !taskIds.has(edge.successor_id)
        || !['FS', 'SS', 'FF', 'SF'].includes(edge.type) || !Number.isFinite(edge.lag_days) || !nullable(string, edge.reason)) {
      throw new Error('Unexpected dependency data from the backend.');
    }
  }
  for (const issue of value.open_discrepancies) {
    if (!record(issue) || ![issue.id, issue.type, issue.status, issue.description].every(string)
        || !['info', 'warning', 'critical'].includes(issue.severity) || !nullable(string, issue.task_id)
        || !timestamp(issue.detected_at) || (issue.impact !== null && (!record(issue.impact)
        || !strings(issue.impact.affected_task_ids) || !Number.isFinite(issue.impact.delay_days)
        || typeof issue.impact.critical_path_affected !== 'boolean'
        || !nullable(date, issue.impact.project_finish_before) || !nullable(date, issue.impact.project_finish_after)))) {
      throw new Error('Unexpected issue data from the backend.');
    }
  }
  return value;
}

export function readEvents(value) {
  if (!Array.isArray(value) || !value.every(event => record(event) && nullable(Number.isInteger, event.id)
      && [event.kind, event.actor].every(string) && timestamp(event.ts) && nullable(string, event.task_id)
      && nullable(string, event.source_message_id) && [event.before, event.after, event.payload].every(record))) {
    throw new Error('Unexpected audit events from the backend.');
  }
  return value;
}

export function graphRanks(tasks, edges) {
  const rank = new Map(tasks.map(task => [task.id, 0]));
  const degree = new Map(rank);
  const next = new Map(tasks.map(task => [task.id, []]));
  for (const edge of edges) {
    next.get(edge.predecessor_id).push(edge.successor_id);
    degree.set(edge.successor_id, degree.get(edge.successor_id) + 1);
  }
  const queue = [...degree.keys()].filter(id => degree.get(id) === 0);
  for (let cursor = 0; cursor < queue.length; cursor++) {
    const id = queue[cursor];
    for (const child of next.get(id)) {
      rank.set(child, Math.max(rank.get(child), rank.get(id) + 1));
      degree.set(child, degree.get(child) - 1);
      if (degree.get(child) === 0) queue.push(child);
    }
  }
  return rank;
}
