/**
 * Permanent deletion contract tests (web/server/store.mjs).
 *
 * Every assertion about "gone" is made against the raw SQLite tables, not only
 * against the public API, so a hidden copy or an untouched annotation row fails
 * the suite. All fixtures are synthetic; no real export is ever mutated.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import {
  ACCOUNT_A, ACCOUNT_B, CONVERSATION_A, CONVERSATION_C, UUID,
  hashExport, listExportFiles,
} from './helpers/fixture.mjs';
import {
  capture, columnNames, countRows, expectStatus, openStoreSet,
  requireMethods, scanForValue, sourceLines,
} from './helpers/store-tools.mjs';

const REQUIRED = [
  'deleteMessage', 'deleteMessages', 'deleteConversation', 'deleteConversations',
];

let ChatStore = null;
let storeModuleError = null;
try {
  ChatStore = (await import('../server/store.mjs')).ChatStore;
} catch (error) {
  storeModuleError = error;
}
if (storeModuleError) {
  throw new Error(`web/server/store.mjs could not be imported: ${storeModuleError.code || storeModuleError.message}`);
}
requireMethods(ChatStore, REQUIRED);

const ACCOUNT_A_MESSAGES = 7;

function sourceByUuid(fixture, uid) {
  const map = new Map();
  for (const line of sourceLines(fixture, uid)) map.set(JSON.parse(line).msg_uuid, line);
  return map;
}

test('single message delete removes the SQL row, its edit and its raw content', async (t) => {
  const { fixture, store, raw } = await openStoreSet(t);
  const db = raw();

  store.updateMessage(ACCOUNT_A, UUID.text, {
    text_override: 'edited before delete', note: 'note before delete', expected_revision: 0,
  });
  assert.equal(countRows(db, 'message_edits', 'WHERE msg_uuid = ?', [UUID.text]), 1);

  const source = sourceByUuid(fixture, ACCOUNT_A);
  const result = store.deleteMessage(ACCOUNT_A, UUID.text, 1);

  assert.equal(result.deleted, true);
  assert.equal(result.account_uid, ACCOUNT_A);
  assert.equal(result.msg_uuid, UUID.text);
  assert.deepEqual(result.msg_uuids, [UUID.text]);
  assert.equal(result.deleted_messages, 1);
  assert.equal(result.deleted_conversations, 0);

  // SQL absence, not just API absence.
  assert.equal(countRows(db, 'messages', 'WHERE msg_uuid = ?', [UUID.text]), 0);
  assert.equal(countRows(db, 'message_edits', 'WHERE msg_uuid = ?', [UUID.text]), 0);
  assert.deepEqual(scanForValue(db, UUID.text), [], 'the deleted uuid survives in some table');
  assert.deepEqual(scanForValue(db, 'hello [smile] world'), [],
    'the deleted original text survives in some table');
  assert.deepEqual(scanForValue(db, 'edited before delete'), [],
    'the deleted annotation survives in some table');

  const conversation = db.prepare('SELECT * FROM conversations WHERE uid = ? AND conversation_id = ?')
    .get(ACCOUNT_A, CONVERSATION_A);
  assert.equal(Number(conversation.message_count), ACCOUNT_A_MESSAGES - 1);
  assert.ok(conversation.first_time !== null && conversation.last_time !== null);

  // The remaining rows must be byte-identical to their source JSON lines.
  const listed = store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, limit: 500 });
  assert.equal(listed.total, ACCOUNT_A_MESSAGES - 1);
  for (const item of listed.items) {
    assert.equal(item.raw_json, source.get(item.msg_uuid), `raw_json changed for ${item.msg_uuid}`);
  }

  await assert.rejects(() => Promise.resolve().then(() => store.getMessage(ACCOUNT_A, UUID.text)),
    (error) => error.status === 404);
});

test('a single message can only be deleted once', async (t) => {
  const { store } = await openStoreSet(t);
  store.deleteMessage(ACCOUNT_A, UUID.text, 0);
  const again = await capture(() => store.deleteMessage(ACCOUNT_A, UUID.text, 0));
  expectStatus(again, 404, 'second delete of the same message');
});

test('batch message delete is atomic when one revision is stale', async (t) => {
  const { fixture, store, raw } = await openStoreSet(t);
  const db = raw();

  store.updateMessage(ACCOUNT_A, UUID.formula, { note: 'kept', expected_revision: 0 });

  const stale = await capture(() => store.deleteMessages(ACCOUNT_A, [
    { msg_uuid: UUID.text, expected_revision: 0 },
    { msg_uuid: UUID.formula, expected_revision: 0 },
  ]));
  expectStatus(stale, 409, 'stale batch');

  assert.equal(countRows(db, 'messages', 'WHERE uid = ?', [ACCOUNT_A]), ACCOUNT_A_MESSAGES,
    'a failed batch deleted rows');
  assert.equal(countRows(db, 'message_edits', 'WHERE msg_uuid = ?', [UUID.formula]), 1,
    'a failed batch dropped an annotation');
  assert.equal(store.getMessage(ACCOUNT_A, UUID.formula).edit.revision, 1);
  assert.equal(countRows(db, 'deletions'), 0, 'a failed batch recorded a deletion hash');

  const ok = store.deleteMessages(ACCOUNT_A, [
    { msg_uuid: UUID.text, expected_revision: 0 },
    { msg_uuid: UUID.formula, expected_revision: 1 },
  ]);
  assert.equal(ok.deleted_messages, 2);
  assert.deepEqual([...ok.msg_uuids].sort(), [UUID.text, UUID.formula].sort());
  assert.equal(countRows(db, 'messages', 'WHERE uid = ?', [ACCOUNT_A]), ACCOUNT_A_MESSAGES - 2);
});

test('malformed batches are rejected without deleting anything', async (t) => {
  const { fixture, store, raw } = await openStoreSet(t);
  const db = raw();

  const big = Array.from({ length: 501 }, (unused, index) => ({
    msg_uuid: `dddddddd-0000-4000-8000-${String(index).padStart(12, '0')}`,
    expected_revision: 0,
  }));
  const cases = [
    [undefined, 'missing array'],
    [[], 'empty array'],
    ['not-an-array', 'non-array'],
    [[{ msg_uuid: UUID.text, expected_revision: -1 }], 'negative revision'],
    [[{ msg_uuid: UUID.text, expected_revision: 0 }, { msg_uuid: UUID.text, expected_revision: 0 }], 'duplicate keys'],
    [[{ msg_uuid: UUID.text }], 'missing revision'],
    [[{ msg_uuid: '', expected_revision: 0 }], 'empty key'],
    [big, 'more than 500 items'],
  ];

  for (const [value, label] of cases) {
    const outcome = await capture(() => store.deleteMessages(ACCOUNT_A, value));
    assert.equal(outcome.ok, false, `${label}: was accepted`);
    assert.ok([400, 404, 413].includes(outcome.error.status),
      `${label}: unexpected status ${outcome.error.status}`);
  }

  const unknown = await capture(() => store.deleteMessages(ACCOUNT_A, [
    { msg_uuid: 'no-such-uuid', expected_revision: 0 },
  ]));
  expectStatus(unknown, 404, 'unknown key');

  const otherAccount = await capture(() => store.deleteMessage(ACCOUNT_B, UUID.text, 0));
  expectStatus(otherAccount, 404, 'cross-account message delete');

  assert.equal(countRows(db, 'messages', 'WHERE uid = ?', [ACCOUNT_A]), ACCOUNT_A_MESSAGES,
    'a rejected batch deleted rows');
  assert.equal(countRows(db, 'deletions'), 0);
});

test('the deletions table stores only a hash, a kind and a timestamp', async (t) => {
  const { fixture, store, raw } = await openStoreSet(t);
  const db = raw();

  store.deleteMessage(ACCOUNT_A, UUID.text, 0);

  assert.deepEqual([...columnNames(db, 'deletions')].sort(), ['deleted_at', 'key_hash', 'kind'],
    'the deletions table must not grow content columns');
  const rows = db.prepare('SELECT * FROM deletions').all();
  assert.equal(rows.length, 1);
  for (const row of rows) {
    assert.equal(typeof row.kind, 'string');
    assert.ok(row.kind.length > 0);
    assert.match(String(row.key_hash), /^[0-9a-f]{64}$/, 'key_hash must be a SHA-256 hex digest');
    assert.ok(!Number.isNaN(Date.parse(String(row.deleted_at))), 'deleted_at must be a timestamp');
    assert.ok(!JSON.stringify(row).includes(UUID.text), 'the raw key leaked into deletions');
    assert.ok(!JSON.stringify(row).includes('hello [smile] world'), 'content leaked into deletions');
  }
});

test('deleting the last message removes the empty conversation but keeps the account', async (t) => {
  const { fixture, store, raw } = await openStoreSet(t);
  const db = raw();

  const result = store.deleteMessage(ACCOUNT_B, UUID.conversationB, 0);
  assert.equal(result.deleted_messages, 1);
  assert.equal(result.deleted_conversations, 1, 'the emptied conversation should be reported');

  assert.equal(countRows(db, 'messages', 'WHERE uid = ?', [ACCOUNT_B]), 0);
  assert.equal(countRows(db, 'conversations', 'WHERE uid = ?', [ACCOUNT_B]), 0,
    'the empty conversation row should be removed');
  assert.equal(store.listConversations(ACCOUNT_B).total, 0);
  assert.equal(store.listMessages(ACCOUNT_B, { limit: 500 }).total, 0);
  // The account row may remain, but it must not claim messages it no longer has.
  const account = store.listAccounts().find((item) => item.uid === ACCOUNT_B);
  if (account) assert.equal(account.message_count, 0);
});

test('deletion in one account never touches the other account', async (t) => {
  const { fixture, store, raw } = await openStoreSet(t);
  const db = raw();

  store.updateMessage(ACCOUNT_B, UUID.conversationB, { note: 'B note', expected_revision: 0 });
  store.deleteMessages(ACCOUNT_A, [
    { msg_uuid: UUID.text, expected_revision: 0 },
    { msg_uuid: UUID.sticker, expected_revision: 0 },
  ]);

  assert.equal(countRows(db, 'messages', 'WHERE uid = ?', [ACCOUNT_B]), 1);
  assert.equal(countRows(db, 'message_edits', 'WHERE uid = ?', [ACCOUNT_B]), 1);
  assert.equal(store.getMessage(ACCOUNT_B, UUID.conversationB).text_original, 'second conversation');
  assert.equal(store.listConversations(ACCOUNT_B).total, 1);
  assert.equal(store.listMessages(ACCOUNT_A, { limit: 500 }).total, ACCOUNT_A_MESSAGES - 2);
});

test('conversation delete removes every row, including hidden messages', async (t) => {
  const { fixture, store, raw } = await openStoreSet(t);
  const db = raw();

  store.updateMessage(ACCOUNT_A, UUID.sticker, { hidden: true, expected_revision: 0 });
  const result = store.deleteConversation(ACCOUNT_A, CONVERSATION_A, ACCOUNT_A_MESSAGES);

  assert.equal(result.deleted, true);
  assert.equal(result.conversation_id, CONVERSATION_A);
  assert.deepEqual(result.conversation_ids, [CONVERSATION_A]);
  assert.equal(result.deleted_conversations, 1);
  assert.equal(result.deleted_messages, ACCOUNT_A_MESSAGES);

  assert.equal(countRows(db, 'messages', 'WHERE uid = ?', [ACCOUNT_A]), 0);
  assert.equal(countRows(db, 'message_edits', 'WHERE uid = ?', [ACCOUNT_A]), 0);
  assert.equal(countRows(db, 'conversations', 'WHERE uid = ? AND conversation_id = ?',
    [ACCOUNT_A, CONVERSATION_A]), 0);
  assert.deepEqual(scanForValue(db, UUID.sticker), []);
  assert.deepEqual(scanForValue(db, 'happy'), []);

  // The other account and its conversation are untouched.
  assert.equal(countRows(db, 'messages', 'WHERE uid = ?', [ACCOUNT_B]), 1);
});

test('conversation delete is atomic and rejects a stale count', async (t) => {
  const { fixture, store, raw } = await openStoreSet(t, { dated: true });
  const db = raw();

  const stale = await capture(() => store.deleteConversations(ACCOUNT_A, [
    { id: CONVERSATION_C, expected_message_count: 2 },
    { id: CONVERSATION_A, expected_message_count: 999 },
  ]));
  expectStatus(stale, 409, 'stale conversation batch');
  assert.equal(countRows(db, 'conversations', 'WHERE uid = ?', [ACCOUNT_A]), 2,
    'a failed conversation batch deleted a conversation');
  assert.equal(countRows(db, 'deletions'), 0);

  const ok = store.deleteConversations(ACCOUNT_A, [
    { id: CONVERSATION_C, expected_message_count: 2 },
    { id: CONVERSATION_A, expected_message_count: 14 },
  ]);
  assert.equal(ok.deleted_conversations, 2);
  assert.equal(ok.deleted_messages, 16);
  assert.deepEqual([...ok.conversation_ids].sort(), [CONVERSATION_A, CONVERSATION_C].sort());
  assert.equal(countRows(db, 'messages', 'WHERE uid = ?', [ACCOUNT_A]), 0);
  assert.equal(countRows(db, 'conversations', 'WHERE uid = ?', [ACCOUNT_A]), 0);
});

test('deletions survive reopen and a forced reindex', async (t) => {
  const { fixture, store, open, closeCurrent, raw } = await openStoreSet(t);
  const db = raw();

  store.deleteMessage(ACCOUNT_A, UUID.text, 0);
  assert.equal(countRows(db, 'deletions'), 1);
  closeCurrent();
  db.close();

  const reopened = await open();
  const reopenedDb = raw();
  assert.equal(countRows(reopenedDb, 'messages', 'WHERE msg_uuid = ?', [UUID.text]), 0,
    'a restart resurrected the deleted message');

  await reopened.initialize({ force: true });
  assert.equal(countRows(reopenedDb, 'messages', 'WHERE msg_uuid = ?', [UUID.text]), 0,
    'a forced reindex resurrected the deleted message');
  assert.ok(reopened.listMessages(ACCOUNT_A, { limit: 500 }).total === ACCOUNT_A_MESSAGES - 1);
});

test('a changed source import still skips the deleted message', async (t) => {
  const { fixture, store, raw } = await openStoreSet(t);

  store.deleteMessage(ACCOUNT_A, UUID.text, 0);

  // Change an unrelated row so the fingerprint differs and a reimport runs.
  const file = path.join(fixture.dir, ACCOUNT_A, 'messages.jsonl');
  const before = fs.readFileSync(file, 'utf8');
  // replaceAll: the text appears in `content` and in `_export.text`.
  const after = before.replaceAll('unsafe integer row', 'unsafe integer row changed');
  assert.notEqual(after, before, 'the fixture edit must change the JSONL source');
  assert.notEqual(after, before, 'the source edit did not apply');
  fs.writeFileSync(file, after);

  const report = await store.initialize();
  assert.equal(report.imported, true, 'a changed source should reimport');
  assert.equal(store.listMessages(ACCOUNT_A, { limit: 500 }).total, ACCOUNT_A_MESSAGES - 1);
  await assert.rejects(() => Promise.resolve().then(() => store.getMessage(ACCOUNT_A, UUID.text)),
    (error) => error.status === 404);
  assert.equal(store.getMessage(ACCOUNT_A, UUID.huge).text_original, 'unsafe integer row changed');
});

test('an additive index-only migration keeps deletions and edits', async (t) => {
  const { fixture, store, open, closeCurrent, raw } = await openStoreSet(t);

  store.updateMessage(ACCOUNT_A, UUID.formula, { note: 'migration note', expected_revision: 0 });
  store.deleteMessage(ACCOUNT_A, UUID.text, 0);
  closeCurrent();

  // Simulate an older on-disk format whose index tables are rebuilt additively.
  const db = raw();
  db.exec("UPDATE store_meta SET value = '1' WHERE key = 'format_version'");
  db.prepare('DELETE FROM store_meta WHERE key = ?').run('fingerprint');
  assert.equal(countRows(db, 'deletions'), 1);
  db.close();

  const reopened = await open();
  const reopenedDb = raw();
  assert.equal(countRows(reopenedDb, 'deletions'), 1, 'the migration dropped the deletions table');
  assert.equal(countRows(reopenedDb, 'messages', 'WHERE msg_uuid = ?', [UUID.text]), 0,
    'the migration resurrected a deleted message');
  assert.equal(reopened.getMessage(ACCOUNT_A, UUID.formula).edit.note, 'migration note');
});

test('deletions hold when only reader.sqlite is available', async (t) => {
  const { fixture, store, open, closeCurrent, raw } = await openStoreSet(t);

  store.deleteMessage(ACCOUNT_A, UUID.text, 0);
  closeCurrent();

  // Prune every importable source: the index must keep serving what it holds.
  for (const uid of [ACCOUNT_A, ACCOUNT_B]) {
    for (const name of ['messages.jsonl', 'statistics.json', 'contacts.json']) {
      fs.rmSync(path.join(fixture.dir, uid, name), { force: true });
    }
  }

  const reopened = await open();
  assert.equal(reopened.listMessages(ACCOUNT_A, { limit: 500 }).total, ACCOUNT_A_MESSAGES - 1);
  await assert.rejects(() => Promise.resolve().then(() => reopened.getMessage(ACCOUNT_A, UUID.text)),
    (error) => error.status === 404);
});

test('deleting every message does not trigger a needless reimport on restart', async (t) => {
  const { fixture, store, open, closeCurrent, raw } = await openStoreSet(t);

  store.deleteMessages(ACCOUNT_A, store.listMessages(ACCOUNT_A, { limit: 500 }).items
    .map((item) => ({ msg_uuid: item.msg_uuid, expected_revision: item.edit.revision })));
  store.deleteMessage(ACCOUNT_B, UUID.conversationB, 0);
  closeCurrent();

  const reopened = await open();
  const report = await reopened.initialize();
  assert.equal(report.imported, false, 'an unchanged, empty index was reimported');
  assert.equal(reopened.listMessages(ACCOUNT_A, { limit: 500 }).total, 0);
  assert.equal(reopened.listMessages(ACCOUNT_B, { limit: 500 }).total, 0);

  const db = raw();
  for (const uuid of [UUID.text, UUID.formula, UUID.huge, UUID.conversationB]) {
    assert.equal(countRows(db, 'messages', 'WHERE msg_uuid = ?', [uuid]), 0);
  }
});

test('deletion never modifies original export files or shared media', async (t) => {
  const { fixture, store, raw } = await openStoreSet(t);
  const before = hashExport(fixture.dir);
  const filesBefore = listExportFiles(fixture.dir).length;

  store.updateMessage(ACCOUNT_A, UUID.text, { note: 'n', expected_revision: 0 });
  store.deleteMessage(ACCOUNT_A, UUID.text, 1);
  store.deleteConversation(ACCOUNT_A, CONVERSATION_A, ACCOUNT_A_MESSAGES - 1);

  assert.equal(hashExport(fixture.dir), before,
    'permanent deletion changed an original export file');
  assert.equal(listExportFiles(fixture.dir).length, filesBefore);
  assert.ok(fs.existsSync(path.join(fixture.dir, 'attachments', 'voice', 'v1.m4a')));
});