import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { dayDiff, graphRanks, locationGroup, readEvents, readState } from '../public/data.js';

const plan = JSON.parse(readFileSync(new URL('../../data/demo/plan.json', import.meta.url)));
const demoState = {
  project_id: plan.project_id,
  name: plan.name,
  today: plan.data_date,
  project_finish_planned: '2026-12-06',
  project_finish_forecast: '2026-12-09',
  tasks: plan.tasks.map(task => ({ ...task, cpm_start: null, cpm_finish: null })),
  dependencies: plan.dependencies,
  open_discrepancies: [],
};

test('demo task dates allow missing computed values and preserve handover delta', () => {
  const state = readState(demoState);
  assert.equal(dayDiff(state.project_finish_planned, state.project_finish_forecast), 3);
});

test('graph layout follows every directed demo dependency', () => {
  const ranks = graphRanks(demoState.tasks, demoState.dependencies);
  for (const edge of demoState.dependencies) {
    assert.ok(ranks.get(edge.predecessor_id) < ranks.get(edge.successor_id), edge.id);
  }
});

test('location focus groups use backend location IDs without inventing labels', () => {
  assert.equal(locationGroup('T5-BATH'), 'T5');
  assert.equal(locationGroup('T6'), 'T6');
  assert.equal(locationGroup('F2'), 'F2');
});

test('rejects dangling edges, invalid dates and invalid event responses', () => {
  assert.throws(() => readState({
    ...demoState,
    dependencies: [{ ...demoState.dependencies[0], successor_id: 'missing' }],
  }), /dependency/);
  assert.throws(() => readState({ ...demoState, today: '2026-02-30' }), /project state/);
  assert.throws(() => readEvents([{ kind: 'task_changed' }]), /audit events/);
  assert.deepEqual(readEvents([]), []);
});
