/**
 * Store contract tests (web/server/store.mjs).
 *
 * The store's read/write methods are synchronous, so every assertion goes
 * through `capture()`, which tolerates either a synchronous throw or a
 * rejected promise. Each test builds its own synthetic export and store, and
 * teardown always closes the database before deleting the directory (Windows
 * keeps a handle open otherwise).
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { DatabaseSync } from 'node:sqlite';

import {
  ACCOUNT_A, ACCOUNT_B, CONVERSATION_A, UUID, BIG_SERVER_ID, BIG_INDEX,
  makeExport, hashExport, copyExport,
} from './helpers/fixture.mjs';

let module;
try {
  module = await import('../server/store.mjs');
} catch (error) {
  throw new Error(`web/server/store.mjs could not be imported: ${error.code || error.message}`);
}
if (typeof module.ChatStore !== 'function') {
  throw new Error('web/server/store.mjs must export ChatStore');
}

const ACCOUNT_A_MESSAGES = 7;

/** Run a possibly-sync call and capture either its value or its error. */
async function capture(fn) {
  try {
    return { ok: true, value: await fn() };
  } catch (error) {
    return { ok: false, error };
  }
}

function expectStatus(outcome, status, label) {
  assert.equal(outcome.ok, false, `${label}: expected a failure, got a value`);
  const error = outcome.error;
  assert.equal(error.name, 'StoreError', `${label}: expected StoreError, got ${error.name}`);
  assert.equal(error.status, status, `${label}: expected status ${status}, got ${error.status}`);
  assert.equal(typeof error.code, 'string', `${label}: error.code must be a string`);
  assert.ok(!/SELECT|INSERT|UPDATE |DROP |sqlite/i.test(error.message),
    `${label}: the error message leaked SQL internals`);
}

/** Open a fresh store and make sure it closes before the directory is removed. */
async function open(t, options) {
  const fixture = makeExport();
  const store = new module.ChatStore(fixture.dir, options);
  t.after(async () => {
    try {
      store.close();
    } catch {
      /* ignore */
    }
    fixture.cleanup();
  });
  await store.initialize();
  return { fixture, store };
}

test('imports the synthetic export with account and conversation counts', async (t) => {
  const { fixture, store } = await open(t);
  const accounts = store.listAccounts();
  assert.equal(accounts.length, fixture.accounts.length);

  const byUid = new Map(accounts.map((account) => [String(account.uid), account]));
  for (const uid of fixture.accounts) {
    assert.ok(byUid.has(uid), `missing account ${uid}`);
    const account = byUid.get(uid);
    assert.equal(typeof account.uid, 'string');
    assert.equal(typeof account.name, 'string');
    assert.equal(typeof account.message_count, 'number');
  }
  assert.equal(byUid.get(ACCOUNT_A).message_count, ACCOUNT_A_MESSAGES);
  assert.equal(byUid.get(ACCOUNT_B).message_count, 1);
  assert.equal(byUid.get(ACCOUNT_A).conversation_count, 1);

  const conversations = store.listConversations(ACCOUNT_A);
  assert.equal(conversations.total, 1);
  assert.equal(conversations.items.length, 1);
  assert.equal(String(conversations.items[0].id), CONVERSATION_A);
  assert.equal(conversations.items[0].message_count, ACCOUNT_A_MESSAGES);
});

test('account sources prefer managed identity and explicit legacy metadata without guessing UID shape', async t => {
  const { fixture, store } = await open(t);
  const sources = () => Object.fromEntries(store.listAccounts().map(account => [account.uid, account.source_app]));
  const before = hashExport(fixture.dir);
  assert.deepEqual(sources(), { [ACCOUNT_A]: 'duoshan', [ACCOUNT_B]: 'duoshan' });
  assert.deepEqual(hashExport(fixture.dir), before, 'listing sources never changes exported files');

  fs.writeFileSync(path.join(fixture.dir, 'export_summary.json'), JSON.stringify({ app: 'Duoshan + Xiaohongshu' }));
  fs.writeFileSync(path.join(fixture.dir, ACCOUNT_A, 'statistics.json'), JSON.stringify({ app: 'WeChat' }));
  assert.deepEqual(sources(), { [ACCOUNT_A]: 'wechat', [ACCOUNT_B]: '' }, 'mixed summary and numeric UID cannot determine a source');

  const legacyDb = new DatabaseSync(store.databasePath);
  let rawBefore;
  try {
    legacyDb.prepare("UPDATE messages SET raw_json=json_set(raw_json,'$._export.app','Xiaohongshu') WHERE uid=?")
      .run(ACCOUNT_B);
    rawBefore = legacyDb.prepare('SELECT uid,msg_uuid,raw_json FROM messages ORDER BY uid,msg_uuid').all();
  } finally { legacyDb.close(); }
  assert.deepEqual(sources(), { [ACCOUNT_A]: 'wechat', [ACCOUNT_B]: 'xhs' },
    'an unprefixed mixed legacy account can identify its source from stored export metadata');

  fs.renameSync(path.join(fixture.dir, ACCOUNT_A), path.join(fixture.dir, 'xhs_' + ACCOUNT_A));
  assert.equal(sources()[ACCOUNT_A], 'xhs', 'explicit prefix works for legacy exports');
  const db = new DatabaseSync(store.databasePath);
  try {
    db.prepare('INSERT INTO library_accounts(uid,app,directory) VALUES(?,?,?)')
      .run(ACCOUNT_A, 'wechat', 'xhs_' + ACCOUNT_A);
    db.prepare('INSERT INTO library_accounts(uid,app,directory) VALUES(?,?,?)')
      .run(ACCOUNT_B, 'future-app', ACCOUNT_B);
  } finally { db.close(); }
  fs.writeFileSync(path.join(fixture.dir, 'export_summary.json'), JSON.stringify({ app: 'Duoshan' }));
  assert.deepEqual(sources(), { [ACCOUNT_A]: 'wechat', [ACCOUNT_B]: '' }, 'registry overrides folder and summary, including unknown future apps');
  const verifyDb = new DatabaseSync(store.databasePath);
  try {
    assert.deepEqual(verifyDb.prepare('SELECT uid,msg_uuid,raw_json FROM messages ORDER BY uid,msg_uuid').all(), rawBefore,
      'source fallback never rewrites stored original messages');
  } finally { verifyDb.close(); }
});

test('raw_json preserves integers beyond 2^53 exactly', async (t) => {
  const { store } = await open(t);
  const message = store.getMessage(ACCOUNT_A, UUID.huge);

  assert.equal(typeof message.raw_json, 'string', 'raw_json must stay a string');
  assert.ok(message.raw_json.includes(`"msg_server_id":${BIG_SERVER_ID}`),
    'raw_json lost the exact 2^53+1 literal');
  assert.ok(message.raw_json.includes(`"index_in_conversation":${BIG_INDEX}`),
    'raw_json lost the exact 19-digit index literal');

  const match = message.raw_json.match(/"msg_server_id":(\d+)/);
  assert.ok(match, 'msg_server_id not found in raw_json');
  assert.equal(match[1], BIG_SERVER_ID);
  assert.equal(BigInt(match[1]), BigInt(BIG_SERVER_ID));

  // Proof the fixture is genuinely unsafe: a naive parse rounds the value.
  const naive = JSON.parse(message.raw_json);
  assert.notEqual(String(naive.msg_server_id), BIG_SERVER_ID,
    'fixture is not exercising an unsafe integer');
  assert.equal(String(naive.msg_server_id), '9007199254740992');
});

test('exposed identifiers are strings and meta is an object', async (t) => {
  const { store } = await open(t);
  const message = store.getMessage(ACCOUNT_A, UUID.text);
  assert.equal(typeof message.account_uid, 'string');
  assert.equal(typeof message.msg_uuid, 'string');
  assert.equal(typeof message.conversation_id, 'string');
  assert.equal(message.msg_uuid, UUID.text);
  assert.equal(message.account_uid, ACCOUNT_A);
  assert.ok(message.meta && typeof message.meta === 'object');
  assert.equal(typeof message.text_original, 'string');
  assert.equal(typeof message.text, 'string');
  assert.equal(message.text_original, 'hello [smile] world');
  assert.equal(message.text, message.text_original);
  assert.equal(message.edit.revision, 0);
  assert.equal(message.edit.text_override, null);
});

test('lists messages with deterministic order and pagination', async (t) => {
  const { store } = await open(t);
  const first = store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, limit: 500 });
  assert.equal(first.total, ACCOUNT_A_MESSAGES);
  assert.equal(first.items.length, ACCOUNT_A_MESSAGES);

  const again = store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, limit: 500 });
  assert.deepEqual(again.items.map((item) => item.msg_uuid), first.items.map((item) => item.msg_uuid),
    'message order is not deterministic');

  const times = first.items.map((item) => Number(item.created_time));
  assert.ok(times.every((value, index) => index === 0 || times[index - 1] <= value),
    'messages are not in chronological order');

  const page = store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, limit: 2, offset: 2 });
  assert.equal(page.limit, 2);
  assert.equal(page.offset, 2);
  assert.equal(page.items.length, 2);
  assert.deepEqual(page.items.map((item) => item.msg_uuid),
    first.items.slice(2, 4).map((item) => item.msg_uuid));
});

test('bounds limit values instead of trusting the caller', async (t) => {
  const { store } = await open(t);
  for (const limit of [0, -1, 1, 2, 1000, 100000]) {
    const outcome = await capture(() => store.listMessages(ACCOUNT_A, {
      conversationId: CONVERSATION_A, limit,
    }));
    if (!outcome.ok) {
      expectStatus(outcome, 400, `limit ${limit}`);
      continue;
    }
    assert.ok(outcome.value.limit >= 1 && outcome.value.limit <= 500,
      `limit ${limit} was not clamped into 1..500 (got ${outcome.value.limit})`);
    assert.ok(outcome.value.items.length <= 500);
  }
});

test('searches effective text and note, including edited text', async (t) => {
  const { store } = await open(t);
  const byOriginal = store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, q: 'unsafe integer' });
  assert.equal(byOriginal.total, 1);
  assert.equal(byOriginal.items[0].msg_uuid, UUID.huge);

  const current = store.getMessage(ACCOUNT_A, UUID.text);
  store.updateMessage(ACCOUNT_A, UUID.text, {
    text_override: 'zzz-search-target', expected_revision: current.edit.revision,
  });

  const byOverride = store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, q: 'zzz-search-target' });
  assert.equal(byOverride.total, 1, 'search does not use the effective text');
  assert.equal(byOverride.items[0].msg_uuid, UUID.text);

  store.updateMessage(ACCOUNT_A, UUID.formula, {
    note: 'note-marker-42', expected_revision: store.getMessage(ACCOUNT_A, UUID.formula).edit.revision,
  });
  const byNote = store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, q: 'note-marker-42' });
  assert.equal(byNote.total, 1, 'search does not use the note');
  assert.equal(byNote.items[0].msg_uuid, UUID.formula);
});

test('omits hidden messages by default and reveals them on request', async (t) => {
  const { store } = await open(t);
  const before = store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, limit: 500 });
  store.updateMessage(ACCOUNT_A, UUID.formula, {
    hidden: true, expected_revision: store.getMessage(ACCOUNT_A, UUID.formula).edit.revision,
  });

  const after = store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, limit: 500 });
  assert.equal(after.total, before.total - 1);
  assert.ok(!after.items.some((item) => item.msg_uuid === UUID.formula));

  const revealed = store.listMessages(ACCOUNT_A, {
    conversationId: CONVERSATION_A, limit: 500, includeHidden: true,
  });
  assert.equal(revealed.total, before.total);
  assert.ok(revealed.items.some((item) => item.msg_uuid === UUID.formula));

  const hidden = store.getMessage(ACCOUNT_A, UUID.formula);
  assert.equal(hidden.edit.hidden, true);
  assert.equal(hidden.text_original, '=SUM(A1:A2)\nsecond line');
});

test('applies text, note and hidden edits and bumps the revision', async (t) => {
  const { store } = await open(t);
  assert.equal(store.getMessage(ACCOUNT_A, UUID.text).edit.revision, 0,
    'a fresh message must start at revision 0');

  const edited = store.updateMessage(ACCOUNT_A, UUID.text, {
    text_override: 'edited text', note: 'a note', hidden: false, expected_revision: 0,
  });
  assert.equal(edited.text, 'edited text');
  assert.equal(edited.text_original, 'hello [smile] world', 'the original text must be kept');
  assert.equal(edited.edit.text_override, 'edited text');
  assert.equal(edited.edit.note, 'a note');
  assert.equal(edited.edit.hidden, false);
  assert.equal(edited.edit.revision, 1);
  assert.equal(typeof edited.edit.updated_at, 'string');
  assert.ok(!Number.isNaN(Date.parse(edited.edit.updated_at)));

  const restoredText = store.updateMessage(ACCOUNT_A, UUID.text, {
    text_override: null, expected_revision: 1,
  });
  assert.equal(restoredText.text, 'hello [smile] world');
  assert.equal(restoredText.edit.revision, 2);
});

test('rejects a stale revision with 409', async (t) => {
  const { store } = await open(t);
  store.updateMessage(ACCOUNT_A, UUID.text, { text_override: 'first', expected_revision: 0 });

  const stale = await capture(() => store.updateMessage(ACCOUNT_A, UUID.text, {
    text_override: 'stale', expected_revision: 0,
  }));
  expectStatus(stale, 409, 'stale update');

  const current = store.getMessage(ACCOUNT_A, UUID.text);
  assert.equal(current.text, 'first', 'a rejected update must not be applied');
  assert.equal(current.edit.revision, 1);

  const missingRevision = await capture(() => store.updateMessage(ACCOUNT_A, UUID.text, {
    text_override: 'no revision',
  }));
  expectStatus(missingRevision, 400, 'missing expected_revision');
});

test('restores the original and advances the revision (no ABA)', async (t) => {
  const { store } = await open(t);
  store.updateMessage(ACCOUNT_A, UUID.text, {
    text_override: 'edited', note: 'note', hidden: true, expected_revision: 0,
  });

  const restored = store.restoreMessage(ACCOUNT_A, UUID.text, 1);
  assert.equal(restored.edit.text_override, null);
  assert.equal(restored.edit.note, null);
  assert.equal(restored.edit.hidden, false);
  assert.equal(restored.edit.revision, 2, 'restore must advance the revision');
  assert.equal(restored.text, 'hello [smile] world');

  const replay = await capture(() => store.updateMessage(ACCOUNT_A, UUID.text, {
    text_override: 'again', expected_revision: 1,
  }));
  expectStatus(replay, 409, 'replayed old revision after restore');
});

test('persists edits across close and reopen', async (t) => {
  const fixture = makeExport();
  const first = new module.ChatStore(fixture.dir);
  await first.initialize();
  first.updateMessage(ACCOUNT_A, UUID.text, {
    text_override: 'persisted', note: 'kept', expected_revision: 0,
  });
  first.close();

  const second = new module.ChatStore(fixture.dir);
  t.after(() => {
    try {
      second.close();
    } catch {
      /* ignore */
    }
    fixture.cleanup();
  });
  await second.initialize();
  const message = second.getMessage(ACCOUNT_A, UUID.text);
  assert.equal(message.edit.text_override, 'persisted');
  assert.equal(message.edit.note, 'kept');
  assert.equal(message.edit.revision, 1);
});

test('reindex skips unchanged sources and preserves edits on rebuild', async (t) => {
  const { store } = await open(t);
  store.updateMessage(ACCOUNT_A, UUID.formula, {
    text_override: 'survives reindex', note: 'note survives', hidden: true, expected_revision: 0,
  });

  const unchanged = await store.initialize();
  assert.equal(unchanged.imported, false, 'an unchanged export must not be reimported');
  const afterUnchanged = store.getMessage(ACCOUNT_A, UUID.formula);
  assert.equal(afterUnchanged.edit.text_override, 'survives reindex');
  assert.equal(afterUnchanged.edit.revision, 1);

  const forced = await store.initialize({ force: true });
  assert.equal(forced.imported, true, 'a forced initialize must reimport');
  assert.equal(forced.messages, ACCOUNT_A_MESSAGES + 1);
  const afterForce = store.getMessage(ACCOUNT_A, UUID.formula);
  assert.equal(afterForce.edit.text_override, 'survives reindex');
  assert.equal(afterForce.edit.note, 'note survives');
  assert.equal(afterForce.edit.hidden, true);
  assert.equal(afterForce.edit.revision, 1);
});

test('never writes to the original export files', async (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());
  const before = hashExport(fixture.dir);

  const store = new module.ChatStore(fixture.dir);
  await store.initialize();
  store.updateMessage(ACCOUNT_A, UUID.text, { text_override: 'x', expected_revision: 0 });
  store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, q: 'x' });
  await store.initialize({ force: true });
  store.close();

  assert.equal(hashExport(fixture.dir), before,
    'the store modified an original export file (only reader.sqlite may be created)');
});

test('rejects unknown identifiers and SQL-looking input without side effects', async (t) => {
  const { store } = await open(t);

  const missing = await capture(() => store.getMessage(ACCOUNT_A, 'no-such-uuid'));
  expectStatus(missing, 404, 'unknown message');

  // A hostile search string must be treated as data, not SQL.
  const injection = "' OR 1=1; DROP TABLE msg; DELETE FROM message_edits; --";
  const injected = store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, q: injection });
  assert.equal(injected.total, 0, 'an injection string matched rows');
  assert.equal(store.listConversations(ACCOUNT_A, { q: injection }).total, 0);

  // The store still works, so nothing was dropped.
  assert.equal(store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, limit: 500 }).total,
    ACCOUNT_A_MESSAGES);
  assert.equal(store.listAccounts().length, 2);

  // Malformed accounts must not fall back to "all accounts".
  for (const bad of ['', 'not-a-uid', "1' OR '1'='1", '../etc', '1;DROP TABLE messages']) {
    const outcome = await capture(() => store.listConversations(bad));
    if (outcome.ok) {
      assert.equal(outcome.value.items.length, 0, `account ${JSON.stringify(bad)} leaked rows`);
    } else {
      expectStatus(outcome, 400, `account ${JSON.stringify(bad)}`);
    }
  }

  const badPatch = await capture(() => store.updateMessage(ACCOUNT_A, UUID.text, {
    text_override: 5, expected_revision: 0,
  }));
  expectStatus(badPatch, 400, 'non-string text_override');
});

test('serves already-imported data when the source JSONL is removed', async (t) => {
  const fixture = makeExport();
  const first = new module.ChatStore(fixture.dir);
  await first.initialize();
  const expected = first.getMessage(ACCOUNT_A, UUID.text);
  first.close();

  fs.rmSync(path.join(fixture.dir, ACCOUNT_A, 'messages.jsonl'));

  const second = new module.ChatStore(fixture.dir);
  t.after(() => {
    try {
      second.close();
    } catch {
      /* ignore */
    }
    fixture.cleanup();
  });
  const report = await second.initialize();
  const message = second.getMessage(ACCOUNT_A, UUID.text);
  assert.equal(message.msg_uuid, expected.msg_uuid);
  assert.equal(message.text_original, expected.text_original);
  assert.equal(second.getMessage(ACCOUNT_B, UUID.conversationB).msg_uuid, UUID.conversationB);
  assert.equal(typeof report.imported, 'boolean');
});

test('an exported copy keeps serving the same messages', async (t) => {
  const fixture = makeExport();
  const moved = copyExport(fixture.dir, `${fixture.dir}-uploaded`);
  const store = new module.ChatStore(moved);
  t.after(() => {
    try {
      store.close();
    } catch {
      /* ignore */
    }
    fs.rmSync(moved, { recursive: true, force: true });
    fixture.cleanup();
  });
  await store.initialize();

  const original = store.listMessages(ACCOUNT_A, { conversationId: CONVERSATION_A, limit: 500 });
  assert.equal(original.total, ACCOUNT_A_MESSAGES);
  assert.equal(store.listAccounts().length, 2);
  assert.equal(store.getMessage(ACCOUNT_A, UUID.huge).raw_json.includes(BIG_SERVER_ID), true);
});
