import test from 'node:test';
import assert from 'node:assert/strict';
import { createNotesState } from '../frontend/src/features/notes/application/state.js';
import { createNotesWorkflows } from '../frontend/src/features/notes/application/workflows.js';

test('views cannot mutate Notes state, but captured identities observe application updates', () => {
  const app = createNotesState();
  app.replaceNotes([{ id: 'a', items: [{ text: 'Task', done: false }] }]);
  const note = app.state.notes[0];
  assert.throws(() => { note.items[0].done = true; }, /state operations/);
  assert.throws(() => app.state.notes.push({ id: 'other' }), /state operations/);
  assert.throws(() => app.state.selectedIds.add('a'), /selection operations/);
  assert.throws(() => Object.defineProperty(note, 'title', { value: 'unowned' }), /state operations/);
  assert.throws(() => Object.preventExtensions(note), /state operations/);
  assert.throws(() => Object.getOwnPropertyDescriptor(note, 'items').value.push({}), /state operations/);
  app.updateItem(note, 0, { done: true });
  assert.equal(note.items[0].done, true);
  assert.equal(app.selectId('a'), undefined, 'selection operations do not leak the mutable set');
  assert.deepEqual([...app.state.selectedIds], ['a']);
  assert.throws(() => app.state.selectedIds.valueOf().clear(), /selection operations/);
  app.state.selectedIds.forEach((_value, _key, set) => {
    assert.equal(set, app.state.selectedIds);
    assert.throws(() => set.clear(), /selection operations/);
  });
  app.mergeNote(note, { title: 'Changed' });
  assert.equal(note.title, 'Changed');
});

test('fetch preserves archived query, failures clear notes, and successful fallback shapes', async () => {
  const app = createNotesState(), requests = [];
  let response = { ok: true, json: async () => [{ id: 'a' }] };
  const workflows = createNotesWorkflows({ app, api: { listNotes: async archived => { requests.push(archived); return response; } }, effects: {} });
  app.setShowingArchived(true);
  await workflows._fetchNotes();
  assert.deepEqual(requests, [true]);
  assert.equal(app.state.loading, false);
  assert.equal(app.state.notes[0].id, 'a');
  response = { ok: false };
  await workflows._fetchNotes();
  assert.equal(app.state.notes.length, 0);
  response = { ok: true, json: async () => ({ notes: [{ id: 'b' }] }) };
  await workflows._fetchNotes();
  assert.equal(app.state.notes[0].id, 'b');
});

test('fetch completions retain the original last-completion ordering', async () => {
  const app = createNotesState(), releases = [];
  const workflows = createNotesWorkflows({ app, api: { listNotes: () => new Promise(resolve => releases.push(resolve)) }, effects: {} });
  const first = workflows._fetchNotes(), second = workflows._fetchNotes();
  releases[1]({ ok: true, json: async () => ({ notes: [{ id: 'second' }] }) });
  await second;
  assert.equal(app.state.notes[0].id, 'second');
  releases[0]({ ok: true, json: async () => ({ notes: [{ id: 'first' }] }) });
  await first;
  assert.equal(app.state.notes[0].id, 'first');
});
