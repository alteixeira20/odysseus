import assert from 'node:assert/strict';
import test from 'node:test';

import {
  settleRunningToolNodes,
  settleToolNode,
  toolTerminalState,
} from '../static/js/agentToolLifecycle.js';


class FakeClassList {
  constructor(...names) {
    this.names = new Set(names);
  }
  contains(name) {
    return this.names.has(name);
  }
  add(name) {
    this.names.add(name);
  }
  remove(name) {
    this.names.delete(name);
  }
}


function fakeNode() {
  const status = { textContent: '' };
  const icon = { textContent: '' };
  const wave = { removed: false, remove() { this.removed = true; } };
  return {
    classList: new FakeClassList('agent-thread-node', 'running'),
    _waveInterval: setInterval(() => {}, 10_000),
    _elapsedTicker: setInterval(() => {}, 10_000),
    querySelector(selector) {
      return {
        '.agent-thread-status': status,
        '.agent-thread-icon': icon,
        '.agent-thread-wave': wave,
      }[selector] || null;
    },
    status,
    icon,
    wave,
  };
}


test('terminal state distinguishes timeout, cancellation, failure and success', () => {
  assert.equal(toolTerminalState({ timed_out: true, exit_code: 124 }), 'timed out');
  assert.equal(toolTerminalState({ cancelled: true }), 'cancelled');
  assert.equal(toolTerminalState({ exit_code: 7 }), 'failed');
  assert.equal(toolTerminalState({ exit_code: 0 }), 'done');
});


for (const state of ['timed out', 'cancelled', 'failed', 'done']) {
  test(`${state} settles RUNNING exactly once`, () => {
    const node = fakeNode();
    const root = {
      querySelectorAll(selector) {
        assert.equal(selector, '.agent-thread-node.running');
        return node.classList.contains('running') ? [node] : [];
      },
    };

    assert.equal(settleRunningToolNodes(root, state), 1);
    assert.equal(settleRunningToolNodes(root, state), 0);
    assert.equal(node.classList.contains('running'), false);
    assert.equal(node._waveInterval, null);
    assert.equal(node._elapsedTicker, null);
    assert.equal(node.status.textContent, state);
    assert.equal(node.wave.removed, true);
    assert.equal(
      node.classList.contains('error'),
      state !== 'done',
    );
  });
}


test('settling a non-running node is an idempotent no-op', () => {
  const node = fakeNode();
  assert.equal(settleToolNode(node, 'cancelled'), true);
  assert.equal(settleToolNode(node, 'cancelled'), false);
});
