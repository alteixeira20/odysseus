const state = {
  runs: [],
  selectedRunId: null,
  selected: null,
  events: [],
  source: null,
  reconcileEffect: null,
};

const elements = {
  runList: document.querySelector('#runList'),
  statusFilter: document.querySelector('#statusFilter'),
  refreshRuns: document.querySelector('#refreshRuns'),
  emptyState: document.querySelector('#emptyState'),
  runDetail: document.querySelector('#runDetail'),
  runWorkload: document.querySelector('#runWorkload'),
  runTitle: document.querySelector('#runTitle'),
  runReason: document.querySelector('#runReason'),
  runMetrics: document.querySelector('#runMetrics'),
  effectList: document.querySelector('#effectList'),
  effectSummary: document.querySelector('#effectSummary'),
  eventTimeline: document.querySelector('#eventTimeline'),
  reloadEvents: document.querySelector('#reloadEvents'),
  cancelRun: document.querySelector('#cancelRun'),
  liveState: document.querySelector('#liveState'),
  reconcileDialog: document.querySelector('#reconcileDialog'),
  reconcileForm: document.querySelector('#reconcileForm'),
  reconcileTarget: document.querySelector('#reconcileTarget'),
  reconcileOutcome: document.querySelector('#reconcileOutcome'),
  reconcileNote: document.querySelector('#reconcileNote'),
  toast: document.querySelector('#toast'),
};

function node(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = String(options.text);
  if (options.title) element.title = options.title;
  for (const [name, value] of Object.entries(options.attributes || {})) {
    element.setAttribute(name, String(value));
  }
  for (const child of children) element.append(child);
  return element;
}

function showToast(message, error = false) {
  elements.toast.textContent = message;
  elements.toast.classList.toggle('error', error);
  elements.toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    elements.toast.hidden = true;
  }, 4500);
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || `Request failed (${response.status})`);
  }
  return payload;
}

function formatTime(value) {
  if (!value) return '—';
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'short',
    timeStyle: 'medium',
  }).format(new Date(Number(value) * 1000));
}

function shortId(value) {
  const text = String(value || '');
  return text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text;
}

function statusClass(status) {
  return `status status-${String(status || 'unknown').replace(/[^a-z0-9_-]/gi, '-')}`;
}

function isTerminal(status) {
  return ['completed', 'incomplete', 'cancelled', 'failed', 'interrupted'].includes(status);
}

async function loadRuns({ preserveSelection = true } = {}) {
  const params = new URLSearchParams({ limit: '100' });
  if (elements.statusFilter.value) params.set('status', elements.statusFilter.value);
  const payload = await requestJson(`/api/agent-runs?${params}`);
  state.runs = payload.runs || [];
  renderRuns();
  if (preserveSelection && state.selectedRunId) {
    const exists = state.runs.some((run) => run.run_id === state.selectedRunId);
    if (exists) await selectRun(state.selectedRunId, { reconnect: false });
  }
}

function renderRuns() {
  elements.runList.replaceChildren();
  if (!state.runs.length) {
    elements.runList.append(node('p', { className: 'empty-copy', text: 'No matching durable runs.' }));
    return;
  }
  for (const run of state.runs) {
    const button = node('button', {
      className: `run-card${run.run_id === state.selectedRunId ? ' selected' : ''}`,
      attributes: { type: 'button', 'data-run-id': run.run_id },
    });
    button.append(
      node('span', { className: statusClass(run.status), text: run.status }),
      node('strong', { text: run.model || run.workload || 'Agent run' }),
      node('span', { className: 'run-id', text: shortId(run.run_id), title: run.run_id }),
      node('span', { className: 'run-time', text: formatTime(run.created_at) }),
    );
    button.addEventListener('click', () => selectRun(run.run_id));
    elements.runList.append(button);
  }
}

async function selectRun(runId, { reconnect = true } = {}) {
  state.selectedRunId = runId;
  renderRuns();
  state.selected = await requestJson(`/api/agent-runs/${encodeURIComponent(runId)}`);
  elements.emptyState.hidden = true;
  elements.runDetail.hidden = false;
  renderRun();
  await loadEvents();
  if (reconnect) connectEventStream();
}

function appendMetric(term, value) {
  elements.runMetrics.append(
    node('div', { className: 'metric' }, [
      node('dt', { text: term }),
      node('dd', { text: value ?? '—' }),
    ]),
  );
}

function renderRun() {
  const run = state.selected;
  if (!run) return;
  elements.runWorkload.textContent = run.workload || 'agent';
  elements.runTitle.textContent = `${run.model || 'Agent'} · ${shortId(run.run_id)}`;
  elements.runTitle.title = run.run_id;
  elements.runReason.textContent = run.terminal_reason || 'Execution is active or awaiting a terminal reason.';
  elements.cancelRun.disabled = isTerminal(run.status);
  elements.runMetrics.replaceChildren();
  appendMetric('Status', run.status);
  appendMetric('Created', formatTime(run.created_at));
  appendMetric('Updated', formatTime(run.updated_at));
  appendMetric('Events', run.last_event_seq);
  appendMetric('Revision', run.revision);
  appendMetric('Resumable', run.resumable ? 'Yes' : 'No');
  appendMetric('Session', shortId(run.session_id));
  appendMetric('Endpoint', run.endpoint ? `${run.endpoint.provider_family} · ${run.endpoint.locality}` : '—');
  renderEffects();
}

function renderEffects() {
  const effects = state.selected?.effects || [];
  elements.effectList.replaceChildren();
  const unknown = effects.filter((effect) => effect.status === 'unknown').length;
  elements.effectSummary.textContent = `${effects.length} total · ${unknown} unknown`;
  if (!effects.length) {
    elements.effectList.append(node('p', { className: 'empty-copy', text: 'No durable effects recorded.' }));
    return;
  }
  for (const effect of effects) {
    const card = node('article', { className: 'effect-card' });
    const heading = node('div', { className: 'effect-heading' }, [
      node('div', {}, [
        node('strong', { text: effect.tool_name }),
        node('p', { className: 'muted', text: `${effect.effect_class} · attempt ${effect.attempt}` }),
      ]),
      node('span', { className: statusClass(effect.status), text: effect.status }),
    ]);
    card.append(heading);
    const details = node('details');
    details.append(
      node('summary', { text: 'Recorded contract' }),
      node('pre', { text: JSON.stringify({ request: effect.request, error: effect.error }, null, 2) }),
    );
    card.append(details);
    if (effect.status === 'unknown') {
      const reconcile = node('button', {
        className: 'button warning compact',
        text: 'Reconcile',
        attributes: { type: 'button' },
      });
      reconcile.addEventListener('click', () => openReconcile(effect));
      card.append(reconcile);
    }
    elements.effectList.append(card);
  }
}

async function loadEvents() {
  if (!state.selectedRunId) return;
  const payload = await requestJson(
    `/api/agent-runs/${encodeURIComponent(state.selectedRunId)}/events?after_seq=0&limit=100000`,
  );
  state.events = payload.events || [];
  renderEvents();
}

function renderEvents() {
  elements.eventTimeline.replaceChildren();
  if (!state.events.length) {
    elements.eventTimeline.append(node('li', { className: 'empty-copy', text: 'No events recorded.' }));
    return;
  }
  for (const event of state.events) {
    const item = node('li', { className: 'event-item' });
    item.append(
      node('span', { className: 'event-seq', text: `#${event.seq}` }),
      node('div', { className: 'event-body' }, [
        node('strong', { text: event.type }),
        node('time', { text: formatTime(event.created_at) }),
        node('pre', { text: JSON.stringify(event.payload, null, 2) }),
      ]),
    );
    elements.eventTimeline.append(item);
  }
}

function connectEventStream() {
  if (state.source) state.source.close();
  if (!state.selectedRunId || isTerminal(state.selected?.status)) {
    elements.liveState.textContent = 'Durable replay';
    elements.liveState.className = 'live-state';
    return;
  }
  const after = state.events.at(-1)?.seq || 0;
  const source = new EventSource(
    `/api/agent-runs/${encodeURIComponent(state.selectedRunId)}/stream?after_seq=${after}`,
    { withCredentials: true },
  );
  state.source = source;
  elements.liveState.textContent = 'Connecting';
  elements.liveState.className = 'live-state connecting';
  source.onopen = () => {
    elements.liveState.textContent = 'Live';
    elements.liveState.className = 'live-state live';
  };
  source.addEventListener('runtime_event', (message) => {
    const event = JSON.parse(message.data);
    if (!state.events.some((existing) => existing.seq === event.seq)) {
      state.events.push(event);
      renderEvents();
    }
  });
  source.addEventListener('terminal', async (message) => {
    source.close();
    elements.liveState.textContent = 'Terminal';
    elements.liveState.className = 'live-state';
    const terminal = JSON.parse(message.data);
    showToast(`Run ${terminal.status}: ${terminal.reason || 'terminal'}`);
    await selectRun(state.selectedRunId, { reconnect: false });
    await loadRuns();
  });
  source.onerror = () => {
    elements.liveState.textContent = 'Reconnecting';
    elements.liveState.className = 'live-state connecting';
  };
}

async function cancelSelectedRun() {
  if (!state.selectedRunId) return;
  if (!window.confirm('Cancel this run? Started external effects may remain unknown until reconciled.')) return;
  try {
    const result = await requestJson(
      `/api/agent-runs/${encodeURIComponent(state.selectedRunId)}/cancel`,
      { method: 'POST', body: JSON.stringify({ reason: 'cancelled_from_runtime_inspector' }) },
    );
    showToast(result.active_task_cancelled ? 'Cancellation dispatched.' : 'Run marked cancelled; no active worker remained.');
    await selectRun(state.selectedRunId);
    await loadRuns();
  } catch (error) {
    showToast(error.message, true);
  }
}

function openReconcile(effect) {
  state.reconcileEffect = effect;
  elements.reconcileTarget.textContent = `${effect.tool_name} · ${shortId(effect.effect_id)} · revision ${effect.revision}`;
  elements.reconcileOutcome.value = 'committed';
  elements.reconcileNote.value = '';
  elements.reconcileDialog.showModal();
}

async function submitReconciliation(event) {
  event.preventDefault();
  if (!state.reconcileEffect || !state.selectedRunId) return;
  const submitter = event.submitter?.value;
  if (submitter === 'cancel') {
    elements.reconcileDialog.close();
    return;
  }
  const note = elements.reconcileNote.value.trim();
  if (note.length < 8) {
    showToast('Provide at least eight characters of verification evidence.', true);
    return;
  }
  try {
    await requestJson(
      `/api/agent-runs/${encodeURIComponent(state.selectedRunId)}/effects/${encodeURIComponent(state.reconcileEffect.effect_id)}/reconcile`,
      {
        method: 'POST',
        body: JSON.stringify({
          outcome: elements.reconcileOutcome.value,
          note,
          expected_revision: state.reconcileEffect.revision,
          evidence: { source: 'runtime_inspector' },
        }),
      },
    );
    elements.reconcileDialog.close();
    showToast('Effect reconciliation recorded.');
    await selectRun(state.selectedRunId);
  } catch (error) {
    showToast(error.message, true);
  }
}

elements.refreshRuns.addEventListener('click', () => loadRuns().catch((error) => showToast(error.message, true)));
elements.statusFilter.addEventListener('change', () => loadRuns({ preserveSelection: false }).catch((error) => showToast(error.message, true)));
elements.reloadEvents.addEventListener('click', () => loadEvents().catch((error) => showToast(error.message, true)));
elements.cancelRun.addEventListener('click', cancelSelectedRun);
elements.reconcileForm.addEventListener('submit', submitReconciliation);
window.addEventListener('beforeunload', () => state.source?.close());

loadRuns({ preserveSelection: false }).catch((error) => showToast(error.message, true));
