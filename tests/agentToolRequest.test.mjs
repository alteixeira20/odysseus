import assert from 'node:assert/strict';
import test from 'node:test';

import {
  agentToolRequestFields,
  appendAgentToolRequestFields,
} from '../static/js/agentToolRequest.js';


for (const [workspace, shellEnabled, expected] of [
  ['', false, { allow_bash: 'false', shell_mode: 'disabled', allow_workspace_write: 'false' }],
  ['', true, { allow_bash: 'true', shell_mode: 'sandboxed', allow_workspace_write: 'false' }],
  ['/work/repo', false, { allow_bash: 'false', shell_mode: 'disabled', allow_workspace_write: 'false', workspace: '/work/repo' }],
  ['/work/repo', true, { allow_bash: 'true', shell_mode: 'sandboxed', allow_workspace_write: 'false', workspace: '/work/repo' }],
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
    allow_workspace_write: 'false',
    workspace: '/selected/repository',
  });
  assert.deepEqual(appended, [
    ['allow_bash', 'true'],
    ['shell_mode', 'sandboxed'],
    ['allow_workspace_write', 'false'],
    ['workspace', '/selected/repository'],
  ]);
});


test('full host shell is a distinct explicit request mode', () => {
  assert.deepEqual(agentToolRequestFields({
    shellEnabled: false,
    hostShellEnabled: true,
    hostAuthorization: 'one-run-token',
    workspace: '/selected/repository',
  }), {
    allow_bash: 'true',
    shell_mode: 'host',
    allow_workspace_write: 'false',
    host_authorization: 'one-run-token',
    workspace: '/selected/repository',
  });
});

test('workspace mutation is an independent explicit request grant', () => {
  assert.deepEqual(agentToolRequestFields({
    shellEnabled: false,
    hostShellEnabled: false,
    hostAuthorization: '',
    workspaceWriteEnabled: true,
    workspace: '/selected/repository',
  }), {
    allow_bash: 'false',
    shell_mode: 'disabled',
    allow_workspace_write: 'true',
    workspace: '/selected/repository',
  });
});


test('host UI state without a server authorization cannot request host mode', () => {
  assert.deepEqual(agentToolRequestFields({
    shellEnabled: false,
    hostShellEnabled: true,
    workspace: '/selected/repository',
  }), {
    allow_bash: 'false',
    shell_mode: 'disabled',
    allow_workspace_write: 'false',
    workspace: '/selected/repository',
  });
});
