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
    conversation_id: 'conversation-1',
    turn_id: 'turn-1',
    candidate_id: null,
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

test('runtime reducer rejects missing or foreign ownership identity', () => {
  const reducer = createRuntimeEventReducer();
  const first = event(1, 'run_state', { state: 'preparing' });
  assert.equal(reducer.consume({ ...first, conversation_id: '' }), null);
  assert.equal(reducer.consume(first).runtime_state, 'preparing');
  assert.equal(reducer.consume({ ...event(2, 'run_state', { state: 'running' }), turn_id: 'other-turn' }), null);
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
  assert.equal(runtimeStateIsTerminal('waiting_approval'), false);
  assert.equal(runtimeStateToolStatus('completed'), 'done');
  assert.equal(runtimeStateToolStatus('failed'), 'failed');
  assert.equal(runtimeStateToolStatus(null), 'interrupted');
});

test('approval state retains exact effect details for the decision UI', () => {
  const reducer = createRuntimeEventReducer();
  const projected = reducer.consume(event(1, 'run_state', {
    state: 'waiting_approval',
    terminal: false,
    approval_id: 'approval-1',
    call_id: 'call-1',
    canonical_name: 'patch_workspace',
    effects: [{ kind: 'filesystem.delete', target: 'obsolete.txt' }],
  }));
  assert.equal(projected.approval_id, 'approval-1');
  assert.equal(projected.call_id, 'call-1');
  assert.equal(projected.effects[0].target, 'obsolete.txt');
  assert.equal(projected.terminal, false);
});

test('terminal failure cannot be overwritten by later running or completion events', () => {
  const reducer = createRuntimeEventReducer();
  assert.equal(reducer.consume(event(1, 'run_state', {
    state: 'failed', terminal: true, reason: 'provider_error',
  })).runtime_state, 'failed');
  assert.equal(reducer.consume(event(2, 'run_state', {
    state: 'running', terminal: false,
  })), null);
  assert.equal(reducer.consume(event(3, 'run_state', {
    state: 'completed', terminal: true,
  })), null);
  assert.equal(reducer.runState, 'failed');
});

test('a later failure may correct an earlier false completion', () => {
  const reducer = createRuntimeEventReducer();
  assert.equal(reducer.consume(event(1, 'run_state', {
    state: 'completed', terminal: true,
  })).runtime_state, 'completed');
  assert.equal(reducer.consume(event(2, 'run_state', {
    state: 'failed', terminal: true, reason: 'late_transport_error',
  })).runtime_state, 'failed');
  assert.equal(reducer.runState, 'failed');
});
