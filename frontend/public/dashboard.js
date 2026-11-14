import { dayDiff, ordinal, human, statusText, isDelayed, readState, readEvents } from './data.js';
import { graph } from './graph.js';

const $ = id => document.getElementById(id);
const node = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
};
const field = (label, value) => {
  const pair = node('div', 'field');
  pair.append(node('dt', '', label), node('dd', '', value ?? '—'));
  return pair;
};
const date = value => value ? new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeZone: 'UTC' })
  .format(new Date(value + 'T00:00:00Z')) : '—';
const time = value => new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));
const taskLink = (id, label) => {
  const button = node('button', 'task-link', label);
  button.addEventListener('click', () => chooseTask(id));
  return button;
};
const filters = ['location', 'trade', 'status', 'assignee', 'severity'];
let state, stateSignature, eventSignature, graphView;
let events = [];
let view = 'graph';
let selected;
let selectedIssue;
let issuesById = new Map();
let tasksById = new Map();

function chooseTask(id, issueId) {
  selected = id;
  selectedIssue = issueId;
  render();
}
function chooseView(next) {
  if (view === next) return;
  graphView = undefined;
  view = next;
  for (const button of $('views').querySelectorAll('button')) {
    if (button.dataset.view === view) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  $('filters').hidden = view === 'activity';
  $('severity-filter').hidden = view !== 'issues';
  render();
}
$('views').addEventListener('click', event => {
  const button = event.target.closest('button[data-view]');
  if (button) chooseView(button.dataset.view);
});
for (const id of filters) $(id).addEventListener('change', render);
$('search').addEventListener('input', render);

function choices(id, values, label = human) {
  const select = $(id);
  const previous = select.value;
  select.replaceChildren(new Option('All', ''));
  for (const value of [...new Set(values.filter(Boolean))].sort()) select.add(new Option(label(value), value));
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
}
function installState(project) {
  state = project;
  tasksById = new Map(project.tasks.map(t => [t.id, t]));
  issuesById = new Map(project.open_discrepancies.map(issue => [issue.id, issue]));
  if (!tasksById.has(selected)) selected = undefined;
  if (!issuesById.has(selectedIssue)) selectedIssue = undefined;
  $('project').textContent = state.name;
  $('data-date').textContent = 'Schedule date · ' + date(state.today);
  choices('location', state.tasks.map(t => t.location_id), value => value);
  choices('trade', state.tasks.map(t => t.trade));
  choices('status', state.tasks.map(t => t.status), statusText);
  choices('assignee', state.tasks.map(t => t.responsible_person_id), value => value);
  choices('severity', ['critical', 'warning', 'info']);
  const planned = state.project_finish_planned;
  const forecast = state.project_finish_forecast;
  const delta = planned && forecast ? dayDiff(planned, forecast) : null;
  const shift = delta === null ? 'Forecast comparison unavailable' : delta > 0 ? '+' + delta + ' days late'
    : delta < 0 ? Math.abs(delta) + ' days early' : 'On plan';
  $('handover').replaceChildren(node('p', 'eyebrow', 'PROJECT HANDOVER'), node('strong', 'forecast', date(forecast)),
    node('p', 'muted', 'Planned · ' + date(planned)), node('span', 'pill ' + (delta > 0 ? 'warning' : 'info'), shift));
  const active = state.tasks.filter(t => t.status !== 'cancelled');
  const progress = active.length ? Math.round(active.reduce((sum, t) => sum + t.percent_complete, 0) / active.length) + '%' : '—';
  $('metrics').replaceChildren(...[
    ['Task progress', progress, 'Average of active task completion'],
    ['Completed', active.filter(t => t.status === 'done').length],
    ['In progress', active.filter(t => t.status === 'in_progress').length],
    ['Blocked', active.filter(t => t.status === 'blocked').length],
    ['Not started', active.filter(t => t.status === 'not_started').length],
    ['Open issues', state.open_discrepancies.filter(issue => issue.status === 'open').length],
  ].map(([label, value, description]) => {
    const card = node('div', 'metric');
    card.append(node('p', '', label), node('strong', '', value));
    if (description) card.append(node('small', 'muted', description));
    return card;
  }));
  $('workspace').hidden = false;
}

function matches(task) {
  const query = $('search').value.toLowerCase().trim();
  return (!query || [task.name, task.id].some(value => value.toLowerCase().includes(query)))
    && (!$('location').value || $('location').value === task.location_id)
    && (!$('trade').value || $('trade').value === task.trade)
    && (!$('status').value || $('status').value === task.status)
    && (!$('assignee').value || $('assignee').value === task.responsible_person_id);
}
function issueCard(issue) {
  const card = node('article', 'issue-card ' + issue.severity);
  const head = node('div', 'card-head');
  head.append(node('span', 'pill ' + issue.severity, human(issue.severity)), node('h3', '', human(issue.type)));
  card.append(head, node('p', '', issue.description), node('small', 'muted', time(issue.detected_at) + ' · ' + human(issue.status)));
  if (issue.impact) {
    const impact = node('div', 'impact');
    const { affected_task_ids: ids, delay_days: delay, project_finish_before: before, project_finish_after: after,
      critical_path_affected: critical } = issue.impact;
    impact.append(node('p', '', delay + 'd task delay · ' + ids.length + ' affected tasks' + (critical ? ' · Critical path affected' : '')));
    if (before && after) impact.append(node('p', '', 'Handover · ' + date(before) + ' → ' + date(after)));
    card.append(impact);
  }
  if (tasksById.has(issue.task_id)) {
    const button = node('button', 'outline', 'Inspect impact');
    button.addEventListener('click', () => {
      selected = issue.task_id;
      selectedIssue = issue.id;
      chooseView('graph');
    });
    card.append(button);
  }
  return card;
}

function renderDetail(impact) {
  const pane = $('detail');
  const task = tasksById.get(selected);
  pane.replaceChildren(node('h2', '', 'Task details'));
  if (!task) {
    pane.append(node('p', 'muted', 'Select a task to inspect its dates and dependencies.'));
    return;
  }
  const close = node('button', 'outline clear', 'Clear selection');
  close.addEventListener('click', () => chooseTask(undefined));
  pane.append(close, node('p', 'eyebrow', task.id), node('h3', '', task.name),
    node('span', 'pill ' + (isDelayed(task) ? 'warning' : 'info'), statusText(task.status) + (isDelayed(task) ? ' · Delayed' : '')));
  const facts = node('dl', 'facts');
  facts.append(field('Trade', human(task.trade)), field('Location ID', task.location_id),
    field('Assignee ID', task.responsible_person_id), field('Completion', task.percent_complete + '%'),
    field('Planned', date(task.planned_start) + ' → ' + date(task.planned_finish)),
    field('Computed', date(task.cpm_start) + ' → ' + date(task.cpm_finish)),
    field('Actual', date(task.actual_start) + ' → ' + date(task.actual_finish)),
    field('Reported finish', date(task.forecast_finish)));
  if (task.hold_point) facts.append(field('Inspection', 'Hold point'));
  if (task.drawing_refs.length) facts.append(field('Drawings', task.drawing_refs.join(', ')));
  pane.append(facts);
  for (const [label, from, to] of [
    ['Predecessors', 'successor_id', 'predecessor_id'], ['Successors', 'predecessor_id', 'successor_id'],
  ]) {
    const dependencies = state.dependencies.filter(edge => edge[from] === task.id);
    pane.append(node('h4', '', label));
    if (!dependencies.length) pane.append(node('p', 'muted', 'None'));
    for (const dep of dependencies) {
      const row = node('div', 'relation');
      row.append(taskLink(dep[to], tasksById.get(dep[to]).name), node('small', 'muted', dep.type + ' · ' + dep.lag_days + 'd'));
      if (dep.reason) row.append(node('small', 'muted', dep.reason));
      pane.append(row);
    }
  }
  if (impact.size) {
    pane.append(node('h4', '', 'Affected tasks'));
    for (const id of impact) {
      if (tasksById.has(id)) pane.append(taskLink(id, tasksById.get(id).name));
      else pane.append(node('p', 'muted', id));
    }
  }
}
function renderSchedule(tasks) {
  const panel = node('div', 'schedule');
  panel.append(node('p', 'muted', 'Dark bars show planned dates. Color shows backend-computed dates. Missing computed dates are shown as —.'));
  if (!tasks.length) return panel;
  const min = Math.min(...tasks.flatMap(t => [t.planned_start, t.cpm_start].filter(Boolean).map(ordinal)));
  const max = Math.max(...tasks.flatMap(t => [t.planned_finish, t.cpm_finish].filter(Boolean).map(ordinal)));
  const days = Math.max(1, max - min + 1);
  function bar(start, finish, type) {
    if (!start || !finish) return node('div', 'bar-missing', '—');
    const track = node('div', 'track');
    const segment = node('span', 'bar ' + type);
    segment.style.left = ((ordinal(start) - min) * 100 / days) + '%';
    segment.style.width = (Math.max(1, dayDiff(start, finish) + 1) * 100 / days) + '%';
    track.append(segment);
    return track;
  }
  const range = node('div', 'schedule-range');
  range.append(node('span', '', date(new Date(min * 86400000).toISOString().slice(0, 10))),
    node('span', '', date(new Date(max * 86400000).toISOString().slice(0, 10))));
  panel.append(range);
  for (const task of [...tasks].sort((a, b) => a.planned_start.localeCompare(b.planned_start))) {
    const row = node('div', 'schedule-row' + (task.id === selected ? ' active' : ''));
    const heading = node('div');
    heading.append(taskLink(task.id, task.name), node('small', 'muted', task.location_id + ' · ' + statusText(task.status)));
    const bars = node('div');
    bars.append(bar(task.planned_start, task.planned_finish, 'planned'),
      bar(task.cpm_start, task.cpm_finish, isDelayed(task) ? 'late' : 'forecast-bar'));
    const dates = node('div', 'dates muted');
    dates.append(node('small', '', date(task.planned_start) + ' → ' + date(task.planned_finish)),
      node('small', '', date(task.cpm_start) + ' → ' + date(task.cpm_finish)));
    row.append(heading, bars, dates);
    panel.append(row);
  }
  return panel;
}
function renderActivity() {
  const list = node('div', 'event-list');
  if (!events.length) list.append(node('p', 'muted', 'No events have been recorded.'));
  for (const event of [...events].sort((a, b) => (b.id ?? 0) - (a.id ?? 0) || Date.parse(b.ts) - Date.parse(a.ts))) {
    const entry = node('article', 'event');
    entry.append(node('h3', '', human(event.kind)), node('p', 'muted', time(event.ts) + ' · ' + event.actor + ' · #' + (event.id ?? '—')));
    if (tasksById.has(event.task_id)) entry.append(taskLink(event.task_id, tasksById.get(event.task_id).name));
    const summary = event.payload.text ?? event.payload.discrepancy?.description ?? event.payload.update?.rationale;
    if (typeof summary === 'string' && summary) entry.append(node('p', 'raw-text', summary));
    const raw = node('details');
    raw.dataset.event = event.id;
    raw.append(node('summary', '', 'Event record'), node('pre', '', JSON.stringify(event, null, 2)));
    entry.append(raw);
    list.append(entry);
  }
  return list;
}
function render() {
  if (!state) return;
  const panel = $('view');
  const oldOpen = new Set([...panel.querySelectorAll('details[open]')].map(element => element.dataset.event));
  const relevant = state.tasks.filter(matches);
  const visible = new Set(relevant.map(t => t.id));
  const impactIssues = selectedIssue ? [issuesById.get(selectedIssue)].filter(Boolean)
    : state.open_discrepancies.filter(issue => selected && issue.task_id === selected && issue.status !== 'resolved');
  const impact = new Set(impactIssues.flatMap(issue => issue.impact?.affected_task_ids || []));
  renderDetail(impact);
  if (view === 'graph') {
    const camera = graphView?.camera();
    const header = node('div', 'view-head');
    header.append(node('h2', '', 'Dependency graph'), node('p', 'muted', relevant.length + ' of ' + state.tasks.length
      + ' tasks · lanes by location ID · green outlines mark affected tasks'));
    const flagged = new Set(state.open_discrepancies.filter(issue => issue.status === 'open').map(issue => issue.task_id));
    graphView = graph(state.tasks, state.dependencies, visible, selected, impact, flagged, chooseTask);
    panel.replaceChildren(header);
    panel.append(graphView.element);
    if (selectedIssue && impactIssues[0]) panel.append(issueCard(impactIssues[0]));
    if (camera) graphView.restore(camera);
    else if (state.tasks.length) graphView.fit();
  } else if (view === 'schedule') {
    panel.replaceChildren(node('h2', '', 'Planned vs computed schedule'), renderSchedule(relevant));
  } else if (view === 'issues') {
    const issues = state.open_discrepancies.filter(issue =>
      (!issue.task_id || visible.has(issue.task_id)) && (!$('severity').value || issue.severity === $('severity').value));
    const rank = { critical: 0, warning: 1, info: 2 };
    issues.sort((a, b) => rank[a.severity] - rank[b.severity] || Date.parse(b.detected_at) - Date.parse(a.detected_at));
    panel.replaceChildren(node('h2', '', 'Issues'), ...issues.map(issueCard));
    if (!issues.length) panel.append(node('p', 'muted', 'No issues match these filters.'));
  } else {
    panel.replaceChildren(node('h2', '', 'Activity'), renderActivity());
    for (const detail of panel.querySelectorAll('details')) detail.open = oldOpen.has(detail.dataset.event);
  }
}

async function api(path, validate) {
  const response = await fetch(path, { signal: AbortSignal.timeout(6000), cache: 'no-store' });
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body?.detail === 'string' ? body.detail : 'Dashboard API returned HTTP ' + response.status + '.');
  return validate(body);
}
async function poll() {
  let stateChanged = false;
  let eventsChanged = false;
  const [project, audit] = await Promise.allSettled([api('/api/state', readState), api('/api/events', readEvents)]);
  const failures = [];
  if (project.status === 'fulfilled') {
    const signature = JSON.stringify(project.value);
    if (signature !== stateSignature) {
      installState(project.value);
      stateSignature = signature;
      stateChanged = true;
    }
  } else failures.push('Project · ' + project.reason.message);
  if (audit.status === 'fulfilled') {
    const signature = JSON.stringify(audit.value);
    if (signature !== eventSignature) {
      events = audit.value;
      eventSignature = signature;
      eventsChanged = true;
    }
  } else failures.push('Activity · ' + audit.reason.message);
  const label = project.status === 'rejected' ? 'Disconnected' : failures.length ? 'Activity disconnected' : 'Live API';
  if ($('connection').textContent !== label) $('connection').textContent = label;
  $('connection').dataset.live = String(!failures.length);
  const notice = $('network-error');
  notice.hidden = !failures.length;
  const message = failures.join(' · ') + (state && failures.length ? ' Some displayed data may be stale.' : '');
  if (notice.textContent !== message) notice.textContent = message;
  if (stateChanged || (view === 'activity' && eventsChanged)) render();
  setTimeout(poll, 2000);
}
poll();
