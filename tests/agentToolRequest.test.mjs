import assert from 'node:assert/strict';
import test from 'node:test';

import {
  agentToolRequestFields,
  appendAgentToolRequestFields,
} from '../static/js/agentToolRequest.js';


for (const [workspace, shellEnabled, expected] of [
  ['', false, { allow_bash: 'false' }],
  ['', true, { allow_bash: 'true' }],
  ['/work/repo', false, { allow_bash: 'false', workspace: '/work/repo' }],
  ['/work/repo', true, { allow_bash: 'true', workspace: '/work/repo' }],
]) {
  test(`request fields keep workspace=${workspace || 'none'} and shell=${shellEnabled} independent`, () => {
    assert.deepEqual(
      agentToolRequestFields({ workspace, shellEnabled }),
      expected,
    );
  });
}


test('visible shell state is the exact allow_bash value appended to the request', () => {
  const appended = [];
  const formData = {
    append(name, value) { appended.push([name, value]); },
  };
  const fields = appendAgentToolRequestFields(formData, {
    shellEnabled: true,
    workspace: '/selected/repository',
  });

  assert.deepEqual(fields, {
    allow_bash: 'true',
    workspace: '/selected/repository',
  });
  assert.deepEqual(appended, [
    ['allow_bash', 'true'],
    ['workspace', '/selected/repository'],
  ]);
});
