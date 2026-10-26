/**
 * HTTP API contract tests for web/server/index.mjs.
 *
 * Uses the finalized interface:
 *   createApp({dataDir, databasePath, token, distDir, host, reindex})
 *     -> {server, store, initialization, close}
 * The app binds an ephemeral loopback port. Teardown closes the server before
 * deleting any fixture directory, which Windows requires for open SQLite files.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  ACCOUNT_A, CONVERSATION_A, UUID, BIG_SERVER_ID, MEDIA, makeExport,
} from './helpers/fixture.mjs';
import {
  Client, bodyOf, errorShape, openStalledMedia, rawRequest, startTestApp,
} from './helpers/server.mjs';

const TOKEN = 'synthetic-test-token-0123456789';

function writeDist() {
  const dist = fs.mkdtempSync(path.join(os.tmpdir(), 'dsweb-dist-'));
  fs.mkdirSync(path.join(dist, 'assets'), { recursive: true });
  fs.writeFileSync(
    path.join(dist, 'index.html'),
    '<!doctype html><html lang="zh-CN"><head><base href="./"></head>'
    + '<body><div id="app"></div><script type="module" src="./assets/app.js"></script></body></html>',
    'utf8',
  );
  fs.writeFileSync(path.join(dist, 'assets', 'app.js'), 'export default {};\n', 'utf8');
  return dist;
}

async function boot(t, { token = TOKEN } = {}) {
  const fixture = makeExport();
  const dist = writeDist();
  let app;
  try {
    app = await startTestApp({ dataDir: fixture.dir, distDir: dist, token });
  } catch (error) {
    fs.rmSync(dist, { recursive: true, force: true });
    fixture.cleanup();
    throw error;
  }
  t.after(async () => {
    await app.close();
    // Safety net: close() is already bounded, but never leave a socket behind
    // that could keep a fixture file locked on Windows.
    app.forceClose();
    fs.rmSync(dist, { recursive: true, force: true });
    fixture.cleanup();
  });

  const client = new Client(app.baseUrl);
  const session = await client.session();
  return { fixture, dist, app, client, baseUrl: app.baseUrl, port: app.port, session };
}

/** Log in and assert the full auth contract; never silently returns. */
async function authenticate(t, context) {
  const before = context.session.data || {};
  assert.equal(before.auth_required, true, 'a configured token must require authentication');
  assert.equal(before.authenticated, false, 'a fresh client must not be authenticated');
  assert.equal(before.csrf_token, undefined, 'an anonymous session must not receive a CSRF token');

  const { response, data } = await context.client.login(TOKEN);
  assert.ok(response.status === 200 || response.status === 204, `login returned ${response.status}`);
  assert.ok(context.client.lastSetCookie.some((cookie) => /httponly/i.test(cookie)),
    'the session cookie must be HttpOnly');
  assert.ok(context.client.lastSetCookie.some((cookie) => /samesite/i.test(cookie)),
    'the session cookie must declare SameSite');
  assert.equal(data?.authenticated, true);
  assert.equal(typeof data?.csrf_token, 'string');
  return data;
}

test('GET api/session reports auth state without leaking a token', async (t) => {
  const context = await boot(t);
  const { response, data } = context.session;
  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type') || '', /^application\/json/);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(data.authenticated, false);
  assert.equal(data.auth_required, true);
  assert.equal(data.csrf_token, undefined);
});

test('private API requires authentication when a token is configured', async (t) => {
  const context = await boot(t);
  for (const apiPath of ['/api/accounts', `/api/conversations?account=${ACCOUNT_A}`, '/api/decorations']) {
    const { response, data } = await context.client.json(apiPath);
    assert.equal(response.status, 401, `${apiPath} was reachable without a session`);
    assert.ok(errorShape(data), `${apiPath} did not return the documented error shape`);
  }
  const media = await fetch(`${context.baseUrl}/media/${MEDIA.text}`);
  await media.arrayBuffer();
  assert.ok([401, 403].includes(media.status), `media returned ${media.status} without a session`);
});

test('login rejects a wrong token and accepts the configured one', async (t) => {
  const context = await boot(t);
  const bad = await context.client.login('definitely-not-the-token');
  assert.ok([401, 403].includes(bad.response.status), `wrong token returned ${bad.response.status}`);
  assert.equal((await context.client.session()).data.authenticated, false);

  assert.ok(await authenticate(t, context));
  const accounts = await context.client.json('/api/accounts');
  assert.equal(accounts.response.status, 200);
});

test('logout invalidates the session', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);
  const loggedOut = await context.client.logout();
  assert.equal(loggedOut.response.status, 200);
  assert.deepEqual(loggedOut.data, { ok: true });
  assert.equal((await context.client.session()).data.authenticated, false);
  assert.equal((await context.client.json('/api/accounts')).response.status, 401);
});

test('accounts and conversations are exposed with string identifiers', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);

  const accounts = await context.client.json('/api/accounts');
  assert.equal(accounts.response.status, 200);
  assert.equal(accounts.data.items.length, 2);
  for (const account of accounts.data.items) {
    assert.equal(typeof account.uid, 'string');
    assert.equal(typeof account.name, 'string');
    assert.equal(typeof account.message_count, 'number');
  }

  const conversations = await context.client.json(`/api/conversations?account=${ACCOUNT_A}&limit=50`);
  assert.equal(conversations.response.status, 200);
  assert.equal(conversations.data.total, 1);
  assert.equal(String(conversations.data.items[0].id), CONVERSATION_A);
});

test('messages support filtering, pagination and include_hidden', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);

  const scope = `/api/messages?account=${ACCOUNT_A}&conversation=${encodeURIComponent(CONVERSATION_A)}`;
  const all = await context.client.json(`${scope}&limit=500`);
  assert.equal(all.response.status, 200);
  assert.equal(all.data.total, 7);
  assert.equal(all.data.items.length, 7);
  for (const item of all.data.items) {
    assert.equal(typeof item.msg_uuid, 'string');
    assert.equal(typeof item.raw_json, 'string');
  }

  const page = await context.client.json(`${scope}&limit=2&offset=2`);
  assert.equal(page.data.items.length, 2);
  assert.deepEqual(page.data.items.map((item) => item.msg_uuid),
    all.data.items.slice(2, 4).map((item) => item.msg_uuid));

  const search = await context.client.json(
    `/api/messages?account=${ACCOUNT_A}&q=${encodeURIComponent('unsafe integer')}`);
  assert.equal(search.data.total, 1);
  assert.equal(search.data.items[0].msg_uuid, UUID.huge);

  const including = await context.client.json(`${scope}&include_hidden=1&limit=500`);
  assert.equal(including.data.total, 7);
});

test('a single message keeps raw_json exact and meta structured', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);

  const { response, data } = await context.client.json(
    `/api/message?account=${ACCOUNT_A}&uuid=${UUID.huge}`);
  assert.equal(response.status, 200);
  const item = data.item;
  assert.equal(typeof item.raw_json, 'string');
  assert.ok(item.raw_json.includes(`"msg_server_id":${BIG_SERVER_ID}`));
  assert.ok(BigInt(item.raw_json.match(/"msg_server_id":(\d+)/)[1]) === BigInt(BIG_SERVER_ID));
  assert.equal(typeof item.meta, 'object');
  assert.equal(String(item.account_uid), ACCOUNT_A);
});

test('edits require CSRF, bump the revision and detect conflicts', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);

  const url = `/api/message?account=${ACCOUNT_A}&uuid=${UUID.text}`;

  const noCsrf = await context.client.json(url, {
    method: 'PATCH', body: { text_override: 'x', expected_revision: 0 }, csrf: false,
  });
  assert.equal(noCsrf.response.status, 403, 'a mutation without a CSRF token was accepted');

  const badOrigin = await context.client.json(url, {
    method: 'PATCH', body: { text_override: 'x', expected_revision: 0 },
    headers: { origin: 'http://evil.invalid' },
  });
  assert.equal(badOrigin.response.status, 403, 'a cross-origin mutation was accepted');

  const edited = await context.client.json(url, {
    method: 'PATCH', body: { text_override: 'edited via HTTP', note: 'n', expected_revision: 0 },
  });
  assert.equal(edited.response.status, 200);
  assert.equal(edited.data.item.text, 'edited via HTTP');
  assert.equal(edited.data.item.text_original, 'hello [smile] world');
  assert.equal(edited.data.item.edit.revision, 1);

  const conflict = await context.client.json(url, {
    method: 'PATCH', body: { text_override: 'stale', expected_revision: 0 },
  });
  assert.equal(conflict.response.status, 409);
  assert.ok(errorShape(conflict.data));

  const hidden = await context.client.json(url, {
    method: 'PATCH', body: { hidden: true, expected_revision: 1 },
  });
  assert.equal(hidden.response.status, 200);
  assert.equal(hidden.data.item.edit.hidden, true);

  const visible = await context.client.json(
    `/api/messages?account=${ACCOUNT_A}&conversation=${encodeURIComponent(CONVERSATION_A)}&limit=500`);
  assert.ok(!visible.data.items.some((item) => item.msg_uuid === UUID.text));
  const revealed = await context.client.json(
    `/api/messages?account=${ACCOUNT_A}&conversation=${encodeURIComponent(CONVERSATION_A)}&include_hidden=1&limit=500`);
  assert.ok(revealed.data.items.some((item) => item.msg_uuid === UUID.text));
});

test('DELETE restores the original and advances the revision', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);

  const url = `/api/message?account=${ACCOUNT_A}&uuid=${UUID.text}`;
  await context.client.json(url, {
    method: 'PATCH', body: { text_override: 'edited', note: 'x', hidden: true, expected_revision: 0 },
  });

  const restored = await context.client.json(url, { method: 'DELETE', body: { expected_revision: 1 } });
  assert.equal(restored.response.status, 200);
  assert.equal(restored.data.item.text, 'hello [smile] world');
  assert.equal(restored.data.item.edit.text_override, null);
  assert.equal(restored.data.item.edit.note, null);
  assert.equal(restored.data.item.edit.hidden, false);
  assert.equal(restored.data.item.edit.revision, 2);

  const stale = await context.client.json(url, {
    method: 'PATCH', body: { text_override: 'resurrected', expected_revision: 1 },
  });
  assert.equal(stale.response.status, 409);
});

test('errors use the documented shape and never expose internals', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);

  const missing = await context.client.json(`/api/message?account=${ACCOUNT_A}&uuid=does-not-exist`);
  assert.equal(missing.response.status, 404);
  assert.ok(errorShape(missing.data));

  const noAccount = await context.client.json('/api/messages?conversation=x');
  assert.ok([400, 404].includes(noAccount.response.status));
  if (noAccount.data && noAccount.data.error) assert.ok(errorShape(noAccount.data));

  const text = JSON.stringify(missing.data);
  assert.ok(!text.includes(context.fixture.dir), 'the error leaked an absolute path');
  assert.ok(!/SQLITE|sqlite3|at Object\./.test(text), 'the error leaked internals or a stack');
});

test('there is no raw SQL or arbitrary file endpoint', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);

  for (const probe of [
    '/api/query?sql=SELECT%201',
    '/api/sql?q=SELECT%201',
    '/api/export_summary.json',
    '/api/reader.sqlite',
  ]) {
    const response = await context.client.request(probe);
    const text = await response.text();
    assert.ok([400, 401, 403, 404, 405].includes(response.status), `${probe} returned ${response.status}`);
    assert.ok(!text.includes('SQLite format 3'), `${probe} served a database`);
  }
});

test('API responses are no-store JSON in UTF-8', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);
  for (const apiPath of ['/api/accounts', `/api/conversations?account=${ACCOUNT_A}`]) {
    const response = await context.client.request(apiPath);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.match(response.headers.get('content-type') || '', /^application\/json/);
    await response.arrayBuffer();
  }
});

test('static assets come from the built app, never from the export', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);

  const index = await context.client.request('/');
  const indexHtml = await index.text();
  assert.equal(index.status, 200);
  assert.match(indexHtml, /lang="zh-CN"/);
  assert.ok(!indexHtml.includes('src="/assets'), 'the built app must use a relative base');

  const asset = await fetch(`${context.baseUrl}/assets/app.js`);
  assert.equal(asset.status, 200);
  assert.match(await asset.text(), /export default/);

  for (const probe of [
    '/chat.sqlite',
    `/${ACCOUNT_A}/chat.sqlite`,
    '/reader.sqlite',
    '/messages.jsonl',
    `/${ACCOUNT_A}/messages.jsonl`,
    '/assets/../chat.sqlite',
    '/assets/%2e%2e/export_summary.json',
  ]) {
    const response = await fetch(`${context.baseUrl}${probe}`);
    const text = await response.text();
    assert.ok([400, 403, 404].includes(response.status), `${probe} returned ${response.status}`);
    assert.ok(!text.includes('SQLite format 3'), `${probe} served a database`);
    assert.ok(!text.includes('"total_message_count"'), `${probe} served export JSON`);
  }
});

test('media is served with ranges and rejects traversal', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);

  const full = await bodyOf(await context.client.request(`/media/${MEDIA.text}`));
  assert.equal(full.status, 200);
  assert.equal(full.buffer.toString('utf8'), context.fixture.textFile);
  assert.equal(full.headers['accept-ranges'], 'bytes');
  assert.equal(full.headers['x-content-type-options'], 'nosniff');

  const ranged = await bodyOf(await context.client.request(`/media/${MEDIA.big}`, {
    headers: { range: 'bytes=0-15' },
  }));
  assert.equal(ranged.status, 206);
  assert.equal(ranged.buffer.length, 16);
  assert.equal(ranged.headers['content-range'], `bytes 0-15/${context.fixture.bigFileBytes}`);

  const head = await bodyOf(await context.client.request(`/media/${MEDIA.big}`, { method: 'HEAD' }));
  assert.equal(head.status, 200);
  assert.equal(head.buffer.length, 0);

  for (const probe of [
    '/media/attachments/%2e%2e/reader.sqlite',
    '/media/messages.jsonl',
    '/media/attachments/%2e%2e/%2e%2e/export_summary.json',
  ]) {
    const response = await context.client.request(probe);
    const text = await response.text();
    assert.ok([400, 403, 404].includes(response.status), `${probe} returned ${response.status}`);
    assert.ok(!text.includes(context.fixture.dir), `${probe} leaked the export path`);
  }
});

test('decorations are served from the decoration manifest', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);
  const { response, data } = await context.client.json('/api/decorations');
  assert.equal(response.status, 200);
  assert.ok(data.bubbles && typeof data.bubbles === 'object');
  assert.ok(data.inline_emoji && typeof data.inline_emoji === 'object');
  const reference = data.inline_emoji['[smile]'];
  assert.ok(reference, 'the inline emoji decoration is missing');
  assert.ok(!path.isAbsolute(reference.path), 'decoration path must be relative');
});

test('a token-protected loopback server accepts a proxied public Host', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);

  // Behind a reverse proxy the Host header is the public name, not loopback.
  const proxied = await rawRequest({
    port: context.port,
    path: '/api/session',
    headers: { host: 'reader.example.com', accept: 'application/json' },
  });
  assert.equal(proxied.status, 200, 'a public Host behind a proxy was rejected despite a token');

  const accounts = await rawRequest({
    port: context.port,
    path: '/api/accounts',
    headers: {
      host: 'reader.example.com',
      accept: 'application/json',
      cookie: context.client.cookieHeader(),
    },
  });
  assert.equal(accounts.status, 200, 'the session cookie was rejected for a proxied Host');
});

test('csrf origin must match the host the proxy forwards', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);

  const payload = JSON.stringify({ text_override: 'proxied edit', expected_revision: 0 });
  // A reverse proxy rewrites Host to the public name and forwards it, so Origin
  // must match that Host; X-Forwarded-Host is advisory only.
  const request = (headers) => rawRequest({
    port: context.port,
    path: `/api/message?account=${ACCOUNT_A}&uuid=${UUID.text}`,
    method: 'PATCH',
    headers: {
      host: 'reader.example.com',
      'content-type': 'application/json',
      'content-length': Buffer.byteLength(payload),
      cookie: context.client.cookieHeader(),
      'x-csrf-token': context.client.csrf,
      'x-forwarded-host': 'reader.example.com',
      'x-forwarded-proto': 'https',
      ...headers,
    },
    body: payload,
  });

  const matching = await request({ origin: 'https://reader.example.com' });
  assert.equal(matching.status, 200, 'a same-origin proxied mutation was rejected');

  const mismatched = await request({ origin: 'https://evil.example.com' });
  assert.equal(mismatched.status, 403, 'a cross-origin proxied mutation was accepted');

  // The session cookie must be Secure when the proxy reports an https origin.
  const loginBody = JSON.stringify({ token: TOKEN });
  const login = await rawRequest({
    port: context.port,
    path: '/api/login',
    method: 'POST',
    headers: {
      host: 'reader.example.com',
      accept: 'application/json',
      'content-type': 'application/json',
      'content-length': Buffer.byteLength(loginBody),
      'x-forwarded-proto': 'https',
    },
    body: loginBody,
  });
  assert.ok([200, 204].includes(login.status), `proxied login returned ${login.status}`);
  const setCookie = [].concat(login.headers['set-cookie'] ?? []).join('; ');
  assert.match(setCookie, /Secure/i, 'the session cookie must be Secure behind an https proxy');
  assert.match(setCookie, /HttpOnly/i, 'the session cookie must be HttpOnly');
});

test('without a token, a non-loopback Host is rejected', async (t) => {
  const context = await boot(t, { token: '' });
  assert.equal((context.session.data || {}).auth_required, false);

  const local = await rawRequest({
    port: context.port,
    path: '/api/session',
    headers: { host: `127.0.0.1:${context.port}`, accept: 'application/json' },
  });
  assert.equal(local.status, 200);

  const foreign = await rawRequest({
    port: context.port,
    path: '/api/session',
    headers: { host: 'evil.example.com', accept: 'application/json' },
  });
  assert.ok([400, 403].includes(foreign.status), `a foreign Host returned ${foreign.status}`);
});

test('Origin treats explicit default ports canonically and rejects other ports', async (t) => {
  const context = await boot(t);
  const body = JSON.stringify({ token: TOKEN });
  for (const [host, origin, status] of [
    ['reader.example.com:443', 'https://reader.example.com', 200],
    ['reader.example.com', 'https://reader.example.com:443', 200],
    ['reader.example.com:80', 'http://reader.example.com', 200],
    ['reader.example.com', 'http://reader.example.com:80', 200],
    ['reader.example.com:8443', 'https://reader.example.com:8443', 200],
    ['reader.example.com:444', 'https://reader.example.com', 403],
    ['reader.example.com', 'https://reader.example.com:444', 403],
    ['reader.example.com:443', 'http://reader.example.com', 403],
    ['reader.example.com:81', 'http://reader.example.com', 403],
  ]) {
    const response = await rawRequest({ port: context.port, path: '/api/login', method: 'POST',
      headers: { host, origin, 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) }, body });
    assert.equal(response.status, status, `Host ${host} and Origin ${origin}`);
  }
});

test('X-Forwarded-Host cannot override a mismatched actual Host', async (t) => {
  const context = await boot(t);
  const body = JSON.stringify({ token: TOKEN });
  const response = await rawRequest({ port: context.port, path: '/api/login', method: 'POST',
    headers: { host: 'upstream.invalid', origin: 'https://reader.example.com',
      'x-forwarded-host': 'reader.example.com', 'x-forwarded-proto': 'https',
      'content-type': 'application/json', 'content-length': Buffer.byteLength(body) }, body });
  assert.equal(response.status, 403);
});

test('logout clears a Secure cookie behind HTTPS proxy', async (t) => {
  const context = await boot(t);
  await authenticate(t, context);
  const response = await context.client.json('/api/logout', { method: 'POST', body: {},
    headers: { 'x-forwarded-proto': 'https' } });
  assert.equal(response.response.status, 200);
  assert.match(context.client.lastSetCookie.join('; '), /Max-Age=0/);
  assert.match(context.client.lastSetCookie.join('; '), /Secure/);
});

test('shutdown bounds a stalled media connection and releases SQLite', { timeout: 9000 }, async (t) => {
  const context = await boot(t);
  await authenticate(t, context);
  // Exceed socket buffers so the server remains actively backpressured.
  const filename = path.join(context.fixture.dir, MEDIA.big);
  fs.truncateSync(filename, 32 * 1024 * 1024);
  const stalled = await openStalledMedia({ port: context.port, path: `/media/${MEDIA.big}`,
    cookie: context.client.cookieHeader() });
  t.after(() => { stalled.request.destroy(); stalled.response.destroy(); });
  assert.equal(stalled.response.statusCode, 200);
  const started = performance.now();
  await Promise.all([context.app.app.close(), context.app.app.close()]);
  const elapsed = performance.now() - started;
  assert.ok(elapsed >= 4500, `stream was not stalled: ${elapsed}ms`);
  assert.ok(elapsed < 7500, `close exceeded its deadline: ${elapsed}ms`);
  assert.equal(context.app.server.listening, false);
  assert.throws(() => context.app.app.store.listAccounts());
  // Renaming proves that the OS database handle has actually been released.
  const database = path.join(context.fixture.dir, 'reader.sqlite');
  fs.renameSync(database, database + '.closed');
  fs.renameSync(database + '.closed', database);
});
