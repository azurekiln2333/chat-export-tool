/**
 * Shared HTTP helpers for the web reader tests.
 *
 * Every component exists now, so a missing or incompatible server module is a
 * hard failure rather than a skip: a delivery-order skip would hide a broken
 * build from the suite.
 */
import http from 'node:http';

/**
 * Import web/server/index.mjs and fail loudly when it is missing or does not
 * expose the agreed interface.
 */
export async function loadServerModule() {
  let module;
  try {
    module = await import('../../server/index.mjs');
  } catch (error) {
    throw new Error(`web/server/index.mjs could not be imported: ${error.code || error.message}`);
  }
  if (typeof module.createApp !== 'function') {
    throw new Error('web/server/index.mjs must export createApp');
  }
  return module;
}

/**
 * Start the app on an ephemeral loopback port.
 *
 * Returns the base URL, the port and a `close()` that also drops keep-alive
 * sockets so the caller can always close before deleting a Windows fixture.
 */
export async function startTestApp(options) {
  const module = await loadServerModule();
  const app = await module.createApp({ host: '127.0.0.1', ...options });
  await new Promise((resolve, reject) => {
    app.server.once('error', reject);
    app.server.listen(0, '127.0.0.1', resolve);
  });
  const address = app.server.address();
  const port = typeof address === 'object' && address ? address.port : null;
  if (!port) throw new Error('the test server did not report a port');
  return {
    baseUrl: `http://127.0.0.1:${port}`,
    port,
    app,
    server: app.server,
    // Graceful only: the server bounds active connections itself, and the
    // stalled-media test must exercise that deadline rather than bypass it.
    close: async () => {
      await app.close();
    },
    forceClose: () => {
      if (typeof app.server?.closeAllConnections === 'function') app.server.closeAllConnections();
    },
  };
}

/** A tiny same-origin HTTP client that remembers cookies and the CSRF token. */
export class Client {
  constructor(baseUrl) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.cookies = new Map();
    this.csrf = null;
    this.lastSetCookie = [];
  }

  storeCookies(response) {
    const list = typeof response.headers.getSetCookie === 'function'
      ? response.headers.getSetCookie() : [];
    this.lastSetCookie = list;
    for (const raw of list) {
      const pair = raw.split(';')[0];
      const equals = pair.indexOf('=');
      if (equals > 0) this.cookies.set(pair.slice(0, equals).trim(), pair.slice(equals + 1).trim());
    }
  }

  cookieHeader() {
    return [...this.cookies].map(([name, value]) => `${name}=${value}`).join('; ');
  }

  async request(path, { method = 'GET', body, headers = {}, origin = true, csrf = true, raw = false } = {}) {
    const url = `${this.baseUrl}${path.startsWith('/') ? path : `/${path}`}`;
    const finalHeaders = { accept: 'application/json', ...headers };
    if (this.cookies.size) finalHeaders.cookie = this.cookieHeader();
    if (origin && finalHeaders.origin === undefined) finalHeaders.origin = this.baseUrl;
    if (csrf && this.csrf && !['GET', 'HEAD'].includes(method) && finalHeaders['x-csrf-token'] === undefined) {
      finalHeaders['x-csrf-token'] = this.csrf;
    }
    let payload = body;
    if (body !== undefined && !raw) {
      finalHeaders['content-type'] = finalHeaders['content-type'] || 'application/json';
      payload = JSON.stringify(body);
    }
    const response = await fetch(url, { method, headers: finalHeaders, body: payload, redirect: 'manual' });
    this.storeCookies(response);
    return response;
  }

  async json(path, options) {
    const response = await this.request(path, options);
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    return { response, data, text };
  }

  async session() {
    const { response, data } = await this.json('/api/session');
    if (data && typeof data === 'object' && typeof data.csrf_token === 'string') {
      this.csrf = data.csrf_token;
    }
    return { response, data };
  }

  async login(token) {
    const { response, data } = await this.json('/api/login', {
      method: 'POST', body: { token }, csrf: false,
    });
    if (data && typeof data === 'object' && typeof data.csrf_token === 'string') {
      this.csrf = data.csrf_token;
    }
    return { response, data };
  }

  async logout() {
    const result = await this.json('/api/logout', { method: 'POST', body: {} });
    this.csrf = null;
    this.cookies.clear();
    return result;
  }
}

/**
 * Raw node:http request, needed for Host/Origin policy tests because `Host` is
 * a forbidden header for fetch.
 */
export function rawRequest({ port, path, method = 'GET', headers = {}, body }) {
  return new Promise((resolve, reject) => {
    const request = http.request({ host: '127.0.0.1', port, path, method, headers }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let data = null;
        try {
          data = text ? JSON.parse(text) : null;
        } catch {
          data = text;
        }
        resolve({ status: response.statusCode, headers: response.headers, text, data });
      });
    });
    request.on('error', reject);
    if (body !== undefined) request.write(typeof body === 'string' ? body : JSON.stringify(body));
    request.end();
  });
}

export function errorShape(data) {
  return Boolean(data && typeof data === 'object' && data.error
    && typeof data.error.code === 'string' && typeof data.error.message === 'string');
}

export function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Open a media response and deliberately stop reading it, so the server's
 * `pipe` backpressures and the connection stays active. Used to prove that
 * `close()` is bounded instead of waiting on a paused audio/video client.
 */
export function openStalledMedia({ port, path, cookie, host }) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      host: '127.0.0.1',
      port,
      path,
      method: 'GET',
      headers: {
        host: host || `127.0.0.1:${port}`,
        accept: '*/*',
        ...(cookie ? { cookie } : {}),
      },
    });
    request.once('error', reject);
    request.once('response', (response) => {
      request.removeListener('error', reject);
      response.pause();
      const closed = new Promise((done) => {
        response.once('close', () => done('close'));
        response.once('aborted', () => done('aborted'));
        request.once('error', () => done('error'));
      });
      resolve({ request, response, closed });
    });
    request.end();
  });
}

/** Collect a response body and its headers for range assertions. */
export async function bodyOf(response) {
  const buffer = Buffer.from(await response.arrayBuffer());
  return {
    status: response.status,
    headers: Object.fromEntries(response.headers.entries()),
    buffer,
  };
}

export { http };