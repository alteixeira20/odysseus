// Runtime V2 events are authoritative. This reducer validates ordering and
// projects typed events into the existing rendering vocabulary while the UI
// components are migrated incrementally.

const TERMINAL_STATES = new Set([
  'waiting_user',
  'incomplete',
  'completed',
  'failed',
  'cancelled',
]);

function resultOutput(result) {
  const data = result?.data || {};
  const error = result?.error || null;
  for (const key of ['text', 'summary', 'stdout']) {
    if (data[key] != null && data[key] !== '') return String(data[key]);
  }
  return error?.message ? String(error.message) : '';
}

export function runtimeStateIsTerminal(state) {
  return TERMINAL_STATES.has(String(state || ''));
}

export function runtimeStateToolStatus(state) {
  if (state === 'completed') return 'done';
  if (state === 'cancelled') return 'cancelled';
  if (state === 'failed') return 'failed';
  return 'interrupted';
}

export function createRuntimeEventReducer() {
  let runId = null;
  let lastSequence = 0;
  let runState = null;
  let terminalState = null;

  return {
    get runId() { return runId; },
    get lastSequence() { return lastSequence; },
    get runState() { return runState; },

    consume(raw) {
      if (!raw || raw.version !== 2 || !raw.event_id || !raw.run_id || !raw.timestamp) return null;
      const sequence = Number(raw.sequence);
      if (!Number.isInteger(sequence) || sequence <= 0) return null;
      if (runId && raw.run_id !== runId) return null;
      if (sequence <= lastSequence) return null;
      if (!raw.payload || typeof raw.payload !== 'object' || Array.isArray(raw.payload)) return null;
      runId = raw.run_id;
      lastSequence = sequence;

      const payload = raw.payload;
      if (raw.type === 'run_state') {
        const candidateState = String(payload.state || 'incomplete');
        const candidateTerminal = payload.terminal === true || runtimeStateIsTerminal(candidateState);
        if (terminalState) {
          if (!candidateTerminal) return null;
          if (!(terminalState === 'completed' && candidateState !== 'completed')) return null;
        }
        runState = candidateState;
        if (candidateTerminal) terminalState = candidateState;
        return {
          type: 'run_state',
          state: String(payload.disposition || runState),
          runtime_state: runState,
          terminal: payload.terminal === true || runtimeStateIsTerminal(runState),
          reason: String(payload.reason || runState),
          resumable: !!payload.resumable,
          approval_id: payload.approval_id ? String(payload.approval_id) : null,
          call_id: payload.call_id ? String(payload.call_id) : null,
          canonical_name: payload.canonical_name ? String(payload.canonical_name) : null,
          effects: Array.isArray(payload.effects) ? payload.effects : [],
          runtime_event: raw,
        };
      }

      if (raw.type === 'tool_started') {
        return {
          type: 'tool_start',
          tool: String(payload.canonical_name || ''),
          command: String(payload.command || ''),
          round: payload.round,
          invocation_id: String(payload.call_id || raw.caused_by || ''),
          runtime_event: raw,
        };
      }

      if (raw.type === 'tool_result') {
        const result = payload.result || {};
        const data = result.data || {};
        const successful = result.status === 'success';
        return {
          type: 'tool_output',
          tool: String(result.canonical_name || ''),
          command: String(payload.command || ''),
          round: payload.round,
          invocation_id: String(result.call_id || raw.caused_by || ''),
          completion_state: String(result.status || 'error'),
          output: resultOutput(result),
          exit_code: data.exit_code != null ? data.exit_code : (successful ? 0 : 1),
          timed_out: result.status === 'timed_out',
          cancelled: result.status === 'cancelled',
          error_type: result.error?.code || null,
          backend: result.backend || 'runtime_v2',
          tool_result: result,
          ...data,
          runtime_event: raw,
        };
      }

      return { type: raw.type, ...payload, runtime_event: raw };
    },
  };
}
