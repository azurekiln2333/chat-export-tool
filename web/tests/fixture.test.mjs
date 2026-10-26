/**
 * Self-checks for the synthetic export fixture.
 *
 * These run even before the server modules exist, so a broken fixture is
 * reported as a fixture failure instead of being masked by skipped tests.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import {
  ACCOUNT_A, ACCOUNT_B, BIG_INDEX, BIG_SERVER_ID, MEDIA, UUID,
  hashExport, listExportFiles, makeExport, copyExport, readJson,
} from './helpers/fixture.mjs';

test('fixture contains only constructed placeholder identifiers', () => {
  // Account ids and message ids are generated from a fixed prefix, never
  // copied from a real export.
  for (const uid of [ACCOUNT_A, ACCOUNT_B]) {
    assert.match(uid, /^(81|82)\d{14}$/);
  }
  for (const uuid of Object.values(UUID)) {
    assert.match(uuid, /^([ab])\1{7}-0000-4000-8000-\d{12}$/);
  }
});

test('fixture ships the standard English export files', (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());
  const files = listExportFiles(fixture.dir);
  for (const required of [
    'export_summary.json', 'media_manifest.json', 'decoration_manifest.json',
    'index.html', 'reader.css',
    `${ACCOUNT_A}/messages.jsonl`, `${ACCOUNT_A}/chat.sqlite`, `${ACCOUNT_A}/statistics.json`,
    `${ACCOUNT_A}/contacts.json`, `${ACCOUNT_A}/conversation_metadata.json`,
    `${ACCOUNT_B}/messages.jsonl`,
  ]) {
    assert.ok(files.includes(required), `missing fixture file ${required}`);
  }
  for (const name of files) {
    assert.ok(!/[^\x00-\x7F]/.test(name), `generated name is not ASCII: ${name}`);
  }
});

test('fixture JSONL keeps integers beyond 2^53 as exact literals', (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());
  const lines = fs.readFileSync(path.join(fixture.dir, ACCOUNT_A, 'messages.jsonl'), 'utf8')
    .trim().split('\n');
  const line = lines.find((entry) => entry.includes(BIG_SERVER_ID));
  assert.ok(line, 'no row with the unsafe integer');
  assert.ok(line.includes(`"msg_server_id":${BIG_SERVER_ID}`));
  assert.ok(line.includes(`"index_in_conversation":${BIG_INDEX}`));

  // The value is deliberately unsafe for JSON.parse, which is what makes the
  // raw_json string contract meaningful.
  assert.equal(String(JSON.parse(line).msg_server_id), '9007199254740992');
  assert.equal(String(JSON.parse(line).index_in_conversation), String(Number(BIG_INDEX)));
});

test('fixture carries HTML/script text and formula text', (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());
  const jsonl = fs.readFileSync(path.join(fixture.dir, ACCOUNT_A, 'messages.jsonl'), 'utf8');
  assert.ok(jsonl.includes('<script>alert(\\"xss\\")</script>'));
  assert.ok(jsonl.includes('=SUM(A1:A2)'));
  const decorations = readJson(fixture.dir, 'decoration_manifest.json');
  assert.ok(decorations.inline_emoji['[smile]']);
});

test('fixture media files exist with the recorded bytes', (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());
  for (const [relative, expected] of Object.entries(fixture.mediaHashes)) {
    const absolute = path.join(fixture.dir, relative);
    assert.ok(fs.existsSync(absolute), `missing media ${relative}`);
    assert.equal(fs.statSync(absolute).size > 0, true, `empty media ${relative}`);
  }
  const big = path.join(fixture.dir, MEDIA.big);
  assert.equal(fs.statSync(big).size, fixture.bigFileBytes);
  const manifest = readJson(fixture.dir, 'media_manifest.json');
  for (const resource of manifest.resources) {
    assert.equal(resource.sha256, fixture.mediaHashes[resource.path]);
    assert.equal(resource.bytes, fs.statSync(path.join(fixture.dir, resource.path)).size);
  }
  assert.equal(manifest.messages[`${ACCOUNT_A}:${UUID.unavailable}`].status, 'missing_url');
});

test('hashing ignores reader.sqlite artifacts and detects real changes', (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());
  const before = hashExport(fixture.dir);

  // reader.sqlite and its sidecars are the only files the store may create.
  fs.writeFileSync(path.join(fixture.dir, 'reader.sqlite'), 'not really sqlite');
  fs.writeFileSync(path.join(fixture.dir, 'reader.sqlite-wal'), 'wal');
  assert.equal(hashExport(fixture.dir), before);

  // Any change to an original file must be detected.
  fs.appendFileSync(path.join(fixture.dir, 'export_summary.json'), ' ');
  assert.notEqual(hashExport(fixture.dir), before);
});

test('copying the export preserves every original file byte-for-byte', (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());
  const moved = copyExport(fixture.dir, `${fixture.dir}-copy`);
  t.after(() => fs.rmSync(moved, { recursive: true, force: true }));

  assert.equal(hashExport(moved), hashExport(fixture.dir));
  const manifest = readJson(moved, 'media_manifest.json');
  for (const resource of manifest.resources) {
    assert.ok(!path.isAbsolute(resource.path));
    assert.ok(fs.existsSync(path.join(moved, resource.path)));
  }
});

test('the fixture leaves nothing behind outside its temporary directory', (t) => {
  const fixture = makeExport();
  const created = fixture.dir;
  fixture.cleanup();
  assert.equal(fs.existsSync(created), false);
});