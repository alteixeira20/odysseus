// Pure request-field projection for the workspace + shell controls.
// Workspace identity and shell authorization are independent axes: selecting a
// workspace changes the starting directory, never the visible shell toggle.

export function agentToolRequestFields({ shellEnabled, workspace }) {
  const fields = {
    allow_bash: shellEnabled ? 'true' : 'false',
  };
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
