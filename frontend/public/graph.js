import { graphRanks, isDelayed, statusText } from './data.js';

const ns = 'http://www.w3.org/2000/svg';
function svg(name, attributes = {}, text) {
  const node = document.createElementNS(ns, name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

export function graph(tasks, edges, visible, selected, impacted, flagged, select, scope = '') {
  const inScope = task => !scope || task.location_id === scope || task.location_id.startsWith(scope + '-');
  const included = new Set(tasks.filter(inScope).map(task => task.id));
  for (const id of impacted) included.add(id);
  if (selected) included.add(selected);
  const seeds = new Set([...impacted, selected].filter(Boolean));
  for (const edge of edges) {
    if (seeds.has(edge.predecessor_id)) included.add(edge.successor_id);
    if (seeds.has(edge.successor_id)) included.add(edge.predecessor_id);
  }
  const shownTasks = tasks.filter(task => included.has(task.id));
  const graphEdges = edges.filter(edge => included.has(edge.predecessor_id) && included.has(edge.successor_id));
  const ranking = graphRanks(shownTasks, graphEdges);
  const laneFor = task => scope && !inScope(task) ? 'Context' : scope || task.location_id;
  const locations = [...new Set(shownTasks.map(laneFor))].sort((a, b) =>
    a === 'Context' ? 1 : b === 'Context' ? -1 : a.localeCompare(b));
  const positions = new Map();
  const bands = [];
  let top = 20;
  for (const location of locations) {
    const counts = new Map();
    const laneTasks = shownTasks.filter(task => laneFor(task) === location);
    for (const task of laneTasks) {
      const col = ranking.get(task.id);
      const row = counts.get(col) || 0;
      counts.set(col, row + 1);
      positions.set(task.id, scope
        ? [32 + row * 282, top + 43 + col * 116]
        : [32 + col * 310, top + 43 + row * 116]);
    }
    const depth = counts.size ? Math.max(...counts.keys()) + 1 : 1;
    const height = 58 + (scope ? depth : Math.max(1, ...counts.values())) * 116;
    bands.push([location, top, height]);
    top += height + 16;
  }
  const maxParallel = Math.max(1, ...locations.map(location => {
    const rows = new Map();
    for (const task of shownTasks.filter(item => laneFor(item) === location)) {
      const rank = ranking.get(task.id);
      rows.set(rank, (rows.get(rank) || 0) + 1);
    }
    return Math.max(1, ...rows.values());
  }));
  const width = scope ? 45 + maxParallel * 282
    : 45 + (1 + Math.max(0, ...shownTasks.map(task => ranking.get(task.id)))) * 310;
  const wrapper = document.createElement('div');
  wrapper.className = 'graph-wrap';
  const viewport = svg('svg', { class: 'graph-canvas', role: 'group', 'aria-label': 'Task dependency graph' });
  if (scope) viewport.style.height = Math.max(470, Math.min(top + 24, 1100)) + 'px';
  const defs = svg('defs');
  for (const [name, color] of [['arrow', '#87948f'], ['highlight', '#297759']]) {
    const marker = svg('marker', { id: name, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 8, markerHeight: 8, orient: 'auto' });
    marker.append(svg('path', { d: 'M0 0 L10 5 L0 10 Z', fill: color }));
    defs.append(marker);
  }
  viewport.append(defs);
  const scene = svg('g');
  viewport.append(scene);
  const focus = new Set([selected, ...impacted]);
  const neighbors = new Set();
  for (const edge of edges) {
    if (edge.predecessor_id === selected) neighbors.add(edge.successor_id);
    if (edge.successor_id === selected) neighbors.add(edge.predecessor_id);
  }
  const shown = id => included.has(id) && (visible.has(id) || focus.has(id) || neighbors.has(id));
  for (const [location, y, height] of bands) {
    scene.append(svg('rect', { x: 17, y, width: width - 32, height, rx: 10, class: 'lane' }));
    scene.append(svg('text', { x: 33, y: y + 25, class: 'lane-label' },
      location === 'Context' ? 'Adjacent dependencies' : scope ? scope + ' locations' : location));
  }
  for (const edge of edges) {
    if (!included.has(edge.predecessor_id) || !included.has(edge.successor_id)) continue;
    const [ax, ay] = positions.get(edge.predecessor_id);
    const [bx, by] = positions.get(edge.successor_id);
    const focusEdge = (focus.has(edge.predecessor_id) && focus.has(edge.successor_id)) ||
      [edge.predecessor_id, edge.successor_id].includes(selected);
    const a = scope ? ay + 88 : ax + 252;
    const target = scope ? by : bx;
    const middle = (a + target) / 2;
    const line = svg('path', {
      d: scope
        ? 'M ' + (ax + 126) + ' ' + a + ' C ' + (ax + 126) + ' ' + middle + ' ' + (bx + 126) + ' ' + middle + ' ' + (bx + 126) + ' ' + target
        : 'M ' + a + ' ' + (ay + 43) + ' C ' + middle + ' ' + (ay + 43) + ' ' + middle + ' ' + (by + 43) + ' ' + target + ' ' + (by + 43),
      class: focusEdge ? 'edge edge-focus' : 'edge',
      opacity: shown(edge.predecessor_id) && shown(edge.successor_id) ? 1 : 0.18,
      'marker-end': focusEdge ? 'url(#highlight)' : 'url(#arrow)',
    });
    line.append(svg('title', {}, edge.type + ' · ' + edge.lag_days + ' days' + (edge.reason ? ' · ' + edge.reason : '')));
    scene.append(line);
  }
  for (const task of shownTasks) {
    const [x, y] = positions.get(task.id);
    const label = task.hold_point ? 'Hold point · ' : '';
    const status = isDelayed(task) ? 'Delayed · ' + statusText(task.status) : statusText(task.status);
    const classes = ['graph-node', task.status, isDelayed(task) && 'delayed', flagged.has(task.id) && 'flagged',
      task.hold_point && 'holdpoint', selected === task.id && 'selected', impacted.has(task.id) && 'impacted',
      !inScope(task) && 'context-node'].filter(Boolean).join(' ');
    const node = svg('g', { transform: 'translate(' + x + ' ' + y + ')', class: classes,
      tabindex: shown(task.id) ? 0 : -1, role: 'button', 'aria-label': task.name + ', ' + label + status,
      'aria-pressed': String(selected === task.id), opacity: shown(task.id) ? 1 : 0.24 });
    node.append(svg('title', {}, task.name + ' · ' + task.location_id),
      svg('rect', { width: 252, height: 88, rx: 5, class: 'node-card' }),
      svg('circle', { cx: 16, cy: 69, r: 5, class: 'node-status' }),
      svg('text', { x: 12, y: 20, class: 'node-id' }, task.id),
      svg('text', { x: 12, y: 43, class: 'node-name' }, task.name.length > 34 ? task.name.slice(0, 33) + '…' : task.name),
      svg('text', { x: 28, y: 73, class: 'node-meta' }, task.location_id + ' · ' + status + ' · ' + task.percent_complete + '%'));
    if (task.hold_point) node.append(svg('rect', { x: 166, y: 7, width: 76, height: 17, rx: 2, class: 'hold-badge' }),
      svg('text', { x: 204, y: 19, 'text-anchor': 'middle', class: 'hold-label' }, 'HOLD POINT'));
    node.addEventListener('click', () => select(task.id));
    node.addEventListener('keydown', event => {
      if (![' ', 'Enter'].includes(event.key)) return;
      event.preventDefault();
      select(task.id);
    });
    scene.append(node);
  }
  let scale = 0.9;
  let x = 0;
  let y = 0;
  const draw = () => scene.setAttribute('transform', 'translate(' + x + ' ' + y + ') scale(' + scale + ')');
  function zoom(factor, atX, atY) {
    const next = Math.max(0.13, Math.min(2, scale * factor));
    x = atX - (atX - x) * next / scale;
    y = atY - (atY - y) * next / scale;
    scale = next;
    draw();
  }
  function fitView() {
    scale = Math.min(viewport.clientWidth / width, viewport.clientHeight / top, 1.15);
    x = (viewport.clientWidth - width * scale) / 2;
    y = (viewport.clientHeight - top * scale) / 2;
    draw();
  }
  function centerView() {
    x = (viewport.clientWidth - width * scale) / 2;
    y = (viewport.clientHeight - top * scale) / 2;
    draw();
  }
  const controls = document.createElement('div');
  controls.className = 'graph-controls';
  for (const [label, action] of [
    ['Zoom in', () => zoom(1.2, viewport.clientWidth / 2, viewport.clientHeight / 2)],
    ['Zoom out', () => zoom(1 / 1.2, viewport.clientWidth / 2, viewport.clientHeight / 2)],
    ['Fit view', fitView],
    ['Center', centerView],
  ]) {
    const button = document.createElement('button');
    button.textContent = label;
    button.addEventListener('click', action);
    controls.append(button);
  }
  viewport.addEventListener('wheel', event => {
    event.preventDefault();
    const rect = viewport.getBoundingClientRect();
    zoom(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX - rect.x, event.clientY - rect.y);
  }, { passive: false });
  let drag;
  viewport.addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.target.closest('.graph-node')) return;
    viewport.setPointerCapture(event.pointerId);
    drag = [event.clientX - x, event.clientY - y];
  });
  viewport.addEventListener('pointermove', event => {
    if (!drag) return;
    x = event.clientX - drag[0];
    y = event.clientY - drag[1];
    draw();
  });
  viewport.addEventListener('lostpointercapture', () => { drag = null; });
  draw();
  wrapper.append(controls, viewport);
  return {
    element: wrapper,
    camera: () => ({ x, y, scale, scope }),
    restore: camera => { ({ x, y, scale } = camera); draw(); },
    fit: fitView,
  };
}
