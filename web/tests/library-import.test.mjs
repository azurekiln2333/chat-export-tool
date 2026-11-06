import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { DatabaseSync } from 'node:sqlite';
import { ChatStore } from '../server/store.mjs';
import { importLibrary, organizeLibrary } from '../server/library-import.mjs';

const WX = '11112222333344445555666677778888';
const XHS = '111122223333444455556666';
const DY = '8111222233334444';
const epoch = Date.UTC(2026, 0, 2, 0, 0);

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'chat-library-test-'));
  // These independent synthetic fixtures are outside the analysis workspace.
  assert.ok(!path.resolve(root).toLowerCase().startsWith('e:\\analysis\\'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

function message(uid, id, text, own = false, extra = {}) {
  return { msg_uuid: id, sender: own ? uid : 'peer', conversation_id: 'peer@' + uid,
    created_time: epoch, type: 1,
    _export: { text, time: '2026-01-02 08:00:00', conversation_name: 'Conversation',
      sender_name: own ? 'Self' : 'Peer', local_media: [], ...extra } };
}

function source(root, name, uid, app, messages) {
  const directory = path.join(root, name);
  fs.mkdirSync(path.join(directory, uid), { recursive: true });
  fs.writeFileSync(path.join(directory, uid, 'messages.jsonl'), messages.map(row => typeof row === 'string' ? row : JSON.stringify(row)).join('\n') + '\n');
  fs.writeFileSync(path.join(directory, uid, 'statistics.json'), JSON.stringify({ app, nickname: 'Synthetic ' + app }));
  fs.writeFileSync(path.join(directory, 'export_summary.json'), JSON.stringify({ app, timezone: 'Asia/Shanghai' }));
  return directory;
}

function query(root, sql, ...params) {
  const db = new DatabaseSync(path.join(root, 'reader.sqlite'));
  try { return db.prepare(sql).all(...params); } finally { db.close(); }
}

test('independent app imports merge, repeated import is idempotent, and prefixed folders reopen', async t => {
  const root = fixture(t), output = path.join(root, 'library');
  const first = source(root, 'wx', WX, 'wechat', [message(WX, 'wx-one', 'First', true)]);
  const second = source(root, 'dy', DY, 'duoshan', [message(DY, 'dy-one', 'Duoshan')]);
  const third = source(root, 'xhs', XHS, 'xhs', [{ uuid: 'xhs-one', sender_id: XHS,
    local_chat_user_id: 'peer@' + XHS, create_time: epoch, content_type: 1,
    _export: { text: 'XHS', conversation_name: 'XHS peer', time: '2026-01-02 08:00:00' } }]);
  assert.equal((await importLibrary({ sourceDir: first, outputDir: output, app: 'wechat' })).inserted, 1);
  const wxBefore = query(output, 'SELECT * FROM messages WHERE uid=?', WX);
  await importLibrary({ sourceDir: second, outputDir: output, app: 'duoshan' });
  await importLibrary({ sourceDir: third, outputDir: output, app: 'xhs' });
  assert.deepEqual(query(output, 'SELECT * FROM messages WHERE uid=?', WX), wxBefore);
  const repeated = await importLibrary({ sourceDir: first, outputDir: output, app: 'wechat' });
  assert.equal(repeated.repeated, true);
  assert.equal(repeated.inserted, 0);
  for (const name of ['wx_' + WX, 'dy_' + DY, 'xhs_' + XHS]) assert.ok(fs.existsSync(path.join(output, name, 'messages.jsonl')));
  const store = new ChatStore(output);
  try {
    assert.equal((await store.initialize({ force: true })).imported, false);
    assert.deepEqual(Object.fromEntries(store.listAccounts().map(account => [account.uid, account.source_app])),
      { [WX]: 'wechat', [DY]: 'duoshan', [XHS]: 'xhs' });
    assert.equal(store.getMessage(WX, 'wx-one').sender, WX);
    assert.equal(store.getCalendar(WX, { month: '2026-01' }).total, 1);
  } finally { store.close(); }
});

test('new snapshot appends, retains absent history and annotations, unions media, suppresses deletions', async t => {
  const root = fixture(t), output = path.join(root, 'library');
  fs.mkdirSync(path.join(output, 'attachments', 'wechat'), { recursive: true });
  for (const name of ['old.bin', 'new.bin']) fs.writeFileSync(path.join(output, 'attachments/wechat', name), name);
  const first = source(root, 'first', WX, 'wechat', [
    message(WX, 'old', 'old preserved'), message(WX, 'edit', 'original', true, { local_media: ['attachments/wechat/old.bin'] }),
    message(WX, 'deleted', 'will be deleted'),
  ]);
  await importLibrary({ sourceDir: first, outputDir: output, app: 'wechat' });
  const store = new ChatStore(output);
  try {
    await store.initialize();
    store.updateMessage(WX, 'edit', { expected_revision: 0, text_override: 'My edited text', note: 'My note', hidden: true });
    store.deleteMessage(WX, 'deleted', 0);
  } finally { store.close(); }
  const editsBefore = query(output, 'SELECT * FROM message_edits');
  const deletionsBefore = query(output, 'SELECT * FROM deletions');
  const next = source(root, 'next', WX, 'wechat', [
    message(WX, 'edit', 'new source text', true, { local_media: ['attachments/wechat/new.bin'] }),
    message(WX, 'deleted', 'will be deleted'), message(WX, 'new', 'New message'),
  ]);
  const result = await importLibrary({ sourceDir: next, outputDir: output, app: 'wechat' });
  assert.equal(result.inserted, 1);
  assert.equal(result.updated, 1);
  assert.equal(result.suppressed, 1);
  assert.equal(query(output, 'SELECT COUNT(*) AS n FROM messages')[0].n, 3);
  assert.deepEqual(query(output, 'SELECT * FROM message_edits'), editsBefore);
  assert.deepEqual(query(output, 'SELECT * FROM deletions'), deletionsBefore);
  assert.deepEqual(JSON.parse(query(output, 'SELECT meta_json FROM messages WHERE msg_uuid=?', 'edit')[0].meta_json).local_media,
    ['attachments/wechat/old.bin', 'attachments/wechat/new.bin']);
  const again = new ChatStore(output);
  try { await again.initialize(); assert.equal(again.getMessage(WX, 'edit').text, 'My edited text'); }
  finally { again.close(); }
});

test('legacy WeChat aliases preserve message edits and conversation identity for future messages', async t => {
  const root = fixture(t), output = path.join(root, 'library');
  const old = source(root, 'old', WX, 'wechat', [message(WX, 'wx-123-456', 'Legacy', true)]);
  await importLibrary({ sourceDir: old, outputDir: output, app: 'wechat' });
  const db = new DatabaseSync(path.join(output, 'reader.sqlite'));
  try {
    db.prepare('INSERT INTO import_message_aliases VALUES(?,?,?,?)').run(WX, 'server:10:peer', 'wx-123-456', 'peer@' + WX);
    db.prepare('INSERT INTO import_conversation_aliases VALUES(?,?,?)').run(WX, 'wechat:peer', 'peer@' + WX);
  } finally { db.close(); }
  const next = source(root, 'new', WX, 'wechat', [
    { ...message(WX, 'wx-stable-new-id', 'Legacy updated', true, { source_key: 'server:10:peer', source_conversation: 'wechat:peer' }), conversation_id: 'stable-peer' },
    { ...message(WX, 'wx-stable-second', 'Later', false, { source_key: 'server:11:peer', source_conversation: 'wechat:peer' }), conversation_id: 'stable-peer' },
  ]);
  const result = await importLibrary({ sourceDir: next, outputDir: output, app: 'wechat' });
  assert.equal(result.inserted, 1); assert.equal(result.updated, 1);
  assert.deepEqual(query(output, 'SELECT DISTINCT conversation_id FROM messages'), [{ conversation_id: 'peer@' + WX }].map(x => Object.assign(Object.create(null), x)));
  assert.equal(query(output, 'SELECT msg_uuid FROM messages WHERE text_original=?', 'Legacy updated')[0].msg_uuid, 'wx-123-456');
});

test('invalid source rolls back all message/account/history mutations', async t => {
  const root = fixture(t), output = path.join(root, 'library');
  const good = source(root, 'good', DY, 'duoshan', [message(DY, 'dy', 'safe')]);
  await importLibrary({ sourceDir: good, outputDir: output, app: 'duoshan' });
  const before = query(output, 'SELECT * FROM messages');
  const bad = source(root, 'bad', WX, 'wechat', [message(WX, 'first', 'must rollback'), '{broken json']);
  await assert.rejects(importLibrary({ sourceDir: bad, outputDir: output, app: 'wechat' }));
  assert.deepEqual(query(output, 'SELECT * FROM messages'), before);
  assert.equal(query(output, 'SELECT * FROM accounts').length, 1);
  assert.equal(query(output, 'SELECT * FROM import_batches').length, 1);
});

test('organization migrates only folder labels and preserves existing raw rows and edits', async t => {
  const root = fixture(t);
  const output = source(root, 'legacy', WX, 'WeChat', [message(WX, 'wx-legacy', 'Legacy', true)]);
  const store = new ChatStore(output);
  try { await store.initialize(); store.updateMessage(WX, 'wx-legacy', { expected_revision: 0, note: 'Keep this' }); }
  finally { store.close(); }
  const messages = query(output, 'SELECT * FROM messages'), edits = query(output, 'SELECT * FROM message_edits');
  const result = await organizeLibrary({ outputDir: output });
  assert.equal(result.organized, 1);
  assert.ok(fs.existsSync(path.join(output, 'wx_' + WX, 'messages.jsonl')));
  assert.deepEqual(query(output, 'SELECT * FROM messages'), messages);
  assert.deepEqual(query(output, 'SELECT * FROM message_edits'), edits);
  const reopened = new ChatStore(output);
  try { assert.equal((await reopened.initialize()).imported, false); assert.equal(reopened.getMessage(WX, 'wx-legacy').edit.note, 'Keep this'); }
  finally { reopened.close(); }
});

test('unsafe integer raw IDs survive and failed projection is retryable from committed library', async t => {
  const root = fixture(t), output = path.join(root, 'library');
  const raw = JSON.stringify(message(DY, 'precision', 'Precise')).replace('"type":1', '"type":1,"msg_server_id":9007199254740993123');
  const input = source(root, 'incoming', DY, 'duoshan', [raw]);
  await importLibrary({ sourceDir: input, outputDir: output, app: 'duoshan', project: false });
  assert.equal(query(output, 'SELECT raw_json FROM messages')[0].raw_json, raw);
  const result = await importLibrary({ sourceDir: input, outputDir: output, app: 'duoshan' });
  assert.equal(result.repeated, true);
  assert.equal(fs.readFileSync(path.join(output, 'dy_' + DY, 'messages.jsonl'), 'utf8').trim(), raw);
});

test('adoption keeps existing message columns and name while adding aliases and missing messages', async t => {
  const root = fixture(t), output = path.join(root, 'library');
  const first = source(root, 'first', WX, 'wechat', [message(WX, 'old-id', 'Old text', true)]);
  await importLibrary({ sourceDir: first, outputDir: output, app: 'wechat' });
  const db = new DatabaseSync(path.join(output, 'reader.sqlite'));
  try { db.prepare('INSERT INTO import_message_aliases VALUES(?,?,?,?)').run(WX, 'stable-key', 'old-id', 'peer@' + WX); }
  finally { db.close(); }
  const before = query(output, 'SELECT * FROM messages WHERE msg_uuid=?', 'old-id');
  const names = query(output, 'SELECT * FROM accounts');
  const incoming = source(root, 'later', WX, 'wechat', [
    message(WX, 'new-id-for-old', 'Changed source text', false, { source_key: 'stable-key' }),
    message(WX, 'truly-new', 'Missing old account history'),
  ]);
  fs.writeFileSync(path.join(incoming, WX, 'statistics.json'), JSON.stringify({ nickname: 'Changed account name' }));
  const result = await importLibrary({ sourceDir: incoming, outputDir: output, app: 'wechat', preserveExisting: true });
  assert.equal(result.unchanged, 1); assert.equal(result.inserted, 1);
  assert.deepEqual(query(output, 'SELECT * FROM messages WHERE msg_uuid=?', 'old-id'), before);
  assert.deepEqual(query(output, 'SELECT * FROM accounts'), names);
});

test('projected JSONL can reconstruct canonical aliases and accumulated media without the managed database', async t => {
  const root = fixture(t), output = path.join(root, 'library');
  fs.mkdirSync(path.join(output, 'attachments/wechat'), { recursive: true });
  for (const name of ['first.bin', 'second.bin']) fs.writeFileSync(path.join(output, 'attachments/wechat', name), name);
  const first = source(root, 'first', WX, 'wechat', [message(WX, 'legacy-id', 'Legacy', true,
    { source_key: 'stable', local_media: ['attachments/wechat/first.bin'] })]);
  await importLibrary({ sourceDir: first, outputDir: output, app: 'wechat' });
  const second = source(root, 'second', WX, 'wechat', [message(WX, 'new-source-id', 'Updated', true,
    { source_key: 'stable', local_media: ['attachments/wechat/second.bin'] })]);
  await importLibrary({ sourceDir: second, outputDir: output, app: 'wechat' });
  const projection = fs.readFileSync(path.join(output, 'wx_' + WX, 'messages.jsonl'), 'utf8');
  assert.ok(projection.includes('_library_projection'));
  const standalone = path.join(root, 'standalone');
  fs.mkdirSync(path.join(standalone, 'wx_' + WX), { recursive: true });
  fs.writeFileSync(path.join(standalone, 'wx_' + WX, 'messages.jsonl'), projection);
  const store = new ChatStore(standalone);
  try {
    await store.initialize();
    const restored = store.getMessage(WX, 'legacy-id');
    assert.equal(restored.sender, WX);
    assert.deepEqual(restored.meta.local_media, ['attachments/wechat/first.bin', 'attachments/wechat/second.bin']);
  } finally { store.close(); }
});
