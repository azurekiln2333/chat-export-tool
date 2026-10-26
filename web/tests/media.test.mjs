/**
 * Media helper contract tests (web/server/media.mjs).
 *
 * Covers contained path resolution, traversal/symlink escapes, HEAD and single
 * byte ranges over a real loopback HTTP server, and 416 for invalid ranges.
 * No external host is contacted.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import test from 'node:test';

import { ACCOUNT_A, MEDIA, makeExport, copyExport, canSymlink } from './helpers/fixture.mjs';
import { bodyOf } from './helpers/server.mjs';

let module;
try {
  module = await import('../server/media.mjs');
} catch (error) {
  throw new Error(`web/server/media.mjs could not be imported: ${error.code || error.message}`);
}
for (const name of ['resolveMediaPath', 'serveMedia']) {
  if (typeof module[name] !== 'function') {
    throw new Error(`web/server/media.mjs must export ${name}`);
  }
}

const ALLOWED_STATUSES = new Set([400, 403, 404, 416]);

async function settle(fn) {
  try {
    return { value: await fn() };
  } catch (error) {
    return { error };
  }
}

function assertRejected(outcome, label) {
  if (outcome.error) {
    const status = outcome.error.status;
    const code = outcome.error.code;
    assert.ok(ALLOWED_STATUSES.has(status), `${label}: unexpected status ${status}`);
    assert.equal(typeof code, 'string', `${label}: error.code must be a string`);
    assert.ok(!/[:\\/][A-Za-z]:\\/.test(String(outcome.error.message)),
      `${label}: the error message leaks an absolute path`);
    return;
  }
  assert.fail(`${label}: expected rejection, got ${String(outcome.value)}`);
}

function containedInAttachments(candidate, exportDir) {
  const real = fs.realpathSync(candidate);
  const attachments = fs.realpathSync(path.join(exportDir, 'attachments'));
  return real === attachments || real.startsWith(attachments + path.sep);
}

/** A coordinator-like server: decode the path exactly once, then delegate. */
function serveMediaServer(exportDir) {
  return http.createServer((request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    const prefix = '/media/';
    const jsonError = (status, code, message) => {
      response.statusCode = status;
      response.setHeader('content-type', 'application/json; charset=utf-8');
      response.setHeader('x-content-type-options', 'nosniff');
      response.end(JSON.stringify({ error: { code, message } }));
    };
    if (!url.pathname.startsWith(prefix)) {
      jsonError(404, 'not_found', 'no such resource');
      return;
    }
    let relative;
    try {
      relative = decodeURIComponent(url.pathname.slice(prefix.length));
    } catch {
      jsonError(400, 'bad_path', 'the media path is not valid');
      return;
    }
    Promise.resolve(module.serveMedia(request, response, exportDir, relative)).catch((error) => {
      if (response.headersSent) {
        response.destroy();
        return;
      }
      jsonError(ALLOWED_STATUSES.has(error?.status) ? error.status : 500,
        typeof error?.code === 'string' ? error.code : 'media_error',
        'the media file could not be served');
    });
  });
}

async function withServer(t, exportDir, cleanups = []) {
  const server = serveMediaServer(exportDir);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  // One hook: stop the server, then delete the fixture, so Windows never holds
  // an open handle on a file being removed.
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    for (const cleanup of cleanups) {
      try {
        cleanup();
      } catch {
        /* best effort */
      }
    }
  });
  const { port } = server.address();
  return `http://127.0.0.1:${port}`;
}

test('resolves a contained attachment inside the export', async (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());

  for (const relative of [MEDIA.voice, MEDIA.sticker, MEDIA.text]) {
    const resolved = await module.resolveMediaPath(fixture.dir, relative);
    assert.equal(typeof resolved, 'string');
    assert.ok(containedInAttachments(resolved, fixture.dir), `${relative} resolved outside attachments`);
    assert.equal(fs.realpathSync(resolved), fs.realpathSync(path.join(fixture.dir, relative)));
  }
});

test('rejects traversal, absolute, drive and non-attachment paths', async (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());

  const hostile = [
    'attachments/../../reader.sqlite',
    'attachments/../messages.jsonl',
    'attachments/../../../etc/passwd',
    '/attachments/voice/v1.m4a',
    'attachments\\voice\\v1.m4a',
    'attachments/....//voice/v1.m4a',
    'C:/Windows/System32/drivers/etc/hosts',
    'messages.jsonl',
    'reader.sqlite',
    'attachments/does-not-exist.bin',
  ];
  for (const relative of hostile) {
    const outcome = await settle(() => module.resolveMediaPath(fixture.dir, relative));
    assertRejected(outcome, JSON.stringify(relative));
  }
});

test('rejects a symlink that escapes attachments', async (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());
  if (!canSymlink(fixture.dir)) {
    t.skip('this environment cannot create symlinks');
    return;
  }
  const link = path.join(fixture.dir, 'attachments', 'escape.bin');
  try {
    fs.symlinkSync(path.join(fixture.dir, 'export_summary.json'), link, 'file');
  } catch {
    t.skip('symlink creation was rejected by the OS');
    return;
  }
  const outcome = await settle(() => module.resolveMediaPath(fixture.dir, 'attachments/escape.bin'));
  assertRejected(outcome, 'attachments/escape.bin (symlink escape)');
});

test('serves a full file with the required headers', async (t) => {
  const fixture = makeExport();
  const base = await withServer(t, fixture.dir, [() => fixture.cleanup()]);

  const response = await fetch(`${base}/media/${MEDIA.text}`);
  const body = Buffer.from(await response.arrayBuffer());
  assert.equal(response.status, 200);
  assert.equal(body.toString('utf8'), fixture.textFile);
  assert.equal(response.headers.get('accept-ranges'), 'bytes');
  assert.equal(response.headers.get('content-length'), String(body.length));
  assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
  // An unknown extension falls back to a non-executable type on purpose.
  assert.equal(response.headers.get('content-type'), 'application/octet-stream');
});

test('uses the media MIME map for known attachment types', async (t) => {
  const fixture = makeExport();
  const base = await withServer(t, fixture.dir, [() => fixture.cleanup()]);

  const expected = [
    [MEDIA.voice, /^audio\/mp4/],
    [MEDIA.sticker, /^image\/png/],
    [MEDIA.inlineEmoji, /^image\/png/],
    [MEDIA.big, /^application\/octet-stream/],
  ];
  for (const [relative, pattern] of expected) {
    const response = await fetch(`${base}/media/${relative}`);
    assert.equal(response.status, 200, `${relative} was not served`);
    assert.match(response.headers.get('content-type') || '', pattern, `${relative} MIME mismatch`);
    assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
    await response.arrayBuffer();
  }
});

test('HEAD returns headers without a body', async (t) => {
  const fixture = makeExport();
  const base = await withServer(t, fixture.dir, [() => fixture.cleanup()]);

  const response = await fetch(`${base}/media/${MEDIA.big}`, { method: 'HEAD' });
  const body = Buffer.from(await response.arrayBuffer());
  assert.equal(response.status, 200);
  assert.equal(body.length, 0);
  assert.equal(Number(response.headers.get('content-length')), fixture.bigFileBytes);
  assert.equal(response.headers.get('accept-ranges'), 'bytes');
});

test('supports a single HTTP byte range', async (t) => {
  const fixture = makeExport();
  const base = await withServer(t, fixture.dir, [() => fixture.cleanup()]);

  const response = await fetch(`${base}/media/${MEDIA.big}`, { headers: { range: 'bytes=0-9' } });
  const body = Buffer.from(await response.arrayBuffer());
  assert.equal(response.status, 206);
  assert.equal(body.length, 10);
  assert.equal(response.headers.get('content-range'), `bytes 0-9/${fixture.bigFileBytes}`);
  assert.equal(response.headers.get('content-length'), '10');
  const expected = fs.readFileSync(path.join(fixture.dir, MEDIA.big)).subarray(0, 10);
  assert.deepEqual(body, expected);

  const tail = await fetch(`${base}/media/${MEDIA.big}`, {
    headers: { range: `bytes=${fixture.bigFileBytes - 16}-` },
  });
  const tailBody = Buffer.from(await tail.arrayBuffer());
  assert.equal(tail.status, 206);
  assert.equal(tailBody.length, 16);
  assert.equal(tail.headers.get('content-range'),
    `bytes ${fixture.bigFileBytes - 16}-${fixture.bigFileBytes - 1}/${fixture.bigFileBytes}`);
});

test('rejects invalid and multiple ranges with 416', async (t) => {
  const fixture = makeExport();
  const base = await withServer(t, fixture.dir, [() => fixture.cleanup()]);

  const beyond = await fetch(`${base}/media/${MEDIA.text}`, {
    headers: { range: `bytes=${fixture.textFile.length + 100}-` },
  });
  assert.equal(beyond.status, 416);
  await beyond.arrayBuffer();

  const multiple = await fetch(`${base}/media/${MEDIA.big}`, {
    headers: { range: 'bytes=0-1,4-5' },
  });
  assert.equal(multiple.status, 416);
  await multiple.arrayBuffer();
});

test('rejects traversal over HTTP without leaking paths', async (t) => {
  const fixture = makeExport();
  const base = await withServer(t, fixture.dir, [() => fixture.cleanup()]);

  const attempts = [
    '/media/attachments/%2e%2e/reader.sqlite',
    '/media/attachments/..%2f..%2fexport_summary.json',
    '/media/messages.jsonl',
    '/media/attachments/%5c..%5cmessages.jsonl',
  ];
  for (const attempt of attempts) {
    const response = await fetch(`${base}${attempt}`);
    const text = await response.text();
    assert.ok([400, 403, 404].includes(response.status), `${attempt} returned ${response.status}`);
    assert.ok(!text.includes(fixture.dir), `${attempt} leaked the export path`);
    assert.ok(!text.includes('SQLite format 3'), `${attempt} served a database`);
    let parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
    if (parsed && parsed.error) {
      assert.equal(typeof parsed.error.code, 'string');
      assert.equal(typeof parsed.error.message, 'string');
    }
  }
});

test('media references stay relative after the export is copied', async (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());
  const moved = copyExport(fixture.dir, `${fixture.dir}-moved`);
  t.after(() => fs.rmSync(moved, { recursive: true, force: true }));

  const manifest = JSON.parse(fs.readFileSync(path.join(moved, 'media_manifest.json'), 'utf8'));
  const decorations = JSON.parse(fs.readFileSync(path.join(moved, 'decoration_manifest.json'), 'utf8'));
  const references = [
    ...manifest.resources.map((resource) => resource.path),
    ...Object.values(manifest.messages).map((entry) => entry.path).filter(Boolean),
    ...Object.values(decorations.inline_emoji).map((asset) => asset.path),
    ...Object.values(decorations.bubbles).flatMap((bubble) => Object.values(bubble.sides).map((side) => side.path)),
  ];
  for (const reference of references) {
    assert.ok(!path.isAbsolute(reference), `${reference} is an absolute path`);
    assert.ok(!reference.includes('\\'), `${reference} uses a backslash`);
    assert.ok(reference.startsWith('attachments/'), `${reference} is outside attachments/`);
    const resolved = await module.resolveMediaPath(moved, reference);
    assert.ok(fs.existsSync(resolved), `${reference} does not resolve in the copied export`);
  }
});

test('If-Range with a weak validator returns the full representation', async (t) => {
  const fixture = makeExport();
  const base = await withServer(t, fixture.dir, [() => fixture.cleanup()]);

  const plain = await fetch(`${base}/media/${MEDIA.big}`);
  const etag = plain.headers.get('etag');
  assert.ok(etag, 'the response must carry an ETag');
  await plain.arrayBuffer();

  // RFC 9110: a weak validator must not be used for a range request.
  const weak = await bodyOf(await fetch(`${base}/media/${MEDIA.big}`, {
    headers: { range: 'bytes=0-15', 'if-range': `W/${etag}` },
  }));
  assert.equal(weak.status, 200, 'a weak If-Range validator must be ignored');
  assert.equal(weak.buffer.length, fixture.bigFileBytes, 'the full file must be returned');
  assert.equal(weak.headers['content-range'], undefined);
});

test('If-Range with a strong validator only ranges when it matches', async (t) => {
  const fixture = makeExport();
  const base = await withServer(t, fixture.dir, [() => fixture.cleanup()]);

  const plain = await fetch(`${base}/media/${MEDIA.big}`);
  const etag = plain.headers.get('etag');
  await plain.arrayBuffer();

  const matching = await bodyOf(await fetch(`${base}/media/${MEDIA.big}`, {
    headers: { range: 'bytes=0-15', 'if-range': etag },
  }));
  assert.equal(matching.status, 206, 'a matching strong validator must serve the range');
  assert.equal(matching.buffer.length, 16);

  const stale = await bodyOf(await fetch(`${base}/media/${MEDIA.big}`, {
    headers: { range: 'bytes=0-15', 'if-range': '"deadbeefcafe"' },
  }));
  assert.equal(stale.status, 200, 'a stale validator must serve the full file');
  assert.equal(stale.buffer.length, fixture.bigFileBytes);
});

test('If-None-Match returns 304 with an empty body', async (t) => {
  const fixture = makeExport();
  const base = await withServer(t, fixture.dir, [() => fixture.cleanup()]);

  const first = await fetch(`${base}/media/${MEDIA.text}`);
  const etag = first.headers.get('etag');
  await first.arrayBuffer();

  const cached = await bodyOf(await fetch(`${base}/media/${MEDIA.text}`, {
    headers: { 'if-none-match': etag },
  }));
  assert.equal(cached.status, 304);
  assert.equal(cached.buffer.length, 0);
  assert.equal(cached.headers['accept-ranges'], 'bytes');

  const original = await fetch(`${base}/media/${MEDIA.text}`);
  await original.arrayBuffer();
  const lastModified = original.headers.get('last-modified') ?? first.headers.get('last-modified');
  if (lastModified) {
    const byDate = await bodyOf(await fetch(`${base}/media/${MEDIA.text}`, {
      headers: { 'if-modified-since': lastModified },
    }));
    assert.equal(byDate.status, 304);
  }
});

test('rejects a directory junction that escapes attachments', async (t) => {
  const fixture = makeExport();
  t.after(() => fixture.cleanup());

  const junction = path.join(fixture.dir, 'attachments', 'escape_dir');
  try {
    fs.symlinkSync(fixture.dir, junction, 'junction');
  } catch (error) {
    t.skip(`this platform cannot create a directory junction (${error.code || error.message})`);
    return;
  }

  // attachments/escape_dir/<root file> resolves outside attachments/ via the
  // reparse point; the resolver must refuse it. The package name is not the
  // point: any root file outside attachments/ must be unreachable.
  const outcome = await settle(() => module.resolveMediaPath(
    fixture.dir, 'attachments/escape_dir/export_summary.json'));
  assertRejected(outcome, 'attachments/escape_dir/export_summary.json (junction escape)');

  const nested = await settle(() => module.resolveMediaPath(
    fixture.dir, `attachments/escape_dir/${ACCOUNT_A}/messages.jsonl`));
  assertRejected(nested, 'junction escape into an account directory');
});