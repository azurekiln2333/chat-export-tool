/**
 * SQLite index, query and edit layer for the Duoshan chat reader.
 *
 * The exporter writes a portable, self-contained export tree:
 *
 *     <output>/<uid>/messages.jsonl          one JSON object per line
 *     <output>/<uid>/statistics.json         account summary (nickname, counts)
 *     <output>/<uid>/contacts.json           cached contact rows
 *     <output>/<uid>/chat.sqlite             plaintext copy of the account DB
 *     <output>/attachments/...              attachments served by the server
 *
 * This module turns that tree into a single queryable database,
 * `<output>/reader.sqlite`, without ever touching the exported files or the
 * original per-account databases. It also owns the user's annotations (text
 * overrides, notes, hidden flags) so a reindex never loses them.
 *
 * Design rules that the rest of the reader relies on:
 *
 *   - `raw_json` is the exact JSONL line as written by the exporter. It is
 *     stored verbatim and returned as a string, never re-serialised.
 *   - Every identifier (account uid, msg_uuid, conversation id, sender) is
 *     stored as TEXT so 19-digit ids survive the round trip untouched.
 *   - `text_original` is what the exporter rendered; `text` is the effective
 *     text (`text_override` when the user edited the row, else the original).
 *     Search and display use the effective text.
 *   - Reindexing is a single transaction. Annotations live in their own table
 *     and are never deleted, so edits survive a reimport and revision numbers
 *     keep increasing (no ABA).
 *
 * JSON parsing uses `json-bigint` with `storeAsString`, so integers that a
 * plain `JSON.parse` would round become lossless strings instead.
 */

import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { DatabaseSync } from 'node:sqlite';
import JSONbigFactory from 'json-bigint';

const JSONbig = JSONbigFactory({ storeAsString: true, useNativeBigInt: false });

/** Bump when the on-disk schema or the import mapping changes. */
const FORMAT_VERSION = '2';

/** Store file name created inside the export root. */
const DATABASE_FILE = 'reader.sqlite';

/** Per-account files imported into the index. */
const MESSAGES_FILE = 'messages.jsonl';
const STATISTICS_FILE = 'statistics.json';
const CONTACTS_FILE = 'contacts.json';

/** Hard bounds for pagination (see web-contract.md). */
const DEFAULT_LIMIT = 100;
const MAX_LIMIT = 500;

/** Upper bound for a single user annotation, to keep the index healthy. */
const MAX_TEXT_LENGTH = 100_000;

/** An account directory is a plain decimal uid. */
const UID_PATTERN = /^[0-9]+$/;

const SCHEMA = `
CREATE TABLE IF NOT EXISTS store_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
  uid  TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS conversations (
  uid             TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  name            TEXT NOT NULL DEFAULT '',
  name_lower      TEXT NOT NULL DEFAULT '',
  message_count   INTEGER NOT NULL DEFAULT 0,
  first_time      INTEGER,
  last_time       INTEGER,
  PRIMARY KEY (uid, conversation_id)
);

CREATE TABLE IF NOT EXISTS messages (
  uid               TEXT NOT NULL,
  msg_uuid          TEXT NOT NULL,
  conversation_id   TEXT NOT NULL DEFAULT '',
  conversation_name TEXT NOT NULL DEFAULT '',
  sender            TEXT NOT NULL DEFAULT '',
  sender_name       TEXT NOT NULL DEFAULT '',
  created_time      INTEGER,
  time              TEXT NOT NULL DEFAULT '',
  message_type      INTEGER,
  type_name         TEXT NOT NULL DEFAULT '',
  text_original     TEXT NOT NULL DEFAULT '',
  text_lower        TEXT NOT NULL DEFAULT '',
  raw_json          TEXT NOT NULL,
  meta_json         TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (uid, msg_uuid)
);

CREATE TABLE IF NOT EXISTS message_edits (
  uid           TEXT NOT NULL,
  msg_uuid      TEXT NOT NULL,
  text_override TEXT,
  note          TEXT,
  hidden        INTEGER NOT NULL DEFAULT 0,
  revision      INTEGER NOT NULL DEFAULT 0,
  updated_at    TEXT,
  PRIMARY KEY (uid, msg_uuid)
);

CREATE INDEX IF NOT EXISTS idx_messages_uid_time
  ON messages (uid, created_time, msg_uuid);
CREATE INDEX IF NOT EXISTS idx_messages_uid_conversation
  ON messages (uid, conversation_id, created_time, msg_uuid);
CREATE INDEX IF NOT EXISTS idx_messages_uid_sender
  ON messages (uid, sender);
-- Covering index so substring search walks only the lowercased text instead of
-- the whole table (each row also carries a large raw_json/meta_json payload).
CREATE INDEX IF NOT EXISTS idx_messages_search
  ON messages (uid, conversation_id, text_lower, msg_uuid);
CREATE INDEX IF NOT EXISTS idx_conversations_uid_last
  ON conversations (uid, last_time);
`;

/**
 * Error type shared with the HTTP layer: `code` is a stable machine string and
 * `status` is the HTTP status the server should answer with.
 */
export class StoreError extends Error {
  /**
   * @param {string} code stable machine-readable code
   * @param {string} message human readable message (no secrets, no SQL)
   * @param {number} [status] HTTP status code to surface
   * @param {{cause?: unknown}} [options]
   */
  constructor(code, message, status = 500, options = undefined) {
    super(message, options);
    this.name = 'StoreError';
    this.code = code;
    this.status = status;
  }
}

/** Build an HTTP status helper so every throw site stays consistent. */
function badArgument(message) {
  return new StoreError('invalid_argument', message, 400);
}

function parseJsonLossless(text) {
  try {
    return JSONbig.parse(text);
  } catch {
    return null;
  }
}

function stringifyJson(value) {
  try {
    return JSON.stringify(value) ?? '{}';
  } catch {
    return '{}';
  }
}

function asText(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  return String(value);
}

/** Integers (timestamps, type codes) are always safe to read as numbers. */
function asInteger(value) {
  if (value === null || value === undefined) return null;
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? Math.trunc(number) : null;
}

function clampLimit(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return DEFAULT_LIMIT;
  return Math.min(MAX_LIMIT, Math.max(1, Math.trunc(number)));
}

function clampOffset(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return 0;
  return Math.trunc(number);
}

function normalizeFlag(value) {
  return value === true || value === 1 || value === '1' || value === 'true';
}

/** Reject uid values that could escape the export tree. */
function assertUid(uid) {
  const text = asText(uid);
  if (!UID_PATTERN.test(text)) {
    throw new StoreError('invalid_account', 'Account uid must be a decimal string.', 400);
  }
  return text;
}

function assertKey(value, label) {
  const text = asText(value);
  if (!text) {
    throw new StoreError('invalid_argument', label + ' is required.', 400);
  }
  if (text.includes('\u0000')) {
    throw new StoreError('invalid_argument', label + ' contains an invalid character.', 400);
  }
  return text;
}

async function hashFile(file) {
  const hash = createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) {
    hash.update(chunk);
  }
  return hash.digest('hex');
}

function readJsonFile(file) {
  try {
    return parseJsonLossless(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

async function* readJsonLines(file) {
  const stream = fs.createReadStream(file, { encoding: 'utf8' });
  const lines = readline.createInterface({ input: stream, crlfDelay: Infinity });
  try {
    for await (const line of lines) {
      const text = line.replace(/^\uFEFF/, '').trim();
      if (text) yield text;
    }
  } finally {
    lines.close();
    stream.destroy();
  }
}

/** Normalise one imported JSONL row into flat columns plus its metadata. */
function normalizeRow(line) {
  const row = parseJsonLossless(line);
  if (!row || typeof row !== 'object' || Array.isArray(row)) {
    throw new StoreError('invalid_source', MESSAGES_FILE + ' contains a line that is not a JSON object.', 500);
  }
  const meta = row._export && typeof row._export === 'object' && !Array.isArray(row._export)
    ? row._export
    : {};
  const text = typeof meta.text === 'string' ? meta.text : '';
  return {
    msg_uuid: asText(row.msg_uuid),
    conversation_id: asText(row.conversation_id),
    conversation_name: asText(meta.conversation_name),
    sender: asText(row.sender),
    sender_name: asText(meta.sender_name),
    created_time: asInteger(row.created_time),
    time: asText(meta.time),
    message_type: asInteger(row.type),
    type_name: asText(meta.type_name),
    text_original: text,
    text_lower: text.toLowerCase(),
    meta_json: stringifyJson(meta),
  };
}

/** Resolve the display name for an account from statistics or contacts. */
function accountName(uid, statistics, contacts) {
  const nickname = statistics && typeof statistics === 'object'
    ? asText(statistics.nickname)
    : '';
  if (nickname) return nickname;
  if (Array.isArray(contacts)) {
    for (const contact of contacts) {
      if (!contact || typeof contact !== 'object') continue;
      if (asText(contact.uid) !== uid) continue;
      const name = asText(contact.remark_name) || asText(contact.nickname)
        || asText(contact.display_name);
      if (name) return name;
    }
  }
  return '';
}

/** Map a joined `messages` + `message_edits` row into the public message shape. */
function mapMessage(row) {
  const textOverride = row.text_override === undefined ? null : row.text_override;
  const note = row.note === undefined ? null : row.note;
  const revision = asInteger(row.revision) ?? 0;
  const meta = row.meta_json ? parseJsonLossless(row.meta_json) : null;
  return {
    account_uid: row.uid,
    msg_uuid: row.msg_uuid,
    conversation_id: row.conversation_id,
    conversation_name: row.conversation_name,
    sender: row.sender,
    sender_name: row.sender_name,
    created_time: row.created_time,
    time: row.time,
    message_type: row.message_type,
    type_name: row.type_name,
    text_original: row.text_original,
    text: textOverride ?? row.text_original,
    raw_json: row.raw_json,
    meta: meta && typeof meta === 'object' && !Array.isArray(meta) ? meta : {},
    edit: {
      text_override: textOverride,
      note,
      hidden: normalizeFlag(row.hidden),
      revision,
      updated_at: row.updated_at ?? null,
    },
  };
}

/** Columns shared by every message read, including the annotation join. */
const MESSAGE_COLUMNS = `
  m.uid, m.msg_uuid, m.conversation_id, m.conversation_name, m.sender,
  m.sender_name, m.created_time, m.time, m.message_type, m.type_name,
  m.text_original, m.raw_json, m.meta_json,
  e.text_override, e.note, e.hidden, e.revision, e.updated_at`;

const MESSAGE_JOIN = `
  FROM messages m
  LEFT JOIN message_edits e
    ON e.uid = m.uid AND e.msg_uuid = m.msg_uuid`;

/**
 * Effective text and note expressions used by every search filter.
 *
 * `text_lower` is precomputed at import because SQLite's `lower()` only folds
 * ASCII, character by character, over every row. Filtering with `instr()` on a
 * lowercased column keeps Chinese search intact (no tokenisation) while making
 * substring search fast, and `String#toLowerCase` handles non-ASCII scripts.
 * The `CASE` only calls `lower()` for the rare rows that carry an override.
 */
const SEARCH_TEXT = `CASE WHEN e.text_override IS NULL THEN m.text_lower
                         ELSE lower(e.text_override) END`;
const SEARCH_NOTE = `CASE WHEN e.note IS NULL THEN '' ELSE lower(e.note) END`;
const SEARCH_MATCH = `(instr(${SEARCH_TEXT}, ?) > 0 OR instr(${SEARCH_NOTE}, ?) > 0)`;

export class ChatStore {
  #outputDir;
  #databasePath;
  #db;
  #closed = false;

  /**
   * @param {string} outputDir export root (contains `<uid>/` directories)
   * @param {{databasePath?: string}} [options]
   */
  constructor(outputDir, options = {}) {
    if (typeof outputDir !== 'string' || !outputDir.trim()) {
      throw badArgument('outputDir is required.');
    }
    this.#outputDir = path.resolve(outputDir);
    if (!fs.existsSync(this.#outputDir) || !fs.statSync(this.#outputDir).isDirectory()) {
      throw new StoreError('invalid_argument', 'Export directory not found: ' + this.#outputDir, 400);
    }
    const override = typeof options.databasePath === 'string' && options.databasePath.trim()
      ? options.databasePath
      : '';
    this.#databasePath = override
      ? (override === ':memory:' ? override : path.resolve(override))
      : path.join(this.#outputDir, DATABASE_FILE);
    if (this.#databasePath !== ':memory:') {
      fs.mkdirSync(path.dirname(this.#databasePath), { recursive: true });
    }
    this.#db = new DatabaseSync(this.#databasePath);
    this.#db.exec('PRAGMA busy_timeout = 5000');
    if (this.#databasePath !== ':memory:') {
      try {
        this.#db.exec('PRAGMA journal_mode = WAL');
      } catch {
        /* WAL is an optimisation; a read-only location still works. */
      }
    }
    this.#migrate();
  }

  /** Absolute export root this store serves. */
  get outputDir() {
    return this.#outputDir;
  }

  /** Absolute path of the reader index. */
  get databasePath() {
    return this.#databasePath;
  }

  /**
   * Build or refresh the index and report what it now holds.
   *
   * When the export is unchanged the import is skipped; when the JSONL files
   * are gone the existing index is served as-is. Both cases keep annotations.
   *
   * @param {{force?: boolean}} [options]
   * @returns {Promise<{accounts:number, conversations:number, messages:number,
   *                    imported:boolean, fingerprint:string|null}>}
   */
  async initialize(options = {}) {
    this.#assertOpen();
    const force = normalizeFlag(options.force);
    const importable = this.#importableAccounts();
    const counts = this.#counts();
    if (!importable.length) {
      // No JSONL at all: serve the existing index untouched. This is the
      // normal case for a relocated/portable export whose sources were pruned.
      return { ...counts, imported: false, fingerprint: this.#getMeta('fingerprint') };
    }
    const fingerprint = await this.#fingerprint(importable);
    const stored = this.#getMeta('fingerprint');
    if (!force && stored && stored === fingerprint && counts.messages > 0) {
      return { ...counts, imported: false, fingerprint };
    }
    await this.#importAll(importable, fingerprint);
    return { ...this.#counts(), imported: true, fingerprint };
  }

  /** @returns {Array<{uid:string,name:string,message_count:number,conversation_count:number}>} */
  listAccounts() {
    this.#assertOpen();
    const rows = this.#guard(() => this.#db.prepare(`
      SELECT a.uid AS uid,
             a.name AS name,
             (SELECT COUNT(*) FROM messages m WHERE m.uid = a.uid) AS message_count,
             (SELECT COUNT(*) FROM conversations c WHERE c.uid = a.uid) AS conversation_count
      FROM accounts a
      ORDER BY a.uid ASC`).all());
    return rows.map((row) => ({
      uid: row.uid,
      name: row.name || row.uid,
      message_count: asInteger(row.message_count) ?? 0,
      conversation_count: asInteger(row.conversation_count) ?? 0,
    }));
  }

  /**
   * Conversations of one account, filtered by name or message text.
   *
   * @param {string} uid
   * @param {{q?:string, limit?:number, offset?:number, includeHidden?:boolean}} [options]
   * @returns {{items:Array<{id:string,name:string,message_count:number,
   *          first_time:number|null,last_time:number|null}>, total:number}}
   */
  listConversations(uid, options = {}) {
    this.#assertOpen();
    const account = assertUid(uid);
    const query = asText(options.q).trim().toLowerCase();
    const includeHidden = normalizeFlag(options.includeHidden);
    const limit = clampLimit(options.limit);
    const offset = clampOffset(options.offset);

    const params = [account];
    const where = ['c.uid = ?'];

    if (!includeHidden) {
      where.push(`EXISTS (SELECT 1 FROM messages m
        LEFT JOIN message_edits e ON e.uid = m.uid AND e.msg_uuid = m.msg_uuid
        WHERE m.uid = c.uid AND m.conversation_id = c.conversation_id
          AND COALESCE(e.hidden, 0) = 0)`);
    }
    if (query) {
      // Resolve matching conversations with one scan instead of re-scanning the
      // messages table once per conversation.
      const ids = new Set();
      const hiddenOnly = includeHidden ? '' : 'AND COALESCE(e.hidden, 0) = 0';
      const nameRows = this.#guard(() => this.#db.prepare(
        'SELECT conversation_id AS id FROM conversations WHERE uid = ? AND instr(name_lower, ?) > 0',
      ).all(account, query));
      for (const row of nameRows) ids.add(row.id);
      const textRows = this.#guard(() => this.#db.prepare(`
        SELECT DISTINCT m.conversation_id AS id ${MESSAGE_JOIN}
        WHERE m.uid = ? ${hiddenOnly} AND ${SEARCH_MATCH}`).all(account, query, query));
      for (const row of textRows) ids.add(row.id);
      if (!ids.size) return { items: [], total: 0 };
      where.push(`c.conversation_id IN (${Array(ids.size).fill('?').join(', ')})`);
      for (const id of ids) params.push(id);
    }

    const filter = where.join(' AND ');
    const total = this.#guard(() => this.#db.prepare(
      'SELECT COUNT(*) AS total FROM conversations c WHERE ' + filter,
    ).get(...params));
    const rows = this.#guard(() => this.#db.prepare(`
      SELECT c.conversation_id AS id, c.name AS name,
             c.message_count AS message_count,
             c.first_time AS first_time, c.last_time AS last_time
      FROM conversations c
      WHERE ${filter}
      ORDER BY c.last_time DESC, c.conversation_id ASC
      LIMIT ? OFFSET ?`).all(...params, limit, offset));

    return {
      items: rows.map((row) => ({
        id: row.id,
        name: row.name || row.id,
        message_count: asInteger(row.message_count) ?? 0,
        first_time: asInteger(row.first_time),
        last_time: asInteger(row.last_time),
      })),
      total: asInteger(total?.total) ?? 0,
    };
  }

  /**
   * Messages of one account, optionally limited to a conversation, in
   * chronological order (created_time, then msg_uuid).
   *
   * @param {string} uid
   * @param {{conversationId?:string, q?:string, limit?:number, offset?:number,
   *          includeHidden?:boolean}} [options]
   * @returns {{items:object[], total:number, limit:number, offset:number}}
   */
  listMessages(uid, options = {}) {
    this.#assertOpen();
    const account = assertUid(uid);
    const conversationId = asText(options.conversationId).trim();
    const query = asText(options.q).trim().toLowerCase();
    const includeHidden = normalizeFlag(options.includeHidden);
    const limit = clampLimit(options.limit);
    const offset = clampOffset(options.offset);

    const params = [account];
    const where = ['m.uid = ?'];
    if (conversationId) {
      where.push('m.conversation_id = ?');
      params.push(conversationId);
    }
    if (!includeHidden) where.push('COALESCE(e.hidden, 0) = 0');
    if (query) {
      where.push(SEARCH_MATCH);
      params.push(query, query);
    }
    const filter = where.join(' AND ');

    const total = this.#guard(() => this.#db.prepare(
      `SELECT COUNT(*) AS total ${MESSAGE_JOIN} WHERE ${filter}`,
    ).get(...params));
    const rows = this.#guard(() => this.#db.prepare(`
      SELECT ${MESSAGE_COLUMNS} ${MESSAGE_JOIN}
      WHERE ${filter}
      ORDER BY m.created_time ASC, m.msg_uuid ASC
      LIMIT ? OFFSET ?`).all(...params, limit, offset));

    return {
      items: rows.map(mapMessage),
      total: asInteger(total?.total) ?? 0,
      limit,
      offset,
    };
  }

  /**
   * One message with its current annotation.
   *
   * @param {string} uid
   * @param {string} msgUuid
   * @returns {object}
   */
  getMessage(uid, msgUuid) {
    this.#assertOpen();
    const account = assertUid(uid);
    const key = assertKey(msgUuid, 'msg_uuid');
    const row = this.#guard(() => this.#db.prepare(`
      SELECT ${MESSAGE_COLUMNS} ${MESSAGE_JOIN}
      WHERE m.uid = ? AND m.msg_uuid = ?`).get(account, key));
    if (!row) {
      throw new StoreError('not_found', 'Message not found.', 404);
    }
    return mapMessage(row);
  }

  /**
   * Partial update of one message's annotation. Every field is optional;
   * `expected_revision` is mandatory and must match the stored revision.
   *
   * @param {string} uid
   * @param {string} msgUuid
   * @param {{text_override?:string|null, note?:string|null, hidden?:boolean,
   *          expected_revision:number}} patch
   * @returns {object} the updated message
   */
  updateMessage(uid, msgUuid, patch = {}) {
    this.#assertOpen();
    const account = assertUid(uid);
    const key = assertKey(msgUuid, 'msg_uuid');
    const change = this.#readPatch(patch);
    return this.#writeEdit(account, key, change);
  }

  /**
   * Restore a message: clear the text override, note and hidden flag while
   * still advancing the revision, so an old `expected_revision` cannot be
   * replayed (no ABA).
   *
   * @param {string} uid
   * @param {string} msgUuid
   * @param {number} expectedRevision
   * @returns {object} the updated message
   */
  restoreMessage(uid, msgUuid, expectedRevision) {
    this.#assertOpen();
    const account = assertUid(uid);
    const key = assertKey(msgUuid, 'msg_uuid');
    const change = this.#readPatch({
      text_override: null,
      note: null,
      hidden: false,
      expected_revision: expectedRevision,
    });
    return this.#writeEdit(account, key, change);
  }

  /** Close the underlying database handle. Safe to call twice. */
  close() {
    if (this.#closed) return;
    this.#closed = true;
    try {
      this.#db.close();
    } catch {
      /* ignore a handle that is already gone */
    }
  }

  /**
   * Create the schema, replacing index tables written by an older format.
   * Annotations (`message_edits`) are never dropped, so a format bump keeps
   * the user's edits; the next `initialize()` simply reimports the sources.
   */
  #migrate() {
    // Only store_meta may be touched before the format is known: creating the
    // indexes below against an older messages table would fail.
    this.#db.exec('CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT)');
    const stored = this.#getMeta('format_version');
    if (stored && stored !== FORMAT_VERSION) {
      this.#db.exec('DROP TABLE IF EXISTS messages');
      this.#db.exec('DROP TABLE IF EXISTS conversations');
      this.#db.exec('DROP TABLE IF EXISTS accounts');
      this.#db.exec("DELETE FROM store_meta WHERE key IN ('fingerprint', 'imported_at')");
    }
    this.#db.exec(SCHEMA);
    this.#setMeta('format_version', FORMAT_VERSION);
  }

  // ---------------------------------------------------------------- internals

  #assertOpen() {
    if (this.#closed) {
      throw new StoreError('closed', 'The store has been closed.', 500);
    }
  }

  /** Convert unexpected driver failures into StoreError without leaking SQL. */
  #guard(operation) {
    try {
      return operation();
    } catch (error) {
      if (error instanceof StoreError) throw error;
      throw new StoreError('database_error', 'The reader index could not be read.',
        500, { cause: error });
    }
  }

  #setMeta(key, value) {
    this.#guard(() => this.#db.prepare(
      'INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)',
    ).run(key, value === null || value === undefined ? null : String(value)));
  }

  #getMeta(key) {
    const row = this.#guard(() => this.#db.prepare(
      'SELECT value FROM store_meta WHERE key = ?',
    ).get(key));
    return row && row.value !== null && row.value !== undefined
      ? String(row.value) : null;
  }

  #counts() {
    const read = (table) => {
      const row = this.#guard(() => this.#db.prepare(
        `SELECT COUNT(*) AS total FROM ${table}`,
      ).get());
      return asInteger(row?.total) ?? 0;
    };
    return {
      accounts: read('accounts'),
      conversations: read('conversations'),
      messages: read('messages'),
    };
  }

  /** Account directories: `<output>/<decimal uid>/`. */
  #accountDirs() {
    return fs.readdirSync(this.#outputDir, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && UID_PATTERN.test(entry.name))
      .map((entry) => entry.name)
      .sort();
  }

  /**
   * Accounts that actually carry messages. Only these are reimported, so an
   * account whose export was pruned keeps its previously indexed rows instead
   * of silently disappearing from the reader.
   */
  #importableAccounts() {
    return this.#accountDirs().filter((uid) => {
      const file = path.join(this.#outputDir, uid, MESSAGES_FILE);
      return fs.existsSync(file) && fs.statSync(file).isFile();
    });
  }

  /** Importable files, relative to the export root, in a stable order. */
  #sourceFiles(accounts) {
    const files = [];
    for (const uid of accounts) {
      for (const name of [MESSAGES_FILE, STATISTICS_FILE, CONTACTS_FILE]) {
        const file = path.join(this.#outputDir, uid, name);
        if (fs.existsSync(file) && fs.statSync(file).isFile()) {
          files.push(path.join(uid, name).replace(/\\/g, '/'));
        }
      }
    }
    return files;
  }

  /**
   * Content fingerprint over every imported file. Hashing the full bytes (not
   * a size/mtime stamp) keeps "unchanged" trustworthy after copies and
   * rewrites, which is exactly what the portable export needs.
   */
  async #fingerprint(accounts) {
    const hash = createHash('sha256');
    hash.update('format=' + FORMAT_VERSION + '\n');
    for (const relative of this.#sourceFiles(accounts)) {
      const absolute = path.join(this.#outputDir, relative);
      hash.update(relative + '\u0000' + await hashFile(absolute) + '\n');
    }
    return hash.digest('hex');
  }

  /**
   * Rebuild the given accounts from their exported files in one transaction.
   * Accounts that are not listed keep their existing rows and annotations.
   */
  async #importAll(accounts, fingerprint) {
    const insertMessage = this.#db.prepare(`
      INSERT OR REPLACE INTO messages (
        uid, msg_uuid, conversation_id, conversation_name, sender, sender_name,
        created_time, time, message_type, type_name, text_original, text_lower,
        raw_json, meta_json)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
    const insertAccount = this.#db.prepare(
      'INSERT OR REPLACE INTO accounts (uid, name) VALUES (?, ?)');
    const clearMessages = this.#db.prepare('DELETE FROM messages WHERE uid = ?');
    const clearConversations = this.#db.prepare('DELETE FROM conversations WHERE uid = ?');
    const buildConversations = this.#db.prepare(`
      INSERT OR REPLACE INTO conversations (
        uid, conversation_id, name, message_count, first_time, last_time)
      SELECT m.uid,
             m.conversation_id,
             COALESCE((
               SELECT m2.conversation_name FROM messages m2
               WHERE m2.uid = m.uid AND m2.conversation_id = m.conversation_id
                 AND m2.conversation_name <> ''
               ORDER BY m2.created_time DESC, m2.msg_uuid DESC
               LIMIT 1), ''),
             COUNT(*),
             MIN(m.created_time),
             MAX(m.created_time)
      FROM messages m
      WHERE m.uid = ?
      GROUP BY m.uid, m.conversation_id`);
    const nameFromMessages = this.#db.prepare(`
      UPDATE accounts SET name = COALESCE((
        SELECT m.sender_name FROM messages m
        WHERE m.uid = accounts.uid AND m.sender = accounts.uid
          AND m.sender_name <> ''
        ORDER BY m.created_time DESC
        LIMIT 1), name)
      WHERE uid = ? AND name = ''`);
    const setConversationNameLower = this.#db.prepare(
      'UPDATE conversations SET name_lower = ? WHERE uid = ? AND conversation_id = ?');
    const readConversationNames = this.#db.prepare(
      'SELECT conversation_id, name FROM conversations WHERE uid = ?');

    this.#db.exec('BEGIN IMMEDIATE');
    try {
      for (const uid of accounts) {
        const directory = path.join(this.#outputDir, uid);
        const messagesPath = path.join(directory, MESSAGES_FILE);

        // Replace this account's rows; other accounts and all annotations in
        // message_edits are left alone.
        clearMessages.run(uid);
        clearConversations.run(uid);

        const statistics = readJsonFile(path.join(directory, STATISTICS_FILE));
        const contacts = readJsonFile(path.join(directory, CONTACTS_FILE));
        insertAccount.run(uid, accountName(uid, statistics, contacts));

        for await (const line of readJsonLines(messagesPath)) {
          const row = normalizeRow(line);
          if (!row.msg_uuid) continue;
          insertMessage.run(
            uid,
            row.msg_uuid,
            row.conversation_id,
            row.conversation_name,
            row.sender,
            row.sender_name,
            row.created_time,
            row.time,
            row.message_type,
            row.type_name,
            row.text_original,
            row.text_lower,
            line,
            row.meta_json,
          );
        }
      }

      // Conversation aggregates come from the imported rows, so the sidebar
      // always matches what listMessages can return.
      for (const uid of accounts) {
        buildConversations.run(uid);
        // Lowercase in JS so names in any script fold the same way the query
        // does; SQLite's lower() only folds ASCII.
        for (const row of readConversationNames.all(uid)) {
          setConversationNameLower.run(asText(row.name).toLowerCase(), uid, row.conversation_id);
        }
        // Fall back to the sender name the account uses for its own messages
        // when neither statistics nor contacts named it.
        nameFromMessages.run(uid);
      }

      this.#setMeta('fingerprint', fingerprint);
      this.#setMeta('imported_at', new Date().toISOString());
      this.#setMeta('output_dir', this.#outputDir);
      this.#db.exec('COMMIT');
    } catch (error) {
      try {
        this.#db.exec('ROLLBACK');
      } catch {
        /* the transaction may already be gone */
      }
      if (error instanceof StoreError) throw error;
      throw new StoreError('import_failed',
        'The export could not be imported into the reader index.',
        500, { cause: error });
    }
  }

  /** Validate a patch and return only the fields that actually change. */
  #readPatch(patch) {
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)) {
      throw badArgument('A JSON object body is required.');
    }
    const revision = patch.expected_revision;
    if (typeof revision !== 'number' || !Number.isInteger(revision) || revision < 0) {
      throw badArgument('expected_revision must be a non-negative integer.');
    }
    const change = { expected_revision: revision };

    if (Object.prototype.hasOwnProperty.call(patch, 'text_override')) {
      const value = patch.text_override;
      if (value !== null && typeof value !== 'string') {
        throw badArgument('text_override must be a string or null.');
      }
      if (typeof value === 'string' && value.length > MAX_TEXT_LENGTH) {
        throw new StoreError('payload_too_large', 'text_override is too long.', 413);
      }
      change.text_override = value;
    }
    if (Object.prototype.hasOwnProperty.call(patch, 'note')) {
      const value = patch.note;
      if (value !== null && typeof value !== 'string') {
        throw badArgument('note must be a string or null.');
      }
      if (typeof value === 'string' && value.length > MAX_TEXT_LENGTH) {
        throw new StoreError('payload_too_large', 'note is too long.', 413);
      }
      change.note = value;
    }
    if (Object.prototype.hasOwnProperty.call(patch, 'hidden')) {
      if (typeof patch.hidden !== 'boolean') {
        throw badArgument('hidden must be a boolean.');
      }
      change.hidden = patch.hidden;
    }
    if (Object.keys(change).length === 1) {
      throw badArgument('Provide text_override, note or hidden to update.');
    }
    return change;
  }

  /**
   * Apply an annotation change under an optimistic revision check. The read,
   * the check and the write share one transaction so two concurrent editors
   * cannot both win.
   */
  #writeEdit(uid, msgUuid, change) {
    const exists = this.#guard(() => this.#db.prepare(
      'SELECT 1 AS ok FROM messages WHERE uid = ? AND msg_uuid = ?',
    ).get(uid, msgUuid));
    if (!exists) {
      throw new StoreError('not_found', 'Message not found.', 404);
    }

    this.#guard(() => this.#db.exec('BEGIN IMMEDIATE'));
    try {
      const current = this.#guard(() => this.#db.prepare(
        `SELECT text_override, note, hidden, revision
         FROM message_edits WHERE uid = ? AND msg_uuid = ?`,
      ).get(uid, msgUuid)) ?? null;

      const revision = current ? (asInteger(current.revision) ?? 0) : 0;
      if (revision !== change.expected_revision) {
        throw new StoreError('conflict',
          'This message was changed by someone else; reload it and retry.', 409);
      }

      const next = {
        text_override: Object.prototype.hasOwnProperty.call(change, 'text_override')
          ? change.text_override
          : (current ? current.text_override ?? null : null),
        note: Object.prototype.hasOwnProperty.call(change, 'note')
          ? change.note
          : (current ? current.note ?? null : null),
        hidden: Object.prototype.hasOwnProperty.call(change, 'hidden')
          ? (change.hidden ? 1 : 0)
          : (current ? (normalizeFlag(current.hidden) ? 1 : 0) : 0),
      };

      this.#guard(() => this.#db.prepare(`
        INSERT INTO message_edits
          (uid, msg_uuid, text_override, note, hidden, revision, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (uid, msg_uuid) DO UPDATE SET
          text_override = excluded.text_override,
          note = excluded.note,
          hidden = excluded.hidden,
          revision = excluded.revision,
          updated_at = excluded.updated_at`).run(
        uid,
        msgUuid,
        next.text_override,
        next.note,
        next.hidden,
        revision + 1,
        new Date().toISOString(),
      ));

      this.#db.exec('COMMIT');
    } catch (error) {
      try {
        this.#db.exec('ROLLBACK');
      } catch {
        /* the transaction may already be gone */
      }
      throw error;
    }

    return this.getMessage(uid, msgUuid);
  }
}

export default ChatStore;
