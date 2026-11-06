import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { importLibrary } from '../server/library-import.mjs';
import { patchLibraryMedia, resumeLibraryMedia } from '../server/library-media.mjs';
import { ChatStore, deletionHash } from '../server/store.mjs';
import { Client, startTestApp } from './helpers/server.mjs';
import { describeMessage } from '../src/message-model.js';

const WX = '11112222333344445555666677778888';
const DY = '8111222233334444';
const XHS = '111122223333444455556666';
const sha = value => createHash('sha256').update(value).digest('hex');

function fixture(t, beforeCleanup = () => {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'chat-library-media-test-'));
  assert.ok(!path.resolve(root).toLowerCase().startsWith('e:\\analysis\\'));
  t.after(async () => {
    await beforeCleanup();
    fs.rmSync(root, { recursive: true, force: true });
  });
  return { root, output: path.join(root, 'library') };
}

function query(output, sql, ...params) {
  const db = new DatabaseSync(path.join(output, 'reader.sqlite'));
  try { return db.prepare(sql).all(...params); } finally { db.close(); }
}

function exec(output, sql, ...params) {
  const db = new DatabaseSync(path.join(output, 'reader.sqlite'));
  try { return db.prepare(sql).run(...params); } finally { db.close(); }
}

async function seed(root, output, uid = WX, app = 'wechat', ids = ['shared-id']) {
  const source = path.join(root, 'source-' + uid);
  fs.mkdirSync(path.join(source, uid), { recursive: true });
  const lines = ids.map(id => ` {"msg_uuid":${JSON.stringify(id)},"sender":"${uid}","conversation_id":"peer@${uid}",`
    + '"created_time":1767312000000,"type":1,"server_id":9007199254740993123,'
    + '"_export":{"text":"Exact original","time":"2026-01-02 08:00:00","local_media":[],"bubble":{"side":"1"}}} ');
  fs.writeFileSync(path.join(source, uid, 'messages.jsonl'), lines.join('\n') + '\n');
  fs.writeFileSync(path.join(source, uid, 'statistics.json'), JSON.stringify({ app }));
  await importLibrary({ sourceDir: source, outputDir: output, app });
}

function asset(output, name = 'download.bin', bytes = 'verified media') {
  const relative = 'attachments/library/' + name;
  fs.mkdirSync(path.dirname(path.join(output, relative)), { recursive: true });
  fs.writeFileSync(path.join(output, relative), bytes);
  return { kind: 'file', status: 'ok', path: relative, bytes: Buffer.byteLength(bytes), sha256: sha(bytes) };
}

function updateFile(root, name, rows) {
  const file = path.join(root, name + '.jsonl');
  fs.writeFileSync(file, rows.map(row => typeof row === 'string' ? row : JSON.stringify(row)).join('\n') + '\n');
  return file;
}

function messageRow(output, uid = WX, id = 'shared-id') {
  return query(output, 'SELECT * FROM messages WHERE uid=? AND msg_uuid=?', uid, id)[0];
}

test('media-only transaction preserves every other column, raw bytes, unrelated metadata, accounts and annotations', async t => {
  const { root, output } = fixture(t);
  for (const [uid, app] of [[WX, 'wechat'], [DY, 'duoshan'], [XHS, 'xhs']]) await seed(root, output, uid, app);
  const store = new ChatStore(output);
  try {
    await store.initialize();
    store.updateMessage(WX, 'shared-id', { expected_revision: 0, text_override: 'User edit', note: 'Personal note', hidden: true });
  } finally { store.close(); }
  exec(output, 'INSERT INTO deletions VALUES(?,?,?)', 'message', deletionHash('message', WX, 'previously-deleted'), '2026-01-01');
  exec(output, 'UPDATE messages SET meta_json=? WHERE uid=?',
    '{"text":"Exact original","local_media":[],"bubble":{"side":"1"},"precise":9007199254740993123,"unknown":{"keep":true},"media":{"format":"OLD","details":{"retained":true}}}', WX);
  const before = query(output, 'SELECT * FROM messages ORDER BY uid,msg_uuid');
  const protectedTables = Object.fromEntries(['accounts', 'conversations', 'message_edits', 'deletions', 'store_meta',
    'library_accounts', 'import_batches', 'import_message_aliases', 'import_conversation_aliases']
    .map(table => [table, query(output, `SELECT * FROM ${table}`)]));
  const media = asset(output);
  const raw = messageRow(output).raw_json;
  const updatesFile = updateFile(root, 'patch', [{ uid: WX, msg_uuid: 'shared-id', app: 'wechat', expected_raw_sha256: sha(raw),
    local_media: [media.path], media: { ...media, details: { downloaded: true } }, media_status: 'available' }]);
  const result = await patchLibraryMedia({ outputDir: output, updatesFile, sourceId: 'test-download' });
  assert.equal(result.patched_messages, 1);
  assert.equal(result.projection_status, 'completed');
  const after = query(output, 'SELECT * FROM messages ORDER BY uid,msg_uuid');
  for (let i = 0; i < before.length; i++) {
    assert.deepEqual({ ...after[i], meta_json: before[i].meta_json }, { ...before[i] });
    if (before[i].uid !== WX) assert.equal(after[i].meta_json, before[i].meta_json);
  }
  const metaText = messageRow(output).meta_json;
  assert.ok(metaText.includes('"precise":9007199254740993123'));
  const meta = JSON.parse(metaText);
  assert.deepEqual(meta.unknown, { keep: true });
  assert.deepEqual(meta.bubble, { side: '1' });
  assert.deepEqual(meta.media.details, { retained: true, downloaded: true });
  assert.equal(meta.media.format, 'OLD');
  assert.deepEqual(meta.local_media, [media.path]);
  for (const [table, rows] of Object.entries(protectedTables)) assert.deepEqual(query(output, `SELECT * FROM ${table}`), rows, table);
  const projected = fs.readFileSync(path.join(output, 'wx_' + WX, 'messages.jsonl'), 'utf8').trim();
  assert.ok(projected.includes('"server_id":9007199254740993123'));
  assert.equal(JSON.parse(projected)._library_projection.meta_json, metaText);
  assert.equal(messageRow(output).raw_json, raw);
});

test('missing, deleted, stale and wrong-app rows are skipped without resurrecting old JSONL', async t => {
  const { root, output } = fixture(t);
  await seed(root, output, WX, 'wechat', ['live', 'deleted', 'suppressed-conversation']);
  const store = new ChatStore(output);
  try { await store.initialize(); store.deleteMessage(WX, 'deleted', 0); } finally { store.close(); }
  exec(output, 'UPDATE messages SET conversation_id=? WHERE msg_uuid=?', 'deleted-conversation', 'suppressed-conversation');
  exec(output, 'INSERT INTO deletions VALUES(?,?,?)', 'conversation', deletionHash('conversation', WX, 'deleted-conversation'), '2026-01-01');
  const before = query(output, 'SELECT * FROM messages ORDER BY uid,msg_uuid');
  const deletedBefore = query(output, 'SELECT * FROM deletions');
  const media = asset(output);
  const updatesFile = updateFile(root, 'patch', [
    { uid: WX, msg_uuid: 'deleted', local_media: [media.path] },
    { uid: WX, msg_uuid: 'never-existed', local_media: [media.path] },
    { uid: WX, msg_uuid: 'suppressed-conversation', local_media: [media.path] },
    { uid: WX, msg_uuid: 'live', expected_raw_sha256: '0'.repeat(64), local_media: [media.path] },
    { uid: WX, msg_uuid: 'live', app: 'duoshan', local_media: [media.path] },
  ]);
  const result = await patchLibraryMedia({ outputDir: output, updatesFile });
  assert.equal(result.patched_messages, 0);
  assert.equal(result.skipped_messages, 4);
  assert.deepEqual(query(output, 'SELECT * FROM messages ORDER BY uid,msg_uuid'), before);
  assert.deepEqual(query(output, 'SELECT * FROM deletions'), deletedBefore);
  assert.equal(query(output, 'SELECT * FROM messages WHERE msg_uuid=?', 'deleted').length, 0);
});

test('repeated and reordered patch batches do not duplicate paths or replace successful resources with failures', async t => {
  const { root, output } = fixture(t);
  await seed(root, output);
  const old = asset(output, 'original.bin', 'original'), media = asset(output, 'playable.bin', 'playable');
  const updatesFile = updateFile(root, 'patch', [
    { uid: WX, msg_uuid: 'shared-id', original_media: [old.path], local_media: [old.path, media.path], media },
    { uid: WX, msg_uuid: 'shared-id', local_media: [media.path], media_status: 'available' },
  ]);
  const first = await patchLibraryMedia({ outputDir: output, updatesFile });
  assert.equal(first.patched_messages, 1);
  const before = messageRow(output), projection = fs.readFileSync(path.join(output, 'wx_' + WX, 'messages.jsonl'));
  const second = await patchLibraryMedia({ outputDir: output, updatesFile });
  assert.equal(second.repeated, true); assert.equal(second.patched_messages, 0);
  assert.equal(query(output, 'SELECT * FROM library_media_batches').length, 1);
  assert.deepEqual(messageRow(output), before);
  assert.deepEqual(fs.readFileSync(path.join(output, 'wx_' + WX, 'messages.jsonl')), projection);
  const failed = updateFile(root, 'later-failed', [{ uid: WX, msg_uuid: 'shared-id', local_media: [media.path, old.path],
    original_media: [old.path], media: { status: 'failed', kind: 'file', errors: ['offline'] }, media_status: 'failed' }]);
  const again = await patchLibraryMedia({ outputDir: output, updatesFile: failed });
  assert.equal(again.patched_messages, 0); assert.equal(again.unchanged_messages, 1);
  assert.deepEqual(messageRow(output), before);
});

test('photo/video resources retain a stable primary and a later run does not reproject an unchanged message', async t => {
  const { root, output } = fixture(t);
  await seed(root, output);
  const photo = { ...asset(output, 'photo.jpg', 'photo'), kind: 'image', format: 'JPEG', width: 800 };
  const video = { ...asset(output, 'video.mp4', 'video'), kind: 'video', format: 'MP4', duration: 3 };
  const rows = [photo, video].map(media => ({ uid: WX, msg_uuid: 'shared-id', media }));
  const first = await patchLibraryMedia({ outputDir: output, updatesFile: updateFile(root, 'multi-first', rows) });
  assert.equal(first.patched_messages, 1);
  const before = messageRow(output);
  const meta = JSON.parse(before.meta_json);
  assert.deepEqual(meta.media, photo);
  assert.deepEqual(meta.local_media, [video.path]);
  const projection = fs.readFileSync(path.join(output, 'wx_' + WX, 'messages.jsonl'));
  const historyEntries = fs.readdirSync(path.join(output, '.import-history'));
  // Different fingerprint ensures this exercises semantic idempotence, not only history lookup.
  const repeat = await patchLibraryMedia({ outputDir: output, updatesFile: updateFile(root, 'multi-repeat', rows.toReversed()) });
  assert.equal(repeat.repeated, false);
  assert.equal(repeat.patched_messages, 0);
  assert.equal(repeat.unchanged_messages, 1);
  assert.deepEqual(repeat.accounts, []);
  assert.deepEqual(messageRow(output), before);
  assert.deepEqual(fs.readFileSync(path.join(output, 'wx_' + WX, 'messages.jsonl')), projection);
  assert.deepEqual(fs.readdirSync(path.join(output, '.import-history')), historyEntries);
});

test('a missing or corrupt primary is replaced without carrying another resource kind or format metadata', async t => {
  const { root, output } = fixture(t);
  await seed(root, output, WX, 'wechat', ['missing', 'corrupt']);
  for (const id of ['missing', 'corrupt']) {
    const photo = { ...asset(output, id + '.jpg', 'photo'), kind: 'image', format: 'JPEG', width: 800 };
    await patchLibraryMedia({ outputDir: output,
      updatesFile: updateFile(root, id + '-original', [{ uid: WX, msg_uuid: id, media: photo }]) });
    fs.renameSync(path.join(output, photo.path), path.join(output, photo.path + '.retained'));
    if (id === 'corrupt') fs.writeFileSync(path.join(output, photo.path), 'other'); // same bytes, different hash
    const video = { ...asset(output, id + '.mp4', 'video'), kind: 'video' };
    const result = await patchLibraryMedia({ outputDir: output,
      updatesFile: updateFile(root, id + '-replacement', [{ uid: WX, msg_uuid: id, media: video }]) });
    assert.equal(result.patched_messages, 1);
    const meta = JSON.parse(messageRow(output, WX, id).meta_json);
    assert.deepEqual(meta.media, video);
    assert.equal(meta.media.width, undefined);
    assert.equal(meta.media.format, undefined);
  }
});

test('malformed metadata, unsafe paths, nonexistent files and incorrect file hashes roll back the whole batch', async t => {
  const { root, output } = fixture(t);
  await seed(root, output);
  const media = asset(output);
  const before = messageRow(output);
  const invalid = [
    { local_media: 'attachments/library/download.bin' },
    { local_media: [3] }, { local_media: ['attachments/../reader.sqlite'] },
    { local_media: ['attachments\\library\\download.bin'] },
    { local_media: ['attachments//library/download.bin'] },
    { local_media: ['attachments/library/download.bin:stream'] },
    { local_media: ['https://example.invalid/file'] },
    { local_media: ['attachments/missing.bin'] }, { local_media: ['attachments/library'] },
    { media: [] }, { media: { ...media, kind: 'invalid' } },
    { media: { ...media, bytes: media.bytes + 1 } }, { media: { ...media, sha256: '0'.repeat(64) } },
    { media_status: 1 }, { text_original: 'forbidden', local_media: [] },
    JSON.parse('{"media":{"__proto__":{"injected":true}}}'),
  ];
  for (let i = 0; i < invalid.length; i++) {
    const updatesFile = updateFile(root, 'bad-' + i, [
      { uid: WX, msg_uuid: 'shared-id', local_media: [media.path] },
      { uid: WX, msg_uuid: 'shared-id', ...invalid[i] },
    ]);
    await assert.rejects(patchLibraryMedia({ outputDir: output, updatesFile }), undefined, JSON.stringify(invalid[i]));
    assert.deepEqual(messageRow(output), before);
    assert.equal(query(output, 'SELECT * FROM library_media_batches').length, 0);
  }
});

test('attachment junctions cannot point outside the library', async t => {
  const { root, output } = fixture(t);
  await seed(root, output);
  const outside = path.join(root, 'outside');
  fs.mkdirSync(outside);
  fs.writeFileSync(path.join(outside, 'file.bin'), 'outside');
  fs.mkdirSync(path.join(output, 'attachments'));
  try { fs.symlinkSync(outside, path.join(output, 'attachments', 'linked'), 'junction'); }
  catch (error) {
    if (['EPERM', 'EACCES'].includes(error.code)) { t.skip('OS forbids creating test junctions'); return; }
    throw error;
  }
  const before = messageRow(output);
  const updatesFile = updateFile(root, 'junction', [{ uid: WX, msg_uuid: 'shared-id', local_media: ['attachments/linked/file.bin'] }]);
  await assert.rejects(patchLibraryMedia({ outputDir: output, updatesFile }), /inside library attachments/);
  assert.deepEqual(messageRow(output), before);
});

test('projection failure leaves a durable pending batch, and identical retry publishes without reapplying', async t => {
  const { root, output } = fixture(t);
  await seed(root, output);
  const media = asset(output);
  const updatesFile = updateFile(root, 'patch', [{ uid: WX, msg_uuid: 'shared-id', local_media: [media.path], media }]);
  const history = path.join(output, '.import-history');
  fs.renameSync(history, history + '.saved');
  fs.writeFileSync(history, 'blocks projection');
  await assert.rejects(patchLibraryMedia({ outputDir: output, updatesFile }));
  const committed = messageRow(output);
  assert.deepEqual(JSON.parse(committed.meta_json).local_media, [media.path]);
  assert.equal(query(output, 'SELECT projection_status FROM library_media_batches')[0].projection_status, 'pending');
  fs.renameSync(history, history + '.blocker');
  fs.renameSync(history + '.saved', history);
  const result = await patchLibraryMedia({ outputDir: output, updatesFile });
  assert.equal(result.repeated, true); assert.equal(result.patched_messages, 0);
  assert.equal(result.projection_status, 'completed');
  assert.deepEqual(messageRow(output), committed);
  assert.equal(query(output, 'SELECT * FROM library_media_batches').length, 1);
  const lines = fs.readFileSync(path.join(output, 'wx_' + WX, 'messages.jsonl'), 'utf8').trim().split('\n');
  assert.equal(lines.length, 1);
  assert.deepEqual(JSON.parse(JSON.parse(lines[0])._library_projection.meta_json).local_media, [media.path]);
});

test('pending projections recover from the live DB without the patch file and omit messages deleted meanwhile', async t => {
  const { root, output } = fixture(t);
  await seed(root, output, WX, 'wechat', ['survivor', 'removed']);
  const media = asset(output);
  const updatesFile = updateFile(root, 'patch', ['survivor', 'removed'].map(id => ({ uid: WX, msg_uuid: id, local_media: [media.path] })));
  assert.equal((await patchLibraryMedia({ outputDir: output, updatesFile, project: false })).projection_status, 'pending');
  fs.renameSync(updatesFile, updatesFile + '.retained');
  const store = new ChatStore(output);
  try { await store.initialize(); store.deleteMessage(WX, 'removed', 0); } finally { store.close(); }
  const rawBefore = messageRow(output, WX, 'survivor').raw_json;
  const result = await resumeLibraryMedia({ outputDir: output });
  assert.equal(result.resumed_batches, 1);
  const lines = fs.readFileSync(path.join(output, 'wx_' + WX, 'messages.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
  assert.deepEqual(lines.map(row => row.msg_uuid), ['survivor']);
  assert.equal(messageRow(output, WX, 'survivor').raw_json, rawBefore);
  assert.equal((await resumeLibraryMedia({ outputDir: output })).resumed_batches, 0);
});

test('CLI patches an existing managed library and refuses to initialize or create a missing database', async t => {
  const { root, output } = fixture(t);
  await seed(root, output);
  const media = asset(output);
  const updatesFile = updateFile(root, 'patch', [{ uid: WX, msg_uuid: 'shared-id', local_media: [media.path] }]);
  const script = fileURLToPath(new URL('../server/library-media.mjs', import.meta.url));
  const invoke = (...args) => spawnSync(process.execPath, [script, ...args], { encoding: 'utf8' });
  const patched = invoke('--output', output, '--updates', updatesFile, '--json');
  assert.equal(patched.status, 0, patched.stderr);
  assert.equal(JSON.parse(patched.stdout).patched_messages, 1);
  const resumed = invoke('--output', output, '--resume', '--json');
  assert.equal(resumed.status, 0, resumed.stderr);
  assert.equal(JSON.parse(resumed.stdout).resumed_batches, 0);
  const missing = path.join(root, 'missing');
  fs.mkdirSync(missing);
  assert.equal(invoke('--output', missing, '--updates', updatesFile).status, 1);
  assert.equal(fs.existsSync(path.join(missing, 'reader.sqlite')), false);
  exec(output, "UPDATE store_meta SET value='legacy' WHERE key='library_mode'");
  const before = messageRow(output);
  const refused = invoke('--output', output, '--updates', updatesFile);
  assert.equal(refused.status, 1);
  assert.match(refused.stderr, /managed library/);
  assert.deepEqual(messageRow(output), before);
});

test('a running reader immediately exposes patched media over HTTP without reindex or restart', async t => {
  let app;
  const { root, output } = fixture(t, async () => {
    if (app) {
      try { await app.close(); } finally { app.forceClose(); }
    }
  });
  await seed(root, output);
  app = await startTestApp({ dataDir: output, token: '' });
  const runningStore = app.app.store;
  const client = new Client(app.baseUrl);
  await client.session();
  const endpoint = `/api/message?account=${WX}&uuid=shared-id`;
  const edited = await client.json(endpoint, { method: 'PATCH',
    body: { expected_revision: 0, text_override: 'Edited through the running reader', note: 'Keep my note', hidden: true } });
  assert.equal(edited.response.status, 200);
  const before = edited.data.item;
  assert.deepEqual(before.meta.local_media, []);
  assert.equal(before.raw_json.includes('9007199254740993123'), true);
  const bytes = Buffer.from([0, 1, 7, 128, 255, 13, 10, 99]);
  const media = asset(output, 'new-live-attachment.bin', bytes);
  const updatesFile = updateFile(root, 'http-patch', [{ uid: WX, msg_uuid: 'shared-id', app: 'wechat',
    expected_raw_sha256: sha(before.raw_json), local_media: [media.path], media, media_status: 'available' }]);
  // Use the same separate-process boundary as Python while the original server and store stay open.
  const script = fileURLToPath(new URL('../server/library-media.mjs', import.meta.url));
  const patched = spawnSync(process.execPath, [script, '--output', output, '--updates', updatesFile, '--json'], { encoding: 'utf8' });
  assert.equal(patched.status, 0, patched.stderr);
  assert.equal(JSON.parse(patched.stdout).patched_messages, 1);
  const after = await client.json(endpoint);
  assert.equal(after.response.status, 200);
  assert.deepEqual(after.data.item.meta.local_media, [media.path]);
  assert.deepEqual(after.data.item.meta.media, media);
  assert.deepEqual({ ...after.data.item, meta: before.meta }, before);
  assert.equal(after.data.item.text, 'Edited through the running reader');
  assert.equal(after.data.item.edit.note, 'Keep my note');
  assert.equal(after.data.item.raw_json, before.raw_json);
  assert.equal(app.app.store, runningStore);
  assert.equal(app.server.listening, true);
  const rendered = describeMessage(after.data.item, { accountUid: WX });
  assert.equal(rendered.media.available, true);
  assert.equal(rendered.media.href, 'media/' + media.path);
  const response = await client.request('/' + rendered.media.href);
  assert.equal(response.status, 200);
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), bytes);
});
