// Category-scoped actions for Settings > Integrations.
// Keeps creation relevant to the active category and provides a generic
// external-CLI connection path alongside the first-party agent clients.

const CATEGORY_ACTIONS = {
  mcp: 'Add MCP Server',
  email: 'Add Email Account',
  calendar: 'Add Calendar',
  contacts: 'Add Contacts',
  api: 'Add API Service',
};

const CUSTOM_SCOPE_GROUPS = [
  {
    title: 'Work',
    items: [
      ['todos:read', 'Read todos'],
      ['todos:write', 'Manage todos'],
      ['documents:read', 'Read documents'],
      ['documents:write', 'Manage documents'],
    ],
  },
  {
    title: 'Communication',
    items: [
      ['email:read', 'Read email'],
      ['email:draft', 'Draft email'],
      ['email:send', 'Send email'],
      ['calendar:read', 'Read calendar'],
      ['calendar:write', 'Manage calendar'],
    ],
  },
  {
    title: 'Knowledge & automation',
    items: [
      ['memory:read', 'Read memory'],
      ['memory:write', 'Manage memory'],
      ['cookbook:read', 'Read cookbook'],
      ['cookbook:launch', 'Launch cookbook jobs'],
    ],
  },
];

function activeCategory() {
  return document.querySelector('#intg-category-tabs .intg-cat-btn.active')?.dataset.cat || 'overview';
}

function button(label, className = 'admin-btn-sm') {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = className;
  el.textContent = label;
  el.style.cssText = [
    'display:inline-flex',
    'align-items:center',
    'justify-content:center',
    'gap:5px',
    'font-size:11px',
    'font-weight:600',
    'color:var(--accent,var(--red))',
    'border:1px solid color-mix(in srgb,var(--accent,var(--red)) 40%,transparent)',
    'background:color-mix(in srgb,var(--accent,var(--red)) 8%,transparent)',
    'border-radius:6px',
    'padding:6px 10px',
    'cursor:pointer',
  ].join(';');
  return el;
}

function ensureStyles() {
  if (document.getElementById('integration-category-actions-style')) return;
  const style = document.createElement('style');
  style.id = 'integration-category-actions-style';
  style.textContent = `
    #settings-modal .modal-content.settings-integrations-expanded {
      width: min(1040px, calc(100vw - 48px)) !important;
      max-width: 1040px !important;
    }
    #settings-modal .settings-integrations-expanded #intg-category-tabs,
    #settings-modal .settings-integrations-expanded #unified-integrations-list,
    #settings-modal .settings-integrations-expanded #unified-intg-form {
      width: 100%;
      max-width: none;
      box-sizing: border-box;
    }
    .custom-agent-top-action {
      display: flex;
      justify-content: flex-end;
      align-items: center;
      margin: 0 0 10px;
    }
    .custom-agent-modal-layer {
      position: absolute;
      inset: 0;
      z-index: 80;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 28px;
      background: color-mix(in srgb, var(--bg) 68%, transparent);
      backdrop-filter: blur(3px);
      -webkit-backdrop-filter: blur(3px);
      border-radius: inherit;
      box-sizing: border-box;
    }
    .custom-agent-modal {
      width: min(720px, 100%);
      max-height: min(720px, calc(100vh - 110px));
      overflow: auto;
      border: 1px solid color-mix(in srgb, var(--border) 82%, var(--fg) 8%);
      border-radius: 12px;
      background: var(--panel, var(--bg));
      box-shadow: 0 24px 70px rgba(0,0,0,.34);
      color: var(--fg);
    }
    .custom-agent-modal-header {
      display: flex;
      align-items: flex-start;
      gap: 14px;
      padding: 18px 20px 16px;
      border-bottom: 1px solid var(--border);
    }
    .custom-agent-modal-icon {
      width: 34px;
      height: 34px;
      border-radius: 9px;
      display: grid;
      place-items: center;
      flex: 0 0 auto;
      color: var(--accent, var(--red));
      background: color-mix(in srgb, var(--accent, var(--red)) 12%, transparent);
      border: 1px solid color-mix(in srgb, var(--accent, var(--red)) 28%, transparent);
      font-size: 16px;
      font-weight: 700;
    }
    .custom-agent-modal-title {
      margin: 0;
      font-size: 15px;
      line-height: 1.25;
    }
    .custom-agent-modal-subtitle {
      margin-top: 4px;
      font-size: 11px;
      line-height: 1.45;
      opacity: .62;
      max-width: 560px;
    }
    .custom-agent-modal-close {
      margin-left: auto;
      border: 0;
      background: transparent;
      color: inherit;
      opacity: .55;
      cursor: pointer;
      width: 28px;
      height: 28px;
      border-radius: 6px;
      font-size: 18px;
      line-height: 1;
    }
    .custom-agent-modal-close:hover { background: color-mix(in srgb, var(--fg) 7%, transparent); opacity: .9; }
    .custom-agent-modal-body { padding: 18px 20px 6px; }
    .custom-agent-field-label {
      display: block;
      margin: 0 0 6px;
      font-size: 11px;
      font-weight: 650;
      opacity: .78;
    }
    .custom-agent-name {
      width: 100%;
      box-sizing: border-box;
      margin-bottom: 18px;
    }
    .custom-agent-section-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 9px;
    }
    .custom-agent-section-title { font-size: 11px; font-weight: 650; opacity: .8; }
    .custom-agent-section-note { font-size: 10px; opacity: .48; }
    .custom-agent-permission-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 9px;
    }
    .custom-agent-permission-group {
      border: 1px solid var(--border);
      border-radius: 9px;
      padding: 10px 11px;
      background: color-mix(in srgb, var(--fg) 2.5%, transparent);
    }
    .custom-agent-permission-group:last-child:nth-child(odd) { grid-column: 1 / -1; }
    .custom-agent-permission-group-title {
      font-size: 10px;
      font-weight: 700;
      letter-spacing: .02em;
      text-transform: uppercase;
      opacity: .5;
      margin-bottom: 4px;
    }
    .custom-agent-scope-row {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 28px;
      font-size: 11px;
      cursor: pointer;
    }
    .custom-agent-scope-row span:first-of-type { flex: 1; min-width: 0; }
    .custom-agent-scope-code { font-size: 9px; opacity: .36; margin-left: 5px; }
    .custom-agent-status { min-height: 18px; margin-top: 10px; font-size: 11px; opacity: .8; }
    .custom-agent-modal-footer {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 7px;
      padding: 14px 20px 18px;
    }
    .custom-agent-result-block {
      border: 1px solid var(--border);
      border-radius: 9px;
      padding: 11px;
      margin-top: 10px;
      background: color-mix(in srgb, var(--fg) 2.5%, transparent);
    }
    .custom-agent-result-label { font-size: 10px; font-weight: 700; opacity: .55; margin-bottom: 6px; text-transform: uppercase; }
    .custom-agent-result-code {
      display: block;
      white-space: pre;
      overflow: auto;
      word-break: normal;
      padding: 9px 10px;
      border-radius: 7px;
      background: color-mix(in srgb, var(--bg) 74%, #000 26%);
      font-size: 10px;
      line-height: 1.5;
    }
    @media (max-width: 760px) {
      #settings-modal .modal-content.settings-integrations-expanded { width: calc(100vw - 16px) !important; }
      .custom-agent-modal-layer { padding: 12px; }
      .custom-agent-permission-grid { grid-template-columns: 1fr; }
      .custom-agent-permission-group:last-child:nth-child(odd) { grid-column: auto; }
    }
  `;
  document.head.appendChild(style);
}

function syncIntegrationsLayout() {
  ensureStyles();
  const tabs = document.getElementById('intg-category-tabs');
  const panel = tabs?.closest('[data-settings-panel="integrations"]');
  const modal = tabs?.closest('.modal-content') || document.querySelector('#settings-modal .modal-content');
  if (!modal) return false;

  const active = !!panel && !panel.classList.contains('hidden');
  modal.classList.toggle('settings-integrations-expanded', active);
  if (!active) return false;

  if (getComputedStyle(modal).position === 'static') modal.style.position = 'relative';

  const host = tabs?.parentElement;
  if (host) {
    host.style.width = '100%';
    host.style.maxWidth = 'none';
    host.style.boxSizing = 'border-box';
  }
  return true;
}

function expandIntegrationsLayout() {
  syncIntegrationsLayout();
}

function stripOverviewAggregate(list) {
  const grid = list.querySelector('.intg-cat-card')?.parentElement;
  if (!grid) return;
  let node = grid.nextElementSibling;
  while (node) {
    const next = node.nextElementSibling;
    node.remove();
    node = next;
  }
}

function normalizeCategoryAction(list, category) {
  const existing = list.querySelector('.intg-cat-add-btn');
  if (!existing) return;
  existing.textContent = CATEGORY_ACTIONS[category] || existing.textContent;
  const parent = existing.parentElement;
  if (parent) {
    parent.style.display = 'flex';
    parent.style.alignItems = 'center';
    parent.style.justifyContent = 'space-between';
  }
}

function scopeControl(scope, label, checked) {
  const row = document.createElement('label');
  row.className = 'custom-agent-scope-row';

  const text = document.createElement('span');
  text.textContent = label;

  const code = document.createElement('span');
  code.className = 'custom-agent-scope-code';
  code.textContent = scope;

  const input = document.createElement('input');
  input.type = 'checkbox';
  input.className = 'custom-agent-scope';
  input.dataset.scope = scope;
  input.checked = checked;

  row.append(text, code, input);
  return row;
}

function selectedScopes(root) {
  return ['chat'].concat(
    Array.from(root.querySelectorAll('.custom-agent-scope:checked')).map(el => el.dataset.scope)
  );
}

function genericSetup(token) {
  const origin = window.location.origin;
  return `export ODYSSEUS_URL=${origin}\nexport ODYSSEUS_API_TOKEN='${token}'\ncurl -fsSL -H "Authorization: Bearer $ODYSSEUS_API_TOKEN" "$ODYSSEUS_URL/api/codex/capabilities"`;
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {}
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.style.cssText = 'position:fixed;left:-9999px;top:0;';
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (_) {}
  area.remove();
  return ok;
}

function refreshAgents() {
  document.querySelector('#intg-category-tabs [data-cat="agents"]')?.click();
}

let _customAgentModalCleanup = null;

function closeCustomAgentModal() {
  const layer = document.querySelector('.custom-agent-modal-layer');
  if (layer) layer.remove();

  const cleanup = _customAgentModalCleanup;
  _customAgentModalCleanup = null;
  if (cleanup) cleanup();
}

function modalShell(titleText, subtitleText) {
  closeCustomAgentModal();
  expandIntegrationsLayout();

  const settingsModal = document.querySelector('#settings-modal .modal-content') || document.getElementById('settings-modal');
  if (!settingsModal) return null;

  const layer = document.createElement('div');
  layer.className = 'custom-agent-modal-layer';
  layer.addEventListener('mousedown', event => {
    if (event.target === layer) closeCustomAgentModal();
  });

  const modal = document.createElement('div');
  modal.className = 'custom-agent-modal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');

  const header = document.createElement('div');
  header.className = 'custom-agent-modal-header';

  const icon = document.createElement('div');
  icon.className = 'custom-agent-modal-icon';
  icon.textContent = '⌁';

  const headingWrap = document.createElement('div');
  const title = document.createElement('h2');
  title.className = 'custom-agent-modal-title';
  title.textContent = titleText;
  const subtitle = document.createElement('div');
  subtitle.className = 'custom-agent-modal-subtitle';
  subtitle.textContent = subtitleText;
  headingWrap.append(title, subtitle);

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'custom-agent-modal-close';
  close.setAttribute('aria-label', 'Close');
  close.textContent = '×';
  close.addEventListener('click', closeCustomAgentModal);

  header.append(icon, headingWrap, close);
  modal.appendChild(header);
  layer.appendChild(modal);
  settingsModal.appendChild(layer);

  const opener = document.activeElement;
  const esc = event => {
    if (event.key !== 'Escape' || !layer.isConnected) return;
    event.preventDefault();
    event.stopPropagation();
    closeCustomAgentModal();
  };

  document.addEventListener('keydown', esc, true);
  _customAgentModalCleanup = () => {
    document.removeEventListener('keydown', esc, true);
    if (opener && opener.isConnected && typeof opener.focus === 'function') {
      try { opener.focus(); } catch (_) {}
    }
  };

  return modal;
}

async function showCustomAgentForm(tokenId = null) {
  let current = null;
  if (tokenId) {
    try {
      const response = await fetch('/api/tokens', { credentials: 'same-origin' });
      if (response.ok) current = (await response.json()).find(token => String(token.id) === String(tokenId)) || null;
    } catch (_) {}
  }

  const modal = modalShell(
    current ? 'Edit custom agent' : 'Add custom agent',
    'Create an Odysseus connection for an external CLI. The CLI keeps its own model-provider authentication; this credential only controls what it can access in Odysseus.'
  );
  if (!modal) return;

  const body = document.createElement('div');
  body.className = 'custom-agent-modal-body';

  const nameLabel = document.createElement('label');
  nameLabel.className = 'custom-agent-field-label';
  nameLabel.textContent = 'Connection name';
  const name = document.createElement('input');
  name.className = 'settings-input custom-agent-name';
  name.placeholder = 'e.g. Main workstation';
  name.value = current?.name || '';
  body.append(nameLabel, name);

  const sectionHead = document.createElement('div');
  sectionHead.className = 'custom-agent-section-head';
  const sectionTitle = document.createElement('div');
  sectionTitle.className = 'custom-agent-section-title';
  sectionTitle.textContent = 'Permissions';
  const sectionNote = document.createElement('div');
  sectionNote.className = 'custom-agent-section-note';
  sectionNote.textContent = 'Chat access is always included';
  sectionHead.append(sectionTitle, sectionNote);
  body.appendChild(sectionHead);

  const scopeSet = new Set(current?.scopes || []);
  const permissions = document.createElement('div');
  permissions.className = 'custom-agent-permission-grid';
  CUSTOM_SCOPE_GROUPS.forEach(group => {
    const card = document.createElement('div');
    card.className = 'custom-agent-permission-group';
    const heading = document.createElement('div');
    heading.className = 'custom-agent-permission-group-title';
    heading.textContent = group.title;
    card.appendChild(heading);
    group.items.forEach(([scope, label]) => card.appendChild(scopeControl(scope, label, scopeSet.has(scope))));
    permissions.appendChild(card);
  });
  body.appendChild(permissions);

  const status = document.createElement('div');
  status.className = 'custom-agent-status';
  body.appendChild(status);
  modal.appendChild(body);

  const footer = document.createElement('div');
  footer.className = 'custom-agent-modal-footer';
  const cancel = button('Cancel', 'admin-btn-add');
  cancel.addEventListener('click', closeCustomAgentModal);
  footer.appendChild(cancel);

  if (current) {
    const revoke = button('Revoke', 'admin-btn-add');
    revoke.style.color = 'var(--color-error,var(--red))';
    revoke.addEventListener('click', async () => {
      const confirmFn = window.styledConfirm || (async message => window.confirm(message));
      if (!await confirmFn(`Revoke "${current.name}"?`, { confirmText: 'Revoke', danger: true })) return;
      const response = await fetch(`/api/tokens/${current.id}`, { method: 'DELETE', credentials: 'same-origin' });
      if (!response.ok) { status.textContent = 'Revoke failed'; return; }
      closeCustomAgentModal();
      refreshAgents();
    });
    footer.appendChild(revoke);

    const save = button('Save changes', 'admin-btn-add');
    save.addEventListener('click', async () => {
      const label = name.value.trim();
      if (!label) { status.textContent = 'Enter a connection name.'; name.focus(); return; }
      save.disabled = true;
      status.textContent = 'Saving…';
      const response = await fetch(`/api/tokens/${current.id}`, {
        method: 'PATCH', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: label, scopes: selectedScopes(permissions), agent_provider: 'custom' }),
      });
      save.disabled = false;
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        status.textContent = error.detail || 'Save failed';
        return;
      }
      closeCustomAgentModal();
      refreshAgents();
    });
    footer.appendChild(save);
  } else {
    const create = button('Create connection', 'admin-btn-add');
    create.addEventListener('click', async () => {
      const label = name.value.trim();
      if (!label) { status.textContent = 'Enter a connection name.'; name.focus(); return; }
      create.disabled = true;
      status.textContent = 'Creating…';
      try {
        const payload = new FormData();
        payload.append('name', label);
        payload.append('agent_provider', 'custom');
        payload.append('scopes', selectedScopes(permissions).join(','));
        const response = await fetch('/api/tokens', { method: 'POST', credentials: 'same-origin', body: payload });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || 'Create failed');
        showCustomAgentResult(data, label);
      } catch (error) {
        status.textContent = error?.message || 'Create failed';
        create.disabled = false;
      }
    });
    footer.appendChild(create);
  }

  modal.appendChild(footer);
  requestAnimationFrame(() => name.focus());
}

function showCustomAgentResult(data, label) {
  const modal = modalShell(
    'Connection created',
    `“${label}” can now authenticate to Odysseus. Copy this credential now — the raw token will not be shown again.`
  );
  if (!modal) return;

  const body = document.createElement('div');
  body.className = 'custom-agent-modal-body';

  const tokenBlock = document.createElement('div');
  tokenBlock.className = 'custom-agent-result-block';
  const tokenLabel = document.createElement('div');
  tokenLabel.className = 'custom-agent-result-label';
  tokenLabel.textContent = 'Odysseus scoped credential';
  const tokenCode = document.createElement('code');
  tokenCode.className = 'custom-agent-result-code';
  tokenCode.textContent = data.token || '';
  tokenBlock.append(tokenLabel, tokenCode);

  const setupBlock = document.createElement('div');
  setupBlock.className = 'custom-agent-result-block';
  const setupLabel = document.createElement('div');
  setupLabel.className = 'custom-agent-result-label';
  setupLabel.textContent = 'Generic CLI setup';
  const setup = document.createElement('pre');
  setup.className = 'custom-agent-result-code';
  setup.textContent = genericSetup(data.token || '');
  setupBlock.append(setupLabel, setup);

  body.append(tokenBlock, setupBlock);
  modal.appendChild(body);

  const footer = document.createElement('div');
  footer.className = 'custom-agent-modal-footer';
  const copyToken = button('Copy credential');
  copyToken.addEventListener('click', async () => {
    copyToken.textContent = await copyText(data.token || '') ? 'Copied' : 'Copy failed';
  });
  const copySetup = button('Copy setup');
  copySetup.addEventListener('click', async () => {
    copySetup.textContent = await copyText(setup.textContent) ? 'Copied' : 'Copy failed';
  });
  const done = button('Done');
  done.addEventListener('click', () => {
    closeCustomAgentModal();
    refreshAgents();
  });
  footer.append(copyToken, copySetup, done);
  modal.appendChild(footer);
}

async function renderCustomAgents(list) {
  if (list.querySelector('.custom-agent-block')) return;

  if (!list.querySelector('.custom-agent-top-action')) {
    const topAction = document.createElement('div');
    topAction.className = 'custom-agent-top-action';
    const add = button('Add Custom Agent');
    add.classList.add('add-custom-agent-btn');
    add.addEventListener('click', () => showCustomAgentForm());
    topAction.appendChild(add);
    list.prepend(topAction);
  }

  let tokens = [];
  try {
    const response = await fetch('/api/tokens', { credentials: 'same-origin' });
    if (response.ok) tokens = (await response.json()).filter(token => token.agent_provider === 'custom');
  } catch (_) {}

  if (!tokens.length) return;

  const block = document.createElement('div');
  block.className = 'custom-agent-block';
  block.style.cssText = 'border-top:1px solid var(--border);padding-top:10px;margin-top:4px;';

  const heading = document.createElement('div');
  heading.style.cssText = 'font-size:12px;font-weight:600;margin-bottom:6px;';
  heading.textContent = `Custom agents (${tokens.length})`;
  block.appendChild(heading);

  tokens.forEach(token => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'custom-agent-row';
    row.style.cssText = 'display:flex;width:100%;align-items:center;gap:8px;padding:8px 10px;margin-bottom:6px;border:1px solid var(--border);border-radius:7px;background:color-mix(in srgb,var(--fg) 3%,transparent);color:inherit;text-align:left;cursor:pointer;';

    const text = document.createElement('span');
    text.style.cssText = 'flex:1;min-width:0;';
    const title = document.createElement('strong');
    title.textContent = token.name || 'Custom CLI';
    title.style.display = 'block';
    const detail = document.createElement('span');
    detail.textContent = `${token.token_prefix || 'token'}… · ${token.last_used_at ? 'Used' : 'Configured'}`;
    detail.style.cssText = 'display:block;font-size:10px;opacity:.55;margin-top:2px;';
    text.append(title, detail);
    row.appendChild(text);
    row.addEventListener('click', () => showCustomAgentForm(token.id));
    block.appendChild(row);
  });
  list.appendChild(block);
}

async function applyCategoryLayout() {
  const list = document.getElementById('unified-integrations-list');
  if (!list) return;

  expandIntegrationsLayout();

  const globalAdd = document.getElementById('unified-intg-add-btn');
  if (globalAdd?.parentElement) globalAdd.parentElement.style.display = 'none';

  // Legacy inline forms should never consume vertical space for custom-agent
  // creation; custom agents use the centered child modal above.
  const inlineForm = document.getElementById('unified-intg-form');
  if (inlineForm && !inlineForm.children.length) inlineForm.style.display = 'none';

  const category = activeCategory();
  if (category === 'overview') {
    stripOverviewAggregate(list);
    return;
  }
  if (category === 'agents') {
    await renderCustomAgents(list);
    return;
  }
  if (CATEGORY_ACTIONS[category]) normalizeCategoryAction(list, category);
}

function init() {
  const list = document.getElementById('unified-integrations-list');
  if (!list || list.dataset.categoryActionsBound === '1') return;
  list.dataset.categoryActionsBound = '1';

  let pending = false;
  const schedule = () => {
    if (pending) return;
    pending = true;
    queueMicrotask(async () => {
      pending = false;
      await applyCategoryLayout();
    });
  };

  new MutationObserver(schedule).observe(list, { childList: true });
  document.querySelectorAll('#intg-category-tabs .intg-cat-btn').forEach(btn => btn.addEventListener('click', schedule));

  const integrationPanel = document.querySelector('[data-settings-panel="integrations"]');
  if (integrationPanel) {
    new MutationObserver(() => {
      syncIntegrationsLayout();
      if (!integrationPanel.classList.contains('hidden')) schedule();
    }).observe(integrationPanel, {
      attributes: true,
      attributeFilter: ['class', 'style'],
    });
  }

  document.querySelectorAll('[data-settings-tab]').forEach(btn => {
    btn.addEventListener('click', () => queueMicrotask(syncIntegrationsLayout));
  });

  syncIntegrationsLayout();
  schedule();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init, { once: true });
} else {
  init();
}
