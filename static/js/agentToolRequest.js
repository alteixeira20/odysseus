// Pure request-field projection for the workspace + shell controls.
// Workspace identity and shell authorization are independent axes: selecting a
// workspace changes the starting directory, never the visible shell toggle.

export function agentToolRequestFields({ shellEnabled, hostShellEnabled, hostAuthorization, workspaceWriteEnabled, workspace }) {
  const authorizedHost = !!hostShellEnabled && !!String(hostAuthorization || '').trim();
  const shellMode = authorizedHost
    ? 'host'
    : (shellEnabled ? 'sandboxed' : 'disabled');
  const fields = {
    // Keep the legacy boolean for old servers/clients. It can enable only the
    // sandboxed mode server-side; host authority always needs shell_mode=host.
    allow_bash: shellMode === 'disabled' ? 'false' : 'true',
    shell_mode: shellMode,
    // Workspace selection is inspection-only. Mutation is a separate,
    // explicit authenticated grant and never follows from host writability.
    allow_workspace_write: workspaceWriteEnabled ? 'true' : 'false',
  };
  if (authorizedHost) fields.host_authorization = String(hostAuthorization).trim();
  const selectedWorkspace = String(workspace || '').trim();
  if (selectedWorkspace) fields.workspace = selectedWorkspace;
  return fields;
}


export function appendAgentToolRequestFields(formData, state) {
  const fields = agentToolRequestFields(state);
  for (const [name, value] of Object.entries(fields)) {
    formData.append(name, value);
  }
  return fields;
}
