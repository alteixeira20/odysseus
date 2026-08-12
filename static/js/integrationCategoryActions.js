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

const CUSTOM_SCOPES = [
  ['todos:read', 'Todos read'],
  ['todos:write', 'Todos write'],
  ['documents:read', 'Documents read'],
  ['documents:write', 'Documents write'],
  ['email:read', 'Email read'],
  ['email:draft', 'Email drafts'],
  ['email:send', 'Email send'],
  ['calendar:read', 'Calendar read'],
  ['calendar:write', 'Calendar write'],
  ['memory:read', 'Memory read'],
  ['memory:write', 'Memory write'],
  ['cookbook:read', 'Cookbook read'],
  ['cookbook:launch', 'Cookbook launch'],
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
    'gap:5px',
    'font-size:11px',
    'font-weight:600',
    'color:var(--accent,var(--red))',
    'border:1px solid color-mix(in srgb,var(--accent,var(--red)) 40%,transparent)',
    'background:color-mix(in srgb,var(--accent,var(--red)) 8%,transparent)',
    'border-radius:4px',
    'padding:4px 9px',
    'cursor:pointer',
  ].join(';');
  return el;
}

function actionFooter() {
  const wrap = document.createElement('div');
  wrap.className = 'intg-category-action-footer';
  wrap.style.cssText = 'display:flex;justify-content:flex-end;margin-top:10px;';
  return wrap;
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

function moveCategoryActionToFooter(list, category) {
  const existing = list.querySelector('.intg-cat-add-btn');
  if (!existing || list.querySelector('.intg-category-action-footer')) return;

  const header = existing.parentElement;
  const footer = actionFooter();
  existing.textContent = CATEGORY_ACTIONS[category] || existing.textContent;
  footer.appendChild(existing);
  list.appendChild(footer);

  if (header) {
    header.style.justifyContent = 'flex-start';
  }
}

function scopeControl(scope, label, checked) {
  const row = document.createElement('label');
  row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 0;font-size:11px;';

  const text = document.createElement('span');
  text.textContent = label;
  text.style.flex = '1';

  const input = document.createElement('input');
  input.type = 'checkbox';
  input.className = 'custom-agent-scope';
  input.dataset.scope = scope;
  input.checked = checked;

  row.append(text, input);
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

async function showCustomAgentForm(tokenId = null) {
  const form = document.getElementById('unified-intg-form');
  if (!form) return;

  let current = null;
  if (tokenId) {
    try {
      const response = await fetch('/api/tokens', { credentials: 'same-origin' });
      if (response.ok) {
        current = (await response.json()).find(token => String(token.id) === String(tokenId)) || null;
      }
    } catch (_) {}
  }

  form.innerHTML = '';
  form.style.display = '';

  const card = document.createElement('div');
  card.className = 'admin-card';
  card.style.marginTop = '8px';

  const title = document.createElement('h2');
  title.style.fontSize = '13px';
  title.textContent = current ? 'Edit Custom Agent' : 'Add Custom Agent';
  card.appendChild(title);

  const description = document.createElement('div');
  description.style.cssText = 'font-size:11px;opacity:.65;line-height:1.4;margin-bottom:10px;';
  description.textContent = 'Create a scoped Odysseus credential for an external CLI that can call the shared agent API. The CLI keeps its own model-provider authentication.';
  card.appendChild(description);

  const nameRow = document.createElement('div');
  nameRow.className = 'settings-row';
  const nameLabel = document.createElement('label');
  nameLabel.className = 'settings-label';
  nameLabel.textContent = 'Name';
  const name = document.createElement('input');
  name.className = 'settings-input';
  name.placeholder = 'Custom CLI';
  name.value = current?.name || '';
  nameRow.append(nameLabel, name);
  card.appendChild(nameRow);

  const permTitle = document.createElement('div');
  permTitle.style.cssText = 'font-size:11px;font-weight:600;opacity:.65;margin:10px 0 4px;';
  permTitle.textContent = 'Permissions';
  card.appendChild(permTitle);

  const scopeSet = new Set(current?.scopes || []);
  const permissions = document.createElement('div');
  permissions.className = 'custom-agent-permissions';
  CUSTOM_SCOPES.forEach(([scope, label]) => permissions.appendChild(scopeControl(scope, label, scopeSet.has(scope))));
  card.appendChild(permissions);

  const status = document.createElement('div');
  status.style.cssText = 'font-size:11px;min-height:15px;margin-top:8px;';
  card.appendChild(status);

  const controls = document.createElement('div');
  controls.style.cssText = 'display:flex;justify-content:flex-end;gap:6px;margin-top:8px;';
  const cancel = button('Cancel', 'admin-btn-add');
  cancel.addEventListener('click', () => { form.style.display = 'none'; form.innerHTML = ''; });
  controls.appendChild(cancel);

  if (current) {
    const revoke = button('Revoke', 'admin-btn-add');
    revoke.style.color = 'var(--color-error,var(--red))';
    revoke.addEventListener('click', async () => {
      const confirmFn = window.styledConfirm || (async message => window.confirm(message));
      if (!await confirmFn(`Revoke "${current.name}"?`, { confirmText: 'Revoke', danger: true })) return;
      const response = await fetch(`/api/tokens/${current.id}`, { method: 'DELETE', credentials: 'same-origin' });
      if (!response.ok) { status.textContent = 'Revoke failed'; return; }
      form.style.display = 'none';
      form.innerHTML = '';
      refreshAgents();
    });
    controls.appendChild(revoke);

    const save = button('Save', 'admin-btn-add');
    save.addEventListener('click', async () => {
      const label = name.value.trim();
      if (!label) { status.textContent = 'Name required'; return; }
      const response = await fetch(`/api/tokens/${current.id}`, {
        method: 'PATCH', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: label, scopes: selectedScopes(permissions), agent_provider: 'custom' }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        status.textContent = error.detail || 'Save failed';
        return;
      }
      status.textContent = 'Saved';
      refreshAgents();
    });
    controls.appendChild(save);
  } else {
    const create = button('Create connection', 'admin-btn-add');
    create.addEventListener('click', async () => {
      const label = name.value.trim();
      if (!label) { status.textContent = 'Name required'; return; }
      create.disabled = true;
      status.textContent = 'Creating…';
      try {
        const body = new FormData();
        body.append('name', label);
        body.append('agent_provider', 'custom');
        body.append('scopes', selectedScopes(permissions).join(','));
        const response = await fetch('/api/tokens', { method: 'POST', credentials: 'same-origin', body });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || 'Create failed');

        permissions.style.display = 'none';
        nameRow.style.display = 'none';
        permTitle.style.display = 'none';
        controls.style.display = 'none';
        description.textContent = 'Connection created. Copy the credential now; Odysseus will not show the raw token again.';
        status.textContent = '';

        const tokenLabel = document.createElement('div');
        tokenLabel.style.cssText = 'font-size:11px;font-weight:600;margin:8px 0 4px;';
        tokenLabel.textContent = 'Scoped credential';
        card.appendChild(tokenLabel);

        const tokenCode = document.createElement('code');
        tokenCode.textContent = data.token || '';
        tokenCode.style.cssText = 'display:block;word-break:break-all;padding:7px 8px;background:rgba(0,0,0,.08);border-radius:4px;font-size:11px;';
        card.appendChild(tokenCode);

        const setupLabel = document.createElement('div');
        setupLabel.style.cssText = 'font-size:11px;font-weight:600;margin:12px 0 4px;';
        setupLabel.textContent = 'Generic setup';
        card.appendChild(setupLabel);

        const setup = document.createElement('pre');
        setup.style.cssText = 'white-space:pre;overflow:auto;padding:8px;background:rgba(0,0,0,.08);border-radius:4px;font-size:10px;';
        setup.textContent = genericSetup(data.token || '');
        card.appendChild(setup);

        const doneRow = document.createElement('div');
        doneRow.style.cssText = 'display:flex;justify-content:flex-end;gap:6px;margin-top:8px;';
        const copy = button('Copy setup');
        copy.addEventListener('click', async () => {
          copy.textContent = await copyText(setup.textContent) ? 'Copied' : 'Copy failed';
        });
        const done = button('Done');
        done.addEventListener('click', () => {
          form.style.display = 'none';
          form.innerHTML = '';
          refreshAgents();
        });
        doneRow.append(copy, done);
        card.appendChild(doneRow);
      } catch (error) {
        status.textContent = error?.message || 'Create failed';
      } finally {
        create.disabled = false;
      }
    });
    controls.appendChild(create);
  }

  card.appendChild(controls);
  form.appendChild(card);
  name.focus();
}

async function renderCustomAgents(list) {
  if (list.querySelector('.custom-agent-block')) return;

  let tokens = [];
  try {
    const response = await fetch('/api/tokens', { credentials: 'same-origin' });
    if (response.ok) tokens = (await response.json()).filter(token => token.agent_provider === 'custom');
  } catch (_) {}

  if (tokens.length) {
    const block = document.createElement('div');
    block.className = 'custom-agent-block';
    block.style.cssText = 'border-top:1px solid var(--border);padding-top:10px;margin-top:2px;';

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

  if (!list.querySelector('.intg-category-action-footer')) {
    const footer = actionFooter();
    const add = button('Add Custom Agent');
    add.classList.add('add-custom-agent-btn');
    add.addEventListener('click', () => showCustomAgentForm());
    footer.appendChild(add);
    list.appendChild(footer);
  }
}

async function applyCategoryLayout() {
  const list = document.getElementById('unified-integrations-list');
  if (!list) return;

  const globalAdd = document.getElementById('unified-intg-add-btn');
  if (globalAdd?.parentElement) globalAdd.parentElement.style.display = 'none';

  const category = activeCategory();
  if (category === 'overview') {
    stripOverviewAggregate(list);
    return;
  }
  if (category === 'agents') {
    await renderCustomAgents(list);
    return;
  }
  if (CATEGORY_ACTIONS[category]) moveCategoryActionToFooter(list, category);
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
  schedule();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init, { once: true });
} else {
  init();
}
