const fs = require('fs');
const path = require('path');
const vm = require('vm');

// Behavioral harness for the real Settings > Integrations production module.
// The DOM shim deliberately implements only the browser surface used by
// integrationCategoryActions.js so tests fail when that contract changes.

class ClassList {
  constructor(owner) {
    this.owner = owner;
    this.values = new Set();
  }
  add(...names) { names.filter(Boolean).forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    if (force === undefined) force = !this.contains(name);
    force ? this.add(name) : this.remove(name);
    return force;
  }
  toString() { return Array.from(this.values).join(' '); }
}

class Element {
  constructor(tagName, documentRef) {
    this.tagName = String(tagName || 'div').toUpperCase();
    this.ownerDocument = documentRef;
    this.children = [];
    this.parentElement = null;
    this.attributes = {};
    this.dataset = {};
    this.style = { cssText: '' };
    this.classList = new ClassList(this);
    this._textContent = '';
    this._innerHTML = '';
    this._listeners = new Map();
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.type = '';
    this.placeholder = '';
  }

  set id(value) { this.attributes.id = String(value); }
  get id() { return this.attributes.id || ''; }
  set className(value) {
    this.classList.values = new Set(String(value || '').split(/\s+/).filter(Boolean));
  }
  get className() { return this.classList.toString(); }
  set textContent(value) { this._textContent = String(value ?? ''); }
  get textContent() { return this._textContent; }
  set innerHTML(value) {
    const text = String(value ?? '');
    this._innerHTML = text;
    if (/<script\b/i.test(text)) this.ownerDocument.unsafeInnerHtmlAssignments += 1;
    this._textContent = text.replace(/<[^>]*>/g, '');
  }
  get innerHTML() { return this._innerHTML; }
  get isConnected() {
    let node = this;
    while (node) {
      if (node === this.ownerDocument.body || node === this.ownerDocument.head) return true;
      node = node.parentElement;
    }
    return false;
  }
  get nextElementSibling() {
    if (!this.parentElement) return null;
    const siblings = this.parentElement.children;
    const index = siblings.indexOf(this);
    return index >= 0 ? siblings[index + 1] || null : null;
  }

  setAttribute(key, value) {
    const str = String(value);
    this.attributes[key] = str;
    if (key === 'class') this.className = str;
    if (key.startsWith('data-')) {
      const prop = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[prop] = str;
    }
  }
  getAttribute(key) { return this.attributes[key] ?? null; }

  appendChild(child) {
    if (child.parentElement) child.remove();
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  prepend(child) {
    if (child.parentElement) child.remove();
    child.parentElement = this;
    this.children.unshift(child);
  }
  remove() {
    if (!this.parentElement) return;
    const siblings = this.parentElement.children;
    const index = siblings.indexOf(this);
    if (index >= 0) siblings.splice(index, 1);
    this.parentElement = null;
  }

  addEventListener(type, handler) {
    if (!this._listeners.has(type)) this._listeners.set(type, new Set());
    this._listeners.get(type).add(handler);
  }
  removeEventListener(type, handler) { this._listeners.get(type)?.delete(handler); }
  async dispatchEvent(event) {
    event.target ||= this;
    event.currentTarget = this;
    const handlers = Array.from(this._listeners.get(event.type) || []);
    for (const handler of handlers) await handler.call(this, event);
  }
  async click() { await this.dispatchEvent({ type: 'click', preventDefault() {}, stopPropagation() {} }); }
  focus() { this.ownerDocument.activeElement = this; }
  select() {}

  matches(selector) { return matchesSelector(this, selector); }
  querySelector(selector) { return queryAll(this, selector)[0] || null; }
  querySelectorAll(selector) { return queryAll(this, selector); }
  closest(selector) {
    let node = this;
    while (node) {
      if (matchesSelector(node, selector)) return node;
      node = node.parentElement;
    }
    return null;
  }
}

function parseSimpleSelector(selector) {
  let rest = selector.trim();
  const parsed = { id: null, classes: [], attrs: [], checked: false };
  if (rest.endsWith(':checked')) {
    parsed.checked = true;
    rest = rest.slice(0, -8);
  }
  const id = rest.match(/#([\w-]+)/);
  if (id) parsed.id = id[1];
  parsed.classes = Array.from(rest.matchAll(/\.([\w-]+)/g), match => match[1]);
  parsed.attrs = Array.from(rest.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g), match => [match[1], match[2]]);
  return parsed;
}

function matchesSelector(element, selector) {
  const parsed = parseSimpleSelector(selector);
  if (parsed.id && element.id !== parsed.id) return false;
  if (parsed.classes.some(name => !element.classList.contains(name))) return false;
  for (const [key, expected] of parsed.attrs) {
    const actual = element.getAttribute(key);
    if (actual === null) return false;
    if (expected !== undefined && actual !== expected) return false;
  }
  if (parsed.checked && !element.checked) return false;
  return true;
}

function descendants(root) {
  const out = [];
  const visit = node => {
    for (const child of node.children || []) {
      out.push(child);
      visit(child);
    }
  };
  visit(root);
  return out;
}

function queryAll(root, selector) {
  const parts = selector.trim().split(/\s+/);
  const candidates = descendants(root);
  if (parts.length === 1) return candidates.filter(node => matchesSelector(node, parts[0]));

  const last = parts.pop();
  return candidates.filter(node => {
    if (!matchesSelector(node, last)) return false;
    let ancestor = node.parentElement;
    for (let i = parts.length - 1; i >= 0; i -= 1) {
      while (ancestor && !matchesSelector(ancestor, parts[i])) ancestor = ancestor.parentElement;
      if (!ancestor) return false;
      ancestor = ancestor.parentElement;
    }
    return true;
  });
}

class DocumentShim {
  constructor() {
    this.unsafeInnerHtmlAssignments = 0;
    this.listeners = new Map();
    this.readyState = 'loading';
    this.head = new Element('head', this);
    this.body = new Element('body', this);
    this.activeElement = this.body;
  }
  createElement(tag) { return new Element(tag, this); }
  getElementById(id) {
    return [this.head, this.body, ...descendants(this.head), ...descendants(this.body)]
      .find(node => node.id === id) || null;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const matches = [];
    for (const root of [this.head, this.body]) {
      if (matchesSelector(root, selector)) matches.push(root);
      matches.push(...queryAll(root, selector));
    }
    return matches;
  }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(handler);
  }
  removeEventListener(type, handler) { this.listeners.get(type)?.delete(handler); }
  listenerCount(type) { return this.listeners.get(type)?.size || 0; }
}

function response(data, ok = true) {
  return { ok, json: async () => data };
}

function buildFixture() {
  const document = new DocumentShim();
  const settings = document.createElement('div');
  settings.id = 'settings-modal';
  const modal = document.createElement('div');
  modal.className = 'modal-content';
  settings.appendChild(modal);
  document.body.appendChild(settings);

  const panel = document.createElement('section');
  panel.setAttribute('data-settings-panel', 'integrations');
  modal.appendChild(panel);

  const host = document.createElement('div');
  panel.appendChild(host);
  const tabs = document.createElement('div');
  tabs.id = 'intg-category-tabs';
  host.appendChild(tabs);
  const agentsTab = document.createElement('button');
  agentsTab.className = 'intg-cat-btn active';
  agentsTab.setAttribute('data-cat', 'agents');
  tabs.appendChild(agentsTab);

  const list = document.createElement('div');
  list.id = 'unified-integrations-list';
  host.appendChild(list);

  return { document, modal, panel, list };
}

async function run() {
  const { document, modal, panel, list } = buildFixture();
  const sourcePath = path.join(__dirname, '../../static/js/integrationCategoryActions.js');
  const source = fs.readFileSync(sourcePath, 'utf8');

  let tokens = [];
  const context = {
    console,
    document,
    window: {
      location: { origin: 'http://localhost:7000' },
      isSecureContext: false,
      confirm: () => true,
    },
    navigator: { clipboard: null },
    fetch: async (url) => {
      if (url === '/api/tokens') return response(tokens);
      return response({});
    },
    getComputedStyle: () => ({ position: 'static' }),
    requestAnimationFrame: fn => fn(),
    queueMicrotask,
    FormData: class { append() {} },
    MutationObserver: class { observe() {} disconnect() {} },
  };
  vm.createContext(context);
  vm.runInContext(source, context, { filename: sourcePath });

  const results = [];
  const check = (test, pass, detail = '') => results.push({ test, pass: Boolean(pass), detail });

  check(
    'Integrations layout expands while panel is active',
    context.syncIntegrationsLayout() === true && modal.classList.contains('settings-integrations-expanded'),
  );
  panel.classList.add('hidden');
  check(
    'Integrations layout restores outside panel',
    context.syncIntegrationsLayout() === false && !modal.classList.contains('settings-integrations-expanded'),
  );
  panel.classList.remove('hidden');

  tokens = [
    { id: 'custom-a', name: 'Same name', agent_provider: 'custom', scopes: ['chat', 'email:read'], token_prefix: 'ody_a' },
    { id: 'custom-b', name: 'Same name', agent_provider: 'custom', scopes: ['chat', 'documents:write'], token_prefix: 'ody_b' },
    { id: 'codex-a', name: 'Looks custom', agent_provider: 'codex', scopes: ['chat', 'documents:write'], token_prefix: 'ody_c' },
    { id: 'api-a', name: '<script>alert(1)</script>', agent_provider: null, scopes: ['chat', 'documents:write'], token_prefix: 'ody_d' },
  ];
  await context.renderCustomAgents(list);
  const rows = list.querySelectorAll('.custom-agent-row');
  check('Custom-agent rendering uses explicit provider identity', rows.length === 2, `rendered=${rows.length}`);
  check('Duplicate display names remain separate connections', rows.length === 2 && rows[0] !== rows[1]);
  check('Renderer does not assign untrusted names through innerHTML', document.unsafeInnerHtmlAssignments === 0);

  await rows[1].click();
  const scopeControls = document.querySelectorAll('.custom-agent-scope');
  const docsWrite = scopeControls.find(control => control.dataset.scope === 'documents:write');
  const emailRead = scopeControls.find(control => control.dataset.scope === 'email:read');
  check('Duplicate-name edit resolves stable token ID', docsWrite?.checked === true && emailRead?.checked === false);
  context.closeCustomAgentModal();

  context.modalShell('Test', 'First');
  check('Custom-agent modal installs one Escape listener', document.listenerCount('keydown') === 1, `listeners=${document.listenerCount('keydown')}`);
  context.closeCustomAgentModal();
  check('Custom-agent modal removes Escape listener on close', document.listenerCount('keydown') === 0, `listeners=${document.listenerCount('keydown')}`);
  context.modalShell('Test', 'Second');
  context.closeCustomAgentModal();
  check('Repeated modal cycles do not accumulate listeners', document.listenerCount('keydown') === 0, `listeners=${document.listenerCount('keydown')}`);

  const actionHost = document.createElement('div');
  const action = document.createElement('button');
  action.className = 'intg-cat-add-btn';
  actionHost.appendChild(action);
  const actionList = document.createElement('div');
  actionList.appendChild(actionHost);
  context.normalizeCategoryAction(actionList, 'calendar');
  check('Category action receives contextual label', action.textContent === 'Add Calendar');

  process.stdout.write(JSON.stringify(results));
}

run().catch(error => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
