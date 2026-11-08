import http from 'node:http';
import path from 'node:path';
import fs from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { ChatStore } from './store.mjs';
import { serveMedia } from './media.mjs';

const DEFAULT_DIST = fileURLToPath(new URL('../dist/', import.meta.url));
const SESSION_COOKIE = 'duoshan_reader';
const SESSION_AGE = 12 * 60 * 60 * 1000;
const BODY_LIMIT = 1024 * 1024;
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1']);
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.ico': 'image/x-icon', '.webp': 'image/webp', '.woff2': 'font/woff2' };

class HttpError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

function equalSecret(left, right) {
  const a = Buffer.from(String(left || ''));
  const b = Buffer.from(String(right || ''));
  return a.length === b.length && a.length > 0 && timingSafeEqual(a, b);
}

function json(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body), 'Cache-Control': 'no-store' });
  res.end(body);
}

function headers(res) {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'same-origin');
  res.setHeader('X-Frame-Options', 'DENY');
  res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'");
}

function checkOrigin(req) {
  const origin = req.headers.origin;
  if (!origin) return;
  try {
    const parsed = new URL(origin);
    const authority = req.headers.host;
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password
      || parsed.pathname !== '/' || parsed.search || parsed.hash
      || typeof authority !== 'string' || /[\s/\\?#@]/.test(authority)) throw new Error('Invalid origin.');
    // URL canonicalization equates :443 with HTTPS and :80 with HTTP while
    // preserving non-default ports. Only the actual Host header is trusted.
    const expected = new URL(`${parsed.protocol}//${authority}`);
    if (parsed.origin === expected.origin) return;
  } catch { /* Reject malformed Origin or Host values below. */ }
  throw new HttpError(403, 'origin_rejected', 'Origin is not allowed.');
}

function secureCookie(req) {
  return req.socket.encrypted || req.headers['x-forwarded-proto'] === 'https' ? '; Secure' : '';
}

async function bodyJson(req) {
  if (!(req.headers['content-type'] || '').toLowerCase().startsWith('application/json')) {
    throw new HttpError(400, 'invalid_content_type', 'Use application/json.');
  }
  const advertised = Number(req.headers['content-length'] || 0);
  if (!Number.isFinite(advertised) || advertised > BODY_LIMIT) {
    throw new HttpError(413, 'body_too_large', 'Request body is too large.');
  }
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > BODY_LIMIT) throw new HttpError(413, 'body_too_large', 'Request body is too large.');
    chunks.push(chunk);
  }
  let body;
  try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new HttpError(400, 'invalid_json', 'Request body must be valid JSON.'); }
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new HttpError(400, 'invalid_json', 'Request body must be a JSON object.');
  }
  return body;
}

function queryInteger(search, name, defaultValue, minimum, maximum) {
  const value = search.get(name);
  if (value === null) return defaultValue;
  if (!/^\d+$/.test(value)) throw new HttpError(400, 'invalid_query', `${name} must be an integer.`);
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < minimum || number > maximum) {
    throw new HttpError(400, 'invalid_query', `${name} is outside the allowed range.`);
  }
  return number;
}

function queryOptions(search) {
  const q = search.get('q') || '';
  if (q.length > 2000) throw new HttpError(400, 'invalid_query', 'Search text is too long.');
  const hidden = search.get('include_hidden');
  if (hidden !== null && !['0', '1'].includes(hidden)) {
    throw new HttpError(400, 'invalid_query', 'include_hidden must be 0 or 1.');
  }
  return { q, limit: queryInteger(search, 'limit', 100, 1, 500),
    offset: queryInteger(search, 'offset', 0, 0, Number.MAX_SAFE_INTEGER),
    includeHidden: hidden === '1' };
}

function required(search, name) {
  const value = search.get(name);
  if (!value || value.length > 2048) throw new HttpError(400, 'invalid_query', `${name} is required.`);
  return value;
}

function deletionConfirmed(body) {
  if (body.confirm_delete !== true) {
    throw new HttpError(400, 'confirmation_required', 'Permanent deletion requires confirm_delete: true.');
  }
  return body;
}

async function serveStatic(req, res, distDir, pathname) {
  const relative = pathname === '/' ? 'index.html' : pathname.slice(1);
  if (!relative || relative.includes('\\') || relative.includes('\0') || relative.split('/').some(p => p === '..' || p.startsWith('.'))) {
    throw new HttpError(404, 'not_found', 'Not found.');
  }
  const base = await fs.realpath(distDir).catch(() => null);
  if (!base) throw new HttpError(503, 'frontend_not_built', 'Build the frontend with npm run build first.');
  const target = await fs.realpath(path.resolve(base, relative)).catch(() => null);
  const extension = path.extname(relative).toLowerCase();
  if (!target || !target.startsWith(base + path.sep) || !TYPES[extension]) {
    throw new HttpError(404, 'not_found', 'Not found.');
  }
  const stat = await fs.stat(target);
  if (!stat.isFile()) throw new HttpError(404, 'not_found', 'Not found.');
  res.writeHead(200, { 'Content-Type': TYPES[extension], 'Content-Length': stat.size,
    'Cache-Control': extension === '.html' ? 'no-store' : 'public, max-age=3600' });
  if (req.method === 'HEAD') return res.end();
  await new Promise((resolve, reject) => {
    const stream = createReadStream(target);
    stream.once('error', reject);
    res.once('close', () => stream.destroy());
    stream.once('close', resolve);
    stream.pipe(res);
  });
}

export async function createApp({ dataDir, databasePath, token = '', distDir = DEFAULT_DIST,
  host = '127.0.0.1', reindex = false } = {}) {
  if (!dataDir) throw new Error('A data directory is required (--data OUTPUT).');
  if (!LOOPBACK.has(host) && !token) throw new Error('A token is required for a non-loopback host.');
  if (typeof token !== 'string') throw new Error('The access token must be a string.');
  const output = path.resolve(dataDir);
  const store = new ChatStore(output, { databasePath });
  let initialization;
  try { initialization = await store.initialize({ force: reindex }); }
  catch (error) { store.close(); throw error; }
  const sessions = new Map();
  const localCsrf = randomBytes(32).toString('hex');
  const authRequired = Boolean(token);

  function getSession(req) {
    if (!authRequired) return { csrf: localCsrf };
    const cookie = String(req.headers.cookie || '').split(';').map(p => p.trim())
      .find(p => p.startsWith(SESSION_COOKIE + '='));
    const id = cookie?.slice(SESSION_COOKIE.length + 1);
    const session = id && sessions.get(id);
    if (!session) return null;
    if (Date.now() - session.created > SESSION_AGE) { sessions.delete(id); return null; }
    return { ...session, id };
  }

  function sessionResponse(session) {
    return { authenticated: Boolean(session), auth_required: authRequired,
      app_label: store.appLabel(),
      ...(session ? { csrf_token: session.csrf } : {}) };
  }

  const pendingRequests = new Set();
  const server = http.createServer((req, res) => {
    const task = handleRequest(req, res);
    pendingRequests.add(task);
    task.finally(() => pendingRequests.delete(task)).catch(() => {});
  });
  async function handleRequest(req, res) {
    headers(res);
    try {
      // Token-protected instances may receive a public Host through a proxy.
      if (LOOPBACK.has(host) && !authRequired) {
        let requestHost;
        try { requestHost = new URL('http://' + req.headers.host).hostname.replace(/^\[|\]$/g, ''); }
        catch { throw new HttpError(403, 'host_rejected', 'Host is not allowed.'); }
        if (!LOOPBACK.has(requestHost)) throw new HttpError(403, 'host_rejected', 'Host is not allowed.');
      }
      const url = new URL(req.url, 'http://localhost');
      let pathname;
      try { pathname = decodeURIComponent(url.pathname); }
      catch { throw new HttpError(400, 'invalid_path', 'Invalid URL encoding.'); }
      if (pathname.includes('\0')) throw new HttpError(400, 'invalid_path', 'Invalid URL path.');
      const method = req.method;
      const mutating = !['GET', 'HEAD'].includes(method);
      if (mutating) checkOrigin(req);
      let session = getSession(req);
      if (pathname === '/api/session' && method === 'GET') return json(res, 200, sessionResponse(session));
      if (pathname === '/api/login' && method === 'POST') {
        const body = await bodyJson(req);
        if (authRequired && !equalSecret(body.token, token)) {
          throw new HttpError(401, 'invalid_token', 'The access token is incorrect.');
        }
        if (authRequired) {
          for (const [id, old] of sessions) if (Date.now() - old.created > SESSION_AGE) sessions.delete(id);
          const id = randomBytes(32).toString('hex');
          session = { csrf: randomBytes(32).toString('hex'), created: Date.now(), id };
          sessions.set(id, session);
          res.setHeader('Set-Cookie', `${SESSION_COOKIE}=${id}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${SESSION_AGE / 1000}${secureCookie(req)}`);
        }
        return json(res, 200, sessionResponse(session));
      }
      if (pathname.startsWith('/api/') || pathname.startsWith('/media/')) {
        if (!session) throw new HttpError(401, 'authentication_required', 'Please sign in.');
        if (mutating && !equalSecret(req.headers['x-csrf-token'], session.csrf)) {
          throw new HttpError(403, 'csrf_rejected', 'Refresh the page before saving changes.');
        }
      }
      if (pathname === '/api/logout' && method === 'POST') {
        if (session.id) sessions.delete(session.id);
        res.setHeader('Set-Cookie', `${SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0${secureCookie(req)}`);
        return json(res, 200, { ok: true });
      }
      const search = url.searchParams;
      if (pathname === '/api/accounts' && method === 'GET') return json(res, 200, { items: store.listAccounts() });
      if (pathname === '/api/conversations' && method === 'GET') {
        return json(res, 200, store.listConversations(required(search, 'account'), queryOptions(search)));
      }
      if (pathname === '/api/messages' && method === 'GET') {
        return json(res, 200, store.listMessages(required(search, 'account'), {
          ...queryOptions(search), conversationId: search.get('conversation') || '',
          date: search.get('date') || '' }));
      }
      if (pathname === '/api/calendar' && method === 'GET') {
        return json(res, 200, store.getCalendar(required(search, 'account'), {
          ...queryOptions(search), month: required(search, 'month'),
          conversationId: search.get('conversation') || '' }));
      }
      if (pathname === '/api/message/restore' && method === 'POST') {
        const body = await bodyJson(req);
        return json(res, 200, { item: store.restoreMessage(required(search, 'account'),
          required(search, 'uuid'), body.expected_revision) });
      }
      if (pathname === '/api/messages/delete' && method === 'POST') {
        const body = deletionConfirmed(await bodyJson(req));
        return json(res, 200, store.deleteMessages(required(search, 'account'), body.messages));
      }
      if (pathname === '/api/conversations/delete' && method === 'POST') {
        const body = deletionConfirmed(await bodyJson(req));
        return json(res, 200, store.deleteConversations(required(search, 'account'), body.conversations));
      }
      if (pathname === '/api/conversation' && method === 'DELETE') {
        const body = deletionConfirmed(await bodyJson(req));
        return json(res, 200, store.deleteConversation(required(search, 'account'),
          required(search, 'conversation'), body.expected_message_count));
      }
      if (pathname === '/api/message') {
        const uid = required(search, 'account');
        const uuid = required(search, 'uuid');
        if (method === 'GET') return json(res, 200, { item: store.getMessage(uid, uuid) });
        if (method === 'PATCH') return json(res, 200, { item: store.updateMessage(uid, uuid, await bodyJson(req)) });
        if (method === 'DELETE') {
          const body = deletionConfirmed(await bodyJson(req));
          return json(res, 200, store.deleteMessage(uid, uuid, body.expected_revision));
        }
      }
      if (pathname === '/api/decorations' && method === 'GET') {
        const decorationPath = path.join(output, 'decoration_manifest.json');
        const data = await fs.readFile(decorationPath, 'utf8').catch(error => {
          if (error.code === 'ENOENT') return null;
          throw error;
        });
        return json(res, 200, data ? JSON.parse(data) : { bubbles: {}, inline_emoji: {} });
      }
      if (pathname.startsWith('/media/') && ['GET', 'HEAD'].includes(method)) {
        return await serveMedia(req, res, output, pathname.slice('/media/'.length));
      }
      if (pathname.startsWith('/api/') || pathname.startsWith('/media/')) {
        throw new HttpError(404, 'not_found', 'Endpoint not found.');
      }
      if (!['GET', 'HEAD'].includes(method)) throw new HttpError(405, 'method_not_allowed', 'Method not allowed.');
      await serveStatic(req, res, path.resolve(distDir), pathname);
    } catch (error) {
      if (res.headersSent) { res.destroy(); return; }
      const status = Number(error.status) || 500;
      const safeStatus = status >= 400 && status <= 599 ? status : 500;
      const message = safeStatus === 500 ? 'The request could not be completed.' : error.message;
      json(res, safeStatus, { error: { code: error.code || 'internal_error', message } });
    }
  }
  server.requestTimeout = 30_000;
  server.headersTimeout = 15_000;
  let closed = false;
  let closePromise;
  function closeStore() { if (!closed) { closed = true; sessions.clear(); store.close(); } }
  server.once('close', closeStore);
  return { server, store, initialization, async close() {
    if (closePromise) return closePromise;
    if (!server.listening) { closeStore(); return; }
    closePromise = (async () => {
      // A paused audio/video client can otherwise keep server.close pending
      // indefinitely and leave the SQLite file locked on Windows.
      const deadline = setTimeout(() => server.closeAllConnections(), 5000);
      try {
        await new Promise((resolve, reject) => {
          server.close(error => error ? reject(error) : resolve());
          server.closeIdleConnections();
        });
        // Socket closure precedes asynchronous file-stream cleanup on Windows.
        // Wait for handlers to release media handles before reporting shutdown.
        await Promise.allSettled([...pendingRequests]);
      } finally { clearTimeout(deadline); closeStore(); }
    })();
    return closePromise;
  } };
}

export async function startServer(options = {}) {
  const app = await createApp(options);
  const host = options.host || '127.0.0.1';
  const port = options.port ?? 8765;
  try {
    await new Promise((resolve, reject) => { app.server.once('error', reject); app.server.listen(port, host, resolve); });
  } catch (error) { await app.close(); throw error; }
  const address = app.server.address();
  const displayHost = host === '0.0.0.0' ? '127.0.0.1' : host === '::' ? '[::1]' : host.includes(':') ? `[${host}]` : host;
  return { ...app, address, url: `http://${displayHost}:${address.port}/` };
}

async function main() {
  const { values } = parseArgs({ options: {
    data: { type: 'string' }, database: { type: 'string' }, host: { type: 'string', default: '127.0.0.1' },
    port: { type: 'string', default: '8765' }, token: { type: 'string' },
    reindex: { type: 'boolean', default: false }, help: { type: 'boolean', short: 'h' },
  } });
  if (values.help) {
    console.log('Usage: npm start -- --data OUTPUT [--host 127.0.0.1] [--port 8765] [--token TOKEN] [--database PATH] [--reindex]');
    console.log('Web runtime: Vue 3 + Node.js + SQLite. DUOSHAN_ACCESS_TOKEN may supply the token.');
    return;
  }
  if (!/^\d+$/.test(values.port) || Number(values.port) > 65535) throw new Error('Port must be an integer between 0 and 65535.');
  const app = await startServer({ dataDir: values.data, databasePath: values.database,
    host: values.host, port: Number(values.port), token: values.token ?? process.env.DUOSHAN_ACCESS_TOKEN ?? '',
    reindex: values.reindex });
  console.log(`Reader ready: ${app.url}`);
  console.log(`Data: ${path.resolve(values.data)}`);
  console.log(`Database: ${app.store.databasePath}`);
  console.log(`Frontend: ${DEFAULT_DIST}`);
  console.log(`Backend: ${fileURLToPath(import.meta.url)}`);
  console.log(`Media: ${path.join(path.resolve(values.data), 'attachments')}`);
  console.log('Edits and permanent deletions apply to the reader database. Original export files are preserved.');
  let shuttingDown = false;
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => {
    if (shuttingDown) {
      app.server.closeAllConnections();
      process.exit(signal === 'SIGINT' ? 130 : 143);
    }
    shuttingDown = true;
    try { await app.close(); }
    catch (error) { console.error(`Shutdown error: ${error.message}`); process.exitCode = 1; }
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => { console.error(`Error: ${error.message}`); process.exitCode = 1; });
}
