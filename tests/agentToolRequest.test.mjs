import assert from 'node:assert/strict';
import test from 'node:test';

import {
  agentToolRequestFields,
  appendAgentToolRequestFields,
} from '../static/js/agentToolRequest.js';


for (const [workspace, shellEnabled, expected] of [
  ['', false, { allow_bash: 'false', shell_mode: 'disabled' }],
  ['', true, { allow_bash: 'true', shell_mode: 'sandboxed' }],
  ['/work/repo', false, { allow_bash: 'false', shell_mode: 'disabled', workspace: '/work/repo' }],
  ['/work/repo', true, { allow_bash: 'true', shell_mode: 'sandboxed', workspace: '/work/repo' }],
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
    shell_mode: 'sandboxed',
    workspace: '/selected/repository',
  });
  assert.deepEqual(appended, [
    ['allow_bash', 'true'],
    ['shell_mode', 'sandboxed'],
    ['workspace', '/selected/repository'],
  ]);
});


test('full host shell is a distinct explicit request mode', () => {
  assert.deepEqual(agentToolRequestFields({
    shellEnabled: false,
    hostShellEnabled: true,
    workspace: '/selected/repository',
  }), {
    allow_bash: 'true',
    shell_mode: 'host',
    workspace: '/selected/repository',
  });
});
