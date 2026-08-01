import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createRuntimeEventReducer,
  runtimeStateIsTerminal,
  runtimeStateToolStatus,
} from '../static/js/runtimeEvents.js';

function event(sequence, type, payload) {
  return {
    version: 2,
    event_id: `event-${sequence}`,
    run_id: 'run-1',
    sequence,
    timestamp: '2026-08-01T12:00:00Z',
    type,
    caused_by: payload.call_id || null,
    payload,
  };
}

test('runtime reducer preserves order and rejects duplicate or foreign events', () => {
  const reducer = createRuntimeEventReducer();
  assert.equal(reducer.consume(event(1, 'run_state', { state: 'preparing' })).runtime_state, 'preparing');
  assert.equal(reducer.consume(event(1, 'run_state', { state: 'running' })), null);
  assert.equal(reducer.consume({ ...event(2, 'run_state', { state: 'running' }), run_id: 'run-2' }), null);
  assert.equal(reducer.lastSequence, 1);
});

test('typed tool events project one canonical result shape', () => {
  const reducer = createRuntimeEventReducer();
  const started = reducer.consume(event(1, 'tool_started', {
    call_id: 'call-1', canonical_name: 'read_files', command: 'README.md', round: 1,
  }));
  assert.equal(started.type, 'tool_start');
  assert.equal(started.tool, 'read_files');

  const result = reducer.consume(event(2, 'tool_result', {
    command: 'README.md', round: 1,
    result: {
      call_id: 'call-1', canonical_name: 'read_files', status: 'success',
      data: { text: 'contents' }, error: null, backend: 'workspace',
    },
  }));
  assert.equal(result.type, 'tool_output');
  assert.equal(result.output, 'contents');
  assert.equal(result.exit_code, 0);
  assert.equal(result.tool_result.canonical_name, 'read_files');
});

test('provider completion is distinct from semantic terminal state', () => {
  assert.equal(runtimeStateIsTerminal('running'), false);
  assert.equal(runtimeStateIsTerminal('waiting_approval'), true);
  assert.equal(runtimeStateToolStatus('completed'), 'done');
  assert.equal(runtimeStateToolStatus('failed'), 'failed');
  assert.equal(runtimeStateToolStatus(null), 'interrupted');
});
