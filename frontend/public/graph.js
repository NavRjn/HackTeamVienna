import { graphRanks, isDelayed, statusText } from './data.js';

const ns = 'http://www.w3.org/2000/svg';
function svg(name, attributes = {}, text) {
  const node = document.createElementNS(ns, name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

export function graph(tasks, edges, visible, selected, impacted, flagged, select) {
  const ranking = graphRanks(tasks, edges);
  const locations = [...new Set(tasks.map(t => t.location_id))].sort();
  const positions = new Map();
  const bands = [];
  let top = 20;
  for (const location of locations) {
    const counts = new Map();
    for (const task of tasks.filter(t => t.location_id === location)) {
      const col = ranking.get(task.id);
      const row = counts.get(col) || 0;
      counts.set(col, row + 1);
      positions.set(task.id, [32 + col * 245, top + 36 + row * 94]);
    }
    const height = 46 + Math.max(...counts.values()) * 94;
    bands.push([location, top, height]);
    top += height + 12;
  }
  const width = 45 + (1 + Math.max(0, ...ranking.values())) * 245;
  const wrapper = document.createElement('div');
  wrapper.className = 'graph-wrap';
  const viewport = svg('svg', { class: 'graph-canvas', role: 'group', 'aria-label': 'Task dependency graph' });
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
  const shown = id => visible.has(id) || focus.has(id) || neighbors.has(id);
  for (const [location, y, height] of bands) {
    scene.append(svg('rect', { x: 17, y, width: width - 32, height, rx: 10, class: 'lane' }));
    scene.append(svg('text', { x: 33, y: y + 23, class: 'lane-label' }, location));
  }
  for (const edge of edges) {
    const [ax, ay] = positions.get(edge.predecessor_id);
    const [bx, by] = positions.get(edge.successor_id);
    const focusEdge = (focus.has(edge.predecessor_id) && focus.has(edge.successor_id)) ||
      [edge.predecessor_id, edge.successor_id].includes(selected);
    const a = ax + 195;
    const middle = (a + bx) / 2;
    const line = svg('path', {
      d: 'M ' + a + ' ' + (ay + 33) + ' C ' + middle + ' ' + (ay + 33) + ' ' + middle + ' ' + (by + 33) + ' ' + bx + ' ' + (by + 33),
      class: focusEdge ? 'edge edge-focus' : 'edge',
      opacity: shown(edge.predecessor_id) && shown(edge.successor_id) ? 1 : 0.18,
      'marker-end': focusEdge ? 'url(#highlight)' : 'url(#arrow)',
    });
    line.append(svg('title', {}, edge.type + ' · ' + edge.lag_days + ' days' + (edge.reason ? ' · ' + edge.reason : '')));
    scene.append(line);
  }
  for (const task of tasks) {
    const [x, y] = positions.get(task.id);
    const label = task.hold_point ? 'Hold point · ' : '';
    const status = isDelayed(task) ? 'Delayed · ' + statusText(task.status) : statusText(task.status);
    const classes = ['graph-node', task.status, isDelayed(task) && 'delayed', flagged.has(task.id) && 'flagged',
      selected === task.id && 'selected', impacted.has(task.id) && 'impacted'].filter(Boolean).join(' ');
    const node = svg('g', { transform: 'translate(' + x + ' ' + y + ')', class: classes,
      tabindex: shown(task.id) ? 0 : -1, role: 'button', 'aria-label': task.name + ', ' + label + status,
      'aria-pressed': String(selected === task.id), opacity: shown(task.id) ? 1 : 0.24 });
    node.append(svg('title', {}, task.name), svg('rect', { width: 195, height: 66, rx: 9, class: 'node-card' }),
      svg('circle', { cx: 14, cy: 49, r: 4, class: 'node-status' }),
      svg('text', { x: 10, y: 15, class: 'node-id' }, task.id),
      svg('text', { x: 10, y: 31, class: 'node-name' }, task.name.length > 25 ? task.name.slice(0, 24) + '…' : task.name),
      svg('text', { x: 24, y: 53, class: 'node-meta' }, label + status));
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
  const controls = document.createElement('div');
  controls.className = 'graph-controls';
  for (const [label, action] of [
    ['Zoom in', () => zoom(1.2, viewport.clientWidth / 2, viewport.clientHeight / 2)],
    ['Zoom out', () => zoom(1 / 1.2, viewport.clientWidth / 2, viewport.clientHeight / 2)],
    ['Fit graph', () => { scale = Math.min(viewport.clientWidth / width, viewport.clientHeight / top, 1); x = y = 0; draw(); }],
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
    camera: () => ({ x, y, scale }),
    restore: camera => { ({ x, y, scale } = camera); draw(); },
    fit: () => controls.lastElementChild.click(),
  };
}
