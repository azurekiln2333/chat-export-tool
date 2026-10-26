/**
 * Shared helpers for inspecting the reader SQLite database directly.
 *
 * These tests deliberately read the database with a separate `node:sqlite`
 * connection: proving a row is really gone means looking at storage, not only
 * at the public API. Every helper here is independent of the store's own code.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { makeExport } from './fixture.mjs';

/** Construct the store lazily so suites can report a precise missing export. */
export async function loadStore() {
  const module = await import('../../server/store.mjs');
  if (typeof module.ChatStore !== 'function') {
    throw new Error('web/server/store.mjs must export ChatStore');
  }
  return module;
}

/** Fail loudly (never skip) when a required method has not been delivered. */
export function requireMethods(ChatStore, names) {
  const missing = names.filter((name) => typeof ChatStore.prototype[name] !== 'function');
  if (missing.length) {
    throw new Error(`ChatStore is missing required method(s): ${missing.join(', ')}`);
  }
}

/** Open the raw reader database for direct SQL inspection. */
export function openDatabase(dir) {
  return new DatabaseSync(path.join(dir, 'reader.sqlite'));
}

/** Run a possibly-synchronous store call and capture its value or error. */
export async function capture(fn) {
  try {
    return { ok: true, value: await fn() };
  } catch (error) {
    return { ok: false, error };
  }
}

const KNOWN_CODES = {
  400: ['invalid_argument', 'invalid_batch'],
  404: ['not_found'],
  409: ['conflict'],
};

/**
 * Assert a captured failure carries the expected HTTP status. The machine code
 * is checked against the codes the store defines for that status, so a wrong
 * classification still fails.
 */
export function expectStatus(outcome, status, label) {
  assert.equal(outcome.ok, false, `${label}: expected a failure, got a value`);
  const error = outcome.error;
  assert.equal(error.name, 'StoreError', `${label}: expected StoreError, got ${error.name}`);
  assert.equal(error.status, status, `${label}: expected status ${status}, got ${error.status}`);
  assert.equal(typeof error.code, 'string', `${label}: error.code must be a string`);
  if (KNOWN_CODES[status]) {
    assert.ok(KNOWN_CODES[status].includes(error.code),
      `${label}: unexpected code ${error.code} for status ${status}`);
  }
  assert.ok(!/SELECT|INSERT|UPDATE |DELETE FROM|DROP |sqlite/i.test(error.message),
    `${label}: the error message leaked SQL internals`);
}

/** Names of the user tables in the database, excluding SQLite internals. */
export function tableNames(db) {
  return db.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    .all().map((row) => String(row.name));
}

export function columnNames(db, table) {
  return db.prepare(`PRAGMA table_info("${table}")`).all().map((row) => String(row.name));
}

export function countRows(db, table, where = '', params = []) {
  const row = db.prepare(`SELECT COUNT(*) AS total FROM "${table}" ${where}`).get(...params);
  return Number(row.total);
}

/**
 * Search every user table for a literal string. Used to prove that deleted
 * content is not retained anywhere, including a hidden recycle-bin table.
 */
export function scanForValue(db, needle) {
  const hits = [];
  for (const table of tableNames(db)) {
    let rows;
    try {
      rows = db.prepare(`SELECT * FROM "${table}"`).all();
    } catch {
      continue;
    }
    for (const row of rows) {
      let text;
      try {
        text = JSON.stringify(row);
      } catch {
        text = '';
      }
      if (text && text.includes(needle)) hits.push({ table, row });
    }
  }
  return hits;
}

/** Exact raw JSONL lines of an account, in file order. */
export function sourceLines(fixture, uid) {
  return fs.readFileSync(path.join(fixture.dir, uid, 'messages.jsonl'), 'utf8')
    .split('\n').filter((line) => line.length > 0);
}

/**
 * Open a store on a fresh fixture and keep a single teardown hook that closes
 * every store before the directory is removed (Windows keeps handles open).
 * `open()`/`reopen()` support durability tests.
 */
export async function openStoreSet(t, fixtureOptions = {}) {
  const ChatStore = (await loadStore()).ChatStore;
  const fixture = makeExport(fixtureOptions);
  const live = [];
  const databases = [];

  // Raw connections are tracked here so the single teardown hook can close the
  // SQLite handles before the directory is removed (Windows file locks).
  const raw = () => {
    const database = openDatabase(fixture.dir);
    databases.push(database);
    return database;
  };

  t.after(() => {
    for (const database of databases) {
      try {
        database.close();
      } catch {
        /* already closed */
      }
    }
    for (const item of live) {
      try {
        item.close();
      } catch {
        /* already closed */
      }
    }
    fixture.cleanup();
  });

  const open = async (reindex = false) => {
    const store = new ChatStore(fixture.dir);
    live.push(store);
    await store.initialize({ force: reindex });
    return store;
  };

  const closeCurrent = () => {
    const last = live[live.length - 1];
    if (last) {
      try {
        last.close();
      } catch {
        /* already closed */
      }
    }
  };

  const store = await open();
  return { ChatStore, fixture, store, open, closeCurrent, raw };
}