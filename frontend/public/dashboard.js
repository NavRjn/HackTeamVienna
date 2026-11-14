import { dayDiff, ordinal, human, statusText, isDelayed, locationGroup, readState, readEvents } from './data.js';
import { graph } from './graph.js';
import { initBimViewer, openIfcFile } from './bim-viewer.js';

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
let actions = [];
let actionSignature;
let view = 'graph';
let selected;
let selectedIssue;
let graphScope;
let bimStarted = false;
let documents = [];
let documentPreviewUrl;
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
  $('filters').hidden = ['activity', 'bim', 'documents'].includes(view);
  $('severity-filter').hidden = view !== 'issues';
  $('view').hidden = view === 'bim' || view === 'documents';
  $('detail').hidden = view === 'bim' || view === 'documents';
  $('bim-view').hidden = view !== 'bim';
  $('documents-view').hidden = view !== 'documents';
  if (view === 'bim' && !bimStarted) {
    bimStarted = true;
    try {
      initBimViewer();
    } catch (error) {
      const status = $('bim-status');
      status.textContent = 'Could not start the IFC viewer: ' + (error.message || 'WebGL is unavailable.');
      status.dataset.state = 'error';
    }
  }
  if (view === 'documents') void loadDocuments();
  render();
}
$('views').addEventListener('click', event => {
  const button = event.target.closest('button[data-view]');
  if (button) chooseView(button.dataset.view);
});
for (const id of filters) $(id).addEventListener('change', render);
$('search').addEventListener('input', render);
$('document-search').addEventListener('input', renderDocuments);
$('document-sort').addEventListener('change', renderDocuments);
$('document-form').elements.category.addEventListener('change', event => {
  const accepts = {
    bim_models: '.ifc', drawings_plans: '.pdf,.jpg,.jpeg,.png', schedules: '.csv,.xlsx',
    financial_documents: '.pdf',
  };
  $('document-file').accept = accepts[event.target.value];
});
$('document-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const file = $('document-file').files[0];
  if (!file) return;
  const status = $('document-status');
  status.dataset.state = 'loading';
  status.textContent = 'Uploading ' + file.name + '…';
  try {
    const response = await fetch('/api/documents', { method: 'POST', body: new FormData(form) });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'Upload failed.');
    form.reset();
    $('document-file').accept = '.ifc';
    status.dataset.state = 'success';
    status.textContent = 'Uploaded ' + body.original_filename + (body.revision_label ? ' · ' + body.revision_label : '');
    await loadDocuments();
  } catch (error) {
    status.dataset.state = 'error';
    status.textContent = 'Upload failed: ' + error.message;
  }
});

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
  const scopes = [...new Set(project.tasks.map(task => locationGroup(task.location_id)))];
  if (!scopes.includes(graphScope)) graphScope = scopes.includes('T6') ? 'T6' : scopes.includes('T5') ? 'T5' : '';
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
  const deltaLabel = delta === null ? '—' : delta > 0 ? '+' + delta + 'd' : delta < 0 ? '−' + Math.abs(delta) + 'd' : '0d';
  const handoverDates = node('div', 'handover-dates');
  handoverDates.append(node('span', 'muted', 'PLANNED'), node('strong', '', date(planned)),
    node('span', 'muted', 'FORECAST'), node('strong', '', date(forecast)));
  $('handover').replaceChildren(node('p', 'eyebrow', 'PROJECT HANDOVER'),
    node('strong', 'handover-shift ' + (delta > 0 ? 'late' : delta < 0 ? 'early' : ''), deltaLabel),
    node('span', 'handover-shift-copy', shift), handoverDates);
  const active = state.tasks.filter(t => t.status !== 'cancelled');
  const progress = active.length ? Math.round(active.reduce((sum, t) => sum + t.percent_complete, 0) / active.length) + '%' : '—';
  const critical = state.open_discrepancies.filter(issue => issue.status === 'open' && issue.severity === 'critical').length;
  $('metrics').replaceChildren(...[
    ['Progress', progress, 'Mean completion · active tasks'],
    ['Completed', active.filter(t => t.status === 'done').length],
    ['In progress', active.filter(t => t.status === 'in_progress').length],
    ['Blocked', active.filter(t => t.status === 'blocked').length],
    ['Not started', active.filter(t => t.status === 'not_started').length],
    ['Critical issues', critical],
  ].map(([label, value, description]) => {
    const card = node('div', 'metric');
    card.append(node('p', '', label), node('strong', '', value));
    if (description) card.append(node('small', 'muted', description));
    return card;
  }));
  $('loading').hidden = true;
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
  const legend = node('div', 'schedule-legend');
  legend.append(node('span', '', 'Planned'), node('span', '', 'Backend forecast'));
  panel.append(node('p', 'muted', 'Baseline and computed dates from the project state.'), legend);
  if (!tasks.length) return panel;
  const min = Math.min(...tasks.flatMap(t => [t.planned_start, t.cpm_start].filter(Boolean).map(ordinal)));
  const max = Math.max(...tasks.flatMap(t => [t.planned_finish, t.cpm_finish].filter(Boolean).map(ordinal)));
  const days = Math.max(1, max - min + 1);
  const ruler = node('div', 'date-ruler');
  for (let offset = 0; offset < days; offset += 7) {
    const marker = node('span', '', date(new Date((min + offset) * 86400000).toISOString().slice(0, 10)));
    marker.style.left = (offset * 100 / days) + '%';
    ruler.append(marker);
  }
  function bar(start, finish, type) {
    if (!start || !finish) return node('div', 'bar-missing', '—');
    const track = node('div', 'track');
    track.style.backgroundSize = Math.max(7 * 100 / days, 0.5) + '% 100%';
    const segment = node('span', 'bar ' + type);
    segment.style.left = ((ordinal(start) - min) * 100 / days) + '%';
    segment.style.width = (Math.max(1, dayDiff(start, finish) + 1) * 100 / days) + '%';
    track.append(segment);
    return track;
  }
  const range = node('div', 'schedule-range');
  range.append(node('span', '', date(new Date(min * 86400000).toISOString().slice(0, 10))),
    node('span', '', date(new Date(max * 86400000).toISOString().slice(0, 10))));
  const scheduleHead = node('div', 'schedule-head');
  scheduleHead.append(node('span', '', 'TASK'), ruler, node('span', '', 'PLAN / FORECAST'));
  panel.append(scheduleHead, range);
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
    const finishShift = task.cpm_finish ? dayDiff(task.planned_finish, task.cpm_finish) : null;
    if (finishShift !== null && finishShift !== 0) {
      dates.append(node('span', 'schedule-shift ' + (finishShift > 0 ? 'late' : 'early'),
        (finishShift > 0 ? '+' : '−') + Math.abs(finishShift) + 'd finish shift'));
    }
    row.append(heading, bars, dates);
    panel.append(row);
  }
  return panel;
}
function renderActivity() {
  const list = node('div', 'event-list');
  if (!events.length && !actions.length) {
    const empty = node('div', 'empty-state');
    empty.append(node('strong', '', 'No activity recorded'),
      node('span', 'muted', 'No audit events have been returned by the backend.'));
    list.append(empty);
  }
  if (actions.length) {
    list.append(node('h3', 'notification-heading', 'Notification status'));
    const statusTone = { drafted: 'warning', sent: 'info', failed: 'critical' };
    for (const action of [...actions].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))) {
      const entry = node('article', 'event notification-event');
      const heading = node('div', 'notification-head');
      heading.append(node('h3', '', human(action.kind)), node('span', 'pill ' + statusTone[action.status], human(action.status)));
      entry.append(heading, node('p', 'muted', time(action.created_at) + ' · ' + action.recipient.name),
        node('p', '', action.text));
      if (action.detail) entry.append(node('small', 'muted', action.detail));
      list.append(entry);
    }
  }
  for (const event of [...events].sort((a, b) => (a.id ?? 0) - (b.id ?? 0) || Date.parse(a.ts) - Date.parse(b.ts))) {
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
  if (!state || view === 'bim') return;
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
    const title = node('div');
    title.append(node('h2', '', 'Dependency graph'), node('p', 'muted', relevant.length + ' of ' + state.tasks.length
      + ' tasks match filters · impact IDs are backend-reported'));
    const focusLabel = node('label', 'graph-focus', 'LOCATION FOCUS');
    const focus = node('select');
    focus.setAttribute('aria-label', 'Graph location focus');
    focus.add(new Option('All locations', ''));
    for (const group of [...new Set(state.tasks.map(task => locationGroup(task.location_id)))].sort()) {
      focus.add(new Option(group + ' locations', group));
    }
    focus.value = graphScope;
    focus.addEventListener('change', () => { graphScope = focus.value; render(); });
    focusLabel.append(focus);
    header.append(title, focusLabel);
    const flagged = new Set(state.open_discrepancies.filter(issue => issue.status === 'open').map(issue => issue.task_id));
    graphView = graph(state.tasks, state.dependencies, visible, selected, impact, flagged, chooseTask, graphScope);
    panel.replaceChildren(header);
    panel.append(graphView.element);
    if (selectedIssue && impactIssues[0]) panel.append(issueCard(impactIssues[0]));
    if (camera && camera.scope === graphScope) graphView.restore(camera);
    else if (state.tasks.length) graphView.fit();
  } else if (view === 'schedule') {
    panel.replaceChildren(node('h2', '', 'Planned vs computed schedule'), renderSchedule(relevant));
  } else if (view === 'issues') {
    const issues = state.open_discrepancies.filter(issue =>
      (!issue.task_id || visible.has(issue.task_id)) && (!$('severity').value || issue.severity === $('severity').value));
    const rank = { critical: 0, warning: 1, info: 2 };
    issues.sort((a, b) => rank[a.severity] - rank[b.severity] || Date.parse(b.detected_at) - Date.parse(a.detected_at));
    panel.replaceChildren(node('h2', '', 'Issues'), ...issues.map(issueCard));
    if (!issues.length) {
      const empty = node('div', 'empty-state');
      empty.append(node('strong', '', state.open_discrepancies.length ? 'No issues match these filters' : 'No open discrepancies'),
        node('span', 'muted', state.open_discrepancies.length
          ? 'Try another task or severity filter.'
          : 'The backend has not reported any open issues.'));
      panel.append(empty);
    }
  } else {
    panel.replaceChildren(node('h2', '', 'Activity'), renderActivity());
    for (const detail of panel.querySelectorAll('details')) detail.open = oldOpen.has(detail.dataset.event);
  }
}

const documentCategories = {
  bim_models: 'BIM Models', drawings_plans: 'Drawings & Plans', schedules: 'Schedules',
  financial_documents: 'Financial Documents',
};
const fileSize = bytes => bytes < 1024 * 1024 ? Math.ceil(bytes / 1024) + ' KB'
  : (bytes / (1024 * 1024)).toFixed(1) + ' MB';
async function loadDocuments() {
  const status = $('document-status');
  if (!documents.length) {
    status.dataset.state = 'loading';
    status.textContent = 'Loading document library…';
  }
  try {
    const response = await fetch('/api/documents', { cache: 'no-store' });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'Could not load documents.');
    documents = body;
    if (status.dataset.state === 'loading') {
      status.dataset.state = '';
      status.textContent = 'Maximum file size: 100 MB.';
    }
    renderDocuments();
  } catch (error) {
    status.dataset.state = 'error';
    status.textContent = 'Could not load documents: ' + error.message;
  }
}
function renderDocuments() {
  const query = $('document-search').value.trim().toLowerCase();
  const sort = $('document-sort').value;
  const entries = documents.filter(item => [item.original_filename, item.revision_label, item.location_ref, item.task_ref]
    .some(value => value?.toLowerCase().includes(query)) || !query);
  entries.sort((a, b) => sort === 'oldest' ? a.uploaded_at.localeCompare(b.uploaded_at)
    : sort === 'name' ? a.original_filename.localeCompare(b.original_filename)
      : sort === 'category' ? documentCategories[a.category].localeCompare(documentCategories[b.category])
        : b.uploaded_at.localeCompare(a.uploaded_at));
  $('document-count').textContent = '(' + entries.length + ')';
  const list = $('document-list');
  list.replaceChildren();
  if (!entries.length) {
    list.append(node('p', 'muted document-empty', documents.length ? 'No documents match this search.' : 'No documents uploaded yet.'));
    return;
  }
  for (const item of entries) {
    const row = node('article', 'document-row');
    const info = node('div');
    info.append(node('strong', '', item.original_filename),
      node('small', 'muted', documentCategories[item.category] + ' · ' + fileSize(item.file_size) + ' · ' + time(item.uploaded_at)));
    const refs = [item.revision_label, item.location_ref, item.task_ref].filter(Boolean).join(' · ');
    if (refs) info.append(node('small', 'muted', refs));
    const actions = node('div', 'document-actions');
    if (/\.(pdf|jpe?g|png)$/i.test(item.original_filename)) {
      const preview = node('button', 'outline', 'Preview');
      preview.addEventListener('click', () => void previewDocument(item));
      actions.append(preview);
    }
    const download = node('button', 'outline', 'Download');
    download.addEventListener('click', () => void downloadDocument(item));
    actions.append(download);
    if (/\.ifc$/i.test(item.original_filename)) {
      const open = node('button', 'outline', 'Open in 3D');
      open.addEventListener('click', () => void openStoredIfc(item));
      actions.append(open);
    }
    row.append(info, actions);
    list.append(row);
  }
}
async function getDocumentBlob(item, inline = false) {
  const response = await fetch('/api/documents/' + item.id + '/content' + (inline ? '?inline=true' : ''), { cache: 'no-store' });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || 'Document request failed.');
  }
  return response.blob();
}
async function previewDocument(item) {
  try {
    const blob = await getDocumentBlob(item, true);
    if (documentPreviewUrl) URL.revokeObjectURL(documentPreviewUrl);
    documentPreviewUrl = URL.createObjectURL(blob);
    const pane = $('document-preview');
    pane.replaceChildren(node('h2', '', item.original_filename));
    if (/\.pdf$/i.test(item.original_filename)) {
      const frame = node('iframe', 'document-frame');
      frame.src = documentPreviewUrl;
      frame.title = item.original_filename;
      pane.append(frame);
    } else {
      const image = node('img', 'document-image');
      image.src = documentPreviewUrl;
      image.alt = item.original_filename;
      pane.append(image);
    }
  } catch (error) {
    $('document-preview').replaceChildren(node('h2', '', 'Preview unavailable'), node('p', 'muted', error.message));
  }
}
async function downloadDocument(item) {
  try {
    const blob = await getDocumentBlob(item);
    const url = URL.createObjectURL(blob);
    const link = node('a');
    link.href = url;
    link.download = item.original_filename;
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    $('document-status').dataset.state = 'error';
    $('document-status').textContent = 'Download failed: ' + error.message;
  }
}
async function openStoredIfc(item) {
  try {
    const blob = await getDocumentBlob(item);
    const file = new File([blob], item.original_filename, { type: blob.type || 'application/x-step' });
    chooseView('bim');
    openIfcFile(file);
  } catch (error) {
    $('document-status').dataset.state = 'error';
    $('document-status').textContent = 'Could not open IFC: ' + error.message;
  }
}

async function api(path, validate) {
  const response = await fetch(path, { signal: AbortSignal.timeout(6000), cache: 'no-store' });
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body?.detail === 'string' ? body.detail : 'Dashboard API returned HTTP ' + response.status + '.');
  return validate(body);
}
function readActions(value) {
  if (!Array.isArray(value) || !value.every(action => action && typeof action.id === 'string'
      && typeof action.kind === 'string' && typeof action.text === 'string'
      && ['drafted', 'sent', 'failed'].includes(action.status)
      && typeof action.created_at === 'string' && action.recipient && typeof action.recipient.name === 'string')) {
    throw new Error('Unexpected notification status response from the backend.');
  }
  return value;
}
async function poll() {
  let stateChanged = false;
  let eventsChanged = false;
  let actionsChanged = false;
  const [project, audit, outbox] = await Promise.allSettled([
    api('/api/state', readState), api('/api/events', readEvents), api('/api/actions', readActions),
  ]);
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
  if (outbox.status === 'fulfilled') {
    const signature = JSON.stringify(outbox.value);
    if (signature !== actionSignature) {
      actions = outbox.value;
      actionSignature = signature;
      actionsChanged = true;
    }
  } else failures.push('Notifications · ' + outbox.reason.message);
  const label = project.status === 'rejected'
    ? state ? 'API disconnected · stale' : 'API disconnected'
    : failures.length ? 'API partial · some data unavailable' : 'API live';
  if ($('connection').textContent !== label) $('connection').textContent = label;
  $('connection').dataset.live = String(project.status === 'fulfilled');
  if (!state && project.status === 'rejected') $('loading').textContent = 'Project state is unavailable. Check the dashboard API connection.';
  const notice = $('network-error');
  notice.hidden = !failures.length;
  const message = failures.join(' · ') + (state && failures.length ? ' Some displayed data may be stale.' : '');
  if (notice.textContent !== message) notice.textContent = message;
  if (stateChanged || (view === 'activity' && (eventsChanged || actionsChanged))) render();
  setTimeout(poll, 2000);
}
poll();
