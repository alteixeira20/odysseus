// Keep Settings > Integrations creation actions in the panel header.
// Category renderers remain responsible for the actual forms and handlers;
// this module only exposes the active category's existing action beside the
// Integrations title so the content area stays focused on configured items.

const CATEGORY_LABELS = {
  agents: 'Add Custom Agent',
  mcp: 'Add MCP Server',
  email: 'Add Email Account',
  calendar: 'Add Calendar',
  contacts: 'Add Contacts',
  api: 'Add API Service',
};

function activeCategory() {
  return document.querySelector('#intg-category-tabs .intg-cat-btn.active')?.dataset.cat || 'overview';
}

function integrationTitleRow() {
  const panel = document.querySelector('[data-settings-panel="integrations"]');
  if (!panel) return null;
  const heading = Array.from(panel.querySelectorAll('h2')).find(el => (el.textContent || '').trim() === 'Integrations');
  return heading?.parentElement || null;
}

function ensureHeaderHost() {
  const row = integrationTitleRow();
  if (!row) return null;

  row.style.display = 'flex';
  row.style.alignItems = 'center';
  row.style.justifyContent = 'space-between';
  row.style.gap = '12px';

  let host = row.querySelector('#integration-header-action');
  if (!host) {
    host = document.createElement('div');
    host.id = 'integration-header-action';
    host.style.cssText = 'margin-left:auto;display:flex;align-items:center;justify-content:flex-end;flex:0 0 auto;';
    row.appendChild(host);
  }
  return host;
}

function sourceAction(category) {
  const list = document.getElementById('unified-integrations-list');
  if (!list) return null;

  if (category === 'agents') {
    const legacyWrap = list.querySelector('.custom-agent-top-action');
    if (legacyWrap) legacyWrap.style.display = 'none';
    return list.querySelector('.add-custom-agent-btn');
  }

  const action = list.querySelector('.intg-cat-add-btn');
  if (action) action.style.display = 'none';
  return action;
}

function renderHeaderAction() {
  const host = ensureHeaderHost();
  if (!host) return;

  host.replaceChildren();
  const category = activeCategory();
  const label = CATEGORY_LABELS[category];
  if (!label) return;

  const source = sourceAction(category);
  if (!source) return;

  const action = document.createElement('button');
  action.type = 'button';
  action.className = 'admin-btn-add integration-header-add-btn';
  action.textContent = label;
  action.style.cssText = 'display:inline-flex;align-items:center;justify-content:center;gap:5px;white-space:nowrap;';
  action.addEventListener('click', () => source.click());
  host.appendChild(action);
}

function init() {
  const list = document.getElementById('unified-integrations-list');
  const tabs = document.getElementById('intg-category-tabs');
  if (!list || !tabs) return;

  let pending = false;
  const schedule = () => {
    if (pending) return;
    pending = true;
    queueMicrotask(() => {
      pending = false;
      renderHeaderAction();
    });
  };

  new MutationObserver(schedule).observe(list, { childList: true, subtree: true });
  tabs.querySelectorAll('.intg-cat-btn').forEach(button => button.addEventListener('click', schedule));
  schedule();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init, { once: true });
} else {
  init();
}
