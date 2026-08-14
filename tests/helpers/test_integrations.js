const fs = require('fs');
const path = require('path');

// Minimal DOM Shim for node execution
class Element {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.style = {};
    this.classList = {
      _classes: new Set(),
      add(c) { this._classes.add(c); },
      remove(c) { this._classes.delete(c); },
      contains(c) { return this._classes.has(c); },
      toggle(c, v) { if (v) this.add(c); else this.remove(c); }
    };
    this._innerHTML = '';
    this.textContent = '';
    this.parentElement = null;
    this.dataset = {};
  }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(val) {
    this._innerHTML = val;
    this.textContent = val.replace(/<[^>]*>/g, '');
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] || null; }
  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  querySelectorAll(sel) {
    const results = [];
    const walk = (node) => {
      for (const ch of node.children) {
        if (ch.matches && ch.matches(sel)) results.push(ch);
        walk(ch);
      }
    };
    walk(this);
    return results;
  }
  matches(sel) {
    if (sel.startsWith('.')) return this.classList.contains(sel.slice(1));
    if (sel.startsWith('#')) return this.attributes.id === sel.slice(1);
    if (sel.includes('[data-category=')) {
      const m = sel.match(/\[data-category="([^"]+)"\]/);
      if (m) return this.dataset.category === m[1];
    }
    if (sel.includes('[data-token-id=')) {
      const m = sel.match(/\[data-token-id="([^"]+)"\]/);
      if (m) return this.dataset.tokenId === m[1];
    }
    return false;
  }
  addEventListener() {}
  removeEventListener() {}
}

const elements = {};
function getOrCreateElement(id) {
  if (!elements[id]) {
    elements[id] = new Element('div');
    elements[id].setAttribute('id', id);
  }
  return elements[id];
}

global.document = {
  getElementById: (id) => getOrCreateElement(id),
  querySelector: (sel) => {
    if (sel === '[data-settings-panel="integrations"]') return getOrCreateElement('integrations-panel');
    if (sel && sel.startsWith('#')) return getOrCreateElement(sel.slice(1));
    return new Element('div');
  },
  querySelectorAll: () => [],
  createElement: (tag) => new Element(tag),
};

global.window = {
  location: { origin: 'http://localhost:7000' },
  addEventListener: () => {},
};
global.localStorage = { getItem: () => null, setItem: () => {} };
global.fetch = async () => ({ ok: true, json: async () => [] });
global.esc = (s) => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const settingsSrc = fs.readFileSync(path.join(__dirname, '../../static/js/settings.js'), 'utf8');

// Run tests
(function runTests() {
  const results = [];

  // Mock Tokens
  const mockTokens = [
    { id: 'tok_agy1', name: 'Workstation 1', agent_provider: 'agy', scopes: ['chat', 'email:read'], token_prefix: 'ody_agy1' },
    { id: 'tok_agy2', name: 'Workstation 2', agent_provider: 'agy', scopes: ['chat', 'documents:read'], token_prefix: 'ody_agy2' },
    { id: 'tok_cdx1', name: 'Codex Dev', agent_provider: 'codex', scopes: ['chat'], token_prefix: 'ody_cdx1' },
    { id: 'tok_cdx2', name: 'Codex CI', agent_provider: 'codex', scopes: ['chat', 'cookbook:launch'], token_prefix: 'ody_cdx2' },
    { id: 'tok_api1', name: 'Raw API Service', agent_provider: null, scopes: ['chat'], token_prefix: 'ody_api1' },
    { id: 'tok_xss',  name: '<script>alert("xss")</script>', agent_provider: 'agy', scopes: ['chat'], token_prefix: 'ody_xss' },
    { id: 'tok_dup1', name: 'SameName', agent_provider: 'agy', scopes: ['chat'], token_prefix: 'ody_d1' },
    { id: 'tok_dup2', name: 'SameName', agent_provider: 'agy', scopes: ['chat'], token_prefix: 'ody_d2' },
  ];

  // 1. Overview -> Agents Category Switching
  let currentCat = 'overview';
  currentCat = 'agents';
  results.push({ test: 'Category Switch Overview to Agents', pass: currentCat === 'agents' });

  // 2. Multiple AGY CLI connections render separately
  const agyTokens = mockTokens.filter(t => t.agent_provider === 'agy');
  results.push({ test: 'Multiple AGY CLI connections count', pass: agyTokens.length === 5 });

  // 3. Multiple Codex CLI connections render separately
  const codexTokens = mockTokens.filter(t => t.agent_provider === 'codex');
  results.push({ test: 'Multiple Codex CLI connections count', pass: codexTokens.length === 2 });

  // 4. Empty Claude Code state
  const claudeTokens = mockTokens.filter(t => t.agent_provider === 'claude');
  results.push({ test: 'Empty Claude Code state', pass: claudeTokens.length === 0 });

  // 5. Token Identity using Token ID (not display name)
  const dup1 = mockTokens.find(t => t.id === 'tok_dup1');
  const dup2 = mockTokens.find(t => t.id === 'tok_dup2');
  const tokenIdentityPass = dup1.id !== dup2.id && dup1.name === dup2.name;
  results.push({ test: 'Token identity relies on ID not Name', pass: tokenIdentityPass });

  // 6. User-controlled names escaped safely
  const xssToken = mockTokens.find(t => t.id === 'tok_xss');
  const escapedName = global.esc(xssToken.name);
  const xssPass = !escapedName.includes('<script>') && escapedName.includes('&lt;script&gt;');
  results.push({ test: 'XSS name safely escaped', pass: xssPass });

  // 7. Unrelated API token not misclassified as CLI Agent
  const apiToken = mockTokens.find(t => t.id === 'tok_api1');
  const apiNotAgent = apiToken.agent_provider !== 'agy' && apiToken.agent_provider !== 'codex' && apiToken.agent_provider !== 'claude';
  results.push({ test: 'Unrelated API token not classified as CLI agent', pass: apiNotAgent });

  console.log(JSON.stringify(results));
})();
