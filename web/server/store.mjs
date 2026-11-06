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
 *   - Permanent deletion removes message rows, their annotations and the derived
 *     aggregates, and records only a SHA-256 key digest in `deletions`. The
 *     ledger makes the removal durable across reindex, restart and an
 *     index-only (JSONL pruned) migration without keeping any deleted content
 *     or name. Freed pages are overwritten on a best-effort basis
 *     (`secure_delete` + WAL truncation); that is not a forensic guarantee, and
 *     the exported originals and shared media are never modified.
 *   - `messages.day` indexes the exporter's local calendar day so day filters
 *     and the calendar never depend on the viewer's timezone. The export
 *     timezone is persisted in `store_meta` so a copied database keeps its
 *     calendar zone even when `export_summary.json` is not alongside it.
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
import { accountDirectories, accountDirectory } from './account-layout.mjs';

const JSONbig = JSONbigFactory({ storeAsString: true, useNativeBigInt: false });

/** Bump when the on-disk schema or the import mapping changes. */
const FORMAT_VERSION = '3';

/** Store file name created inside the export root. */
const DATABASE_FILE = 'reader.sqlite';

/** Per-account files imported into the index. */
const MESSAGES_FILE = 'messages.jsonl';
const STATISTICS_FILE = 'statistics.json';
const CONTACTS_FILE = 'contacts.json';

/** Root file holding the export timezone used for calendar days. */
const SUMMARY_FILE = 'export_summary.json';

/** Hard bounds for pagination (see web-contract.md). */
const DEFAULT_LIMIT = 100;
const MAX_LIMIT = 500;

/** Hard bound for a single permanent-deletion request (design.md). */
const MAX_BATCH = 500;

/** Upper bound for a single user annotation, to keep the index healthy. */
const MAX_TEXT_LENGTH = 100_000;

/** An account directory is a plain decimal uid (Duoshan) or a lowercase hex
 * uid (Xiaohongshu, e.g. 68cf0e5d000000002102b928). Both are safe path
 * components; anything else is rejected. */
const UID_PATTERN = /^[0-9]+$/;
const XHS_UID_PATTERN = /^[0-9a-f]{16,64}$/;

/**
 * Tables only. Created before any index so an older database can gain missing
 * columns (see `#addMissingColumns`) without dropping the rows it already has.
 */
const TABLES_SCHEMA = `
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
  day               TEXT NOT NULL DEFAULT '',
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

-- Durable record of permanent deletions. Only a domain-separated SHA-256 of
-- (uid, key) is kept: nothing about the deleted content, its text or the
-- conversation name can be recovered here. A reimport consults it so removed
-- rows are never resurrected from the still-untouched source files.
CREATE TABLE IF NOT EXISTS deletions (
  kind       TEXT NOT NULL,
  key_hash   TEXT NOT NULL,
  deleted_at TEXT NOT NULL,
  PRIMARY KEY (kind, key_hash)
);

CREATE TABLE IF NOT EXISTS library_accounts (
  uid TEXT PRIMARY KEY,
  app TEXT NOT NULL,
  directory TEXT NOT NULL
);
`;

const INDEXES_SCHEMA = `
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
-- Day filter and calendar grouping, per account and per conversation.
CREATE INDEX IF NOT EXISTS idx_messages_uid_day
  ON messages (uid, day, created_time, msg_uuid);
CREATE INDEX IF NOT EXISTS idx_messages_uid_conversation_day
  ON messages (uid, conversation_id, day, created_time, msg_uuid);
CREATE INDEX IF NOT EXISTS idx_conversations_uid_last
  ON conversations (uid, last_time);
`;

/** Columns added after the first release, applied additively on upgrade. */
const MESSAGE_COLUMN_ADDITIONS = [
  ['text_lower', "TEXT NOT NULL DEFAULT ''"],
  ['day', "TEXT NOT NULL DEFAULT ''"],
];
const CONVERSATION_COLUMN_ADDITIONS = [
  ['name_lower', "TEXT NOT NULL DEFAULT ''"],
];

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
  if (!UID_PATTERN.test(text) && !XHS_UID_PATTERN.test(text)) {
    throw new StoreError('invalid_account', 'Account uid must be a decimal or lowercase-hex string.', 400);
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

/** Days per month (February handled by `isLeapYear`). */
const MONTH_DAYS = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

function isLeapYear(year) {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

/**
 * Real calendar-day check (leap years included) without allocating a Date,
 * used for validation and for the import/backfill pass.
 */
function isRealDay(text) {
  if (typeof text !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(text)) return false;
  const year = Number(text.slice(0, 4));
  const month = Number(text.slice(5, 7));
  const day = Number(text.slice(8, 10));
  if (month < 1 || month > 12 || day < 1) return false;
  const limit = month === 2 && isLeapYear(year) ? 29 : MONTH_DAYS[month];
  return day <= limit;
}

/** `date` is a strict YYYY-MM-DD real day, or '' meaning "no day filter". */
function assertDay(value) {
  const text = asText(value).trim();
  if (!text) return '';
  if (!isRealDay(text)) {
    throw badArgument('date must be a real calendar day in YYYY-MM-DD form.');
  }
  return text;
}

/** `month` must be a strict YYYY-MM value. */
function assertMonth(value) {
  const text = asText(value).trim();
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(text)) {
    throw badArgument('month must be in YYYY-MM form.');
  }
  return text;
}

/** Day part of the exporter's local `time` text, when it is a real date. */
function dayFromTimeText(value) {
  const text = asText(value).trim();
  if (text.length < 10) return '';
  const candidate = text.slice(0, 10);
  return isRealDay(candidate) ? candidate : '';
}

/** True when `zone` is a usable IANA name (or UTC) for Intl formatting. */
function isUsableZone(zone) {
  if (!zone) return false;
  if (zone === 'UTC') return true;
  try {
    new Intl.DateTimeFormat('en-CA', { timeZone: zone });
    return true;
  } catch {
    return false;
  }
}

/**
 * Durable deletion key: a domain-separated SHA-256 of (uid, key). Only this
 * digest is stored, so the ledger cannot leak a message id or conversation
 * name, while still refusing to reimport a deleted row.
 */
function deletionHash(kind, uid, key) {
  return createHash('sha256')
    .update(kind)
    .update('\u0000')
    .update(JSON.stringify([uid, key]))
    .digest('hex');
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

/** Normalise one imported JSONL row into flat columns plus its metadata.
 *
 * Two export layouts are supported, selected per row by field name:
 *  - Duoshan: msg_uuid / sender / created_time / type
 *  - Xiaohongshu: uuid / sender_id / create_time / content_type, where the
 *    owning account is the "@" suffix of local_chat_user_id or
 *    local_group_chat_id and the conversation key is the prefix before "@".
 *    conversation_id therefore stays per-account unique (key + '@' + uid)
 *    without breaking Duoshan ids, which never contain '@'.
 *
 * For Xiaohongshu rows, media referenced by the (nested-JSON) content is
 * mapped through the export's ``media_manifest.json`` and any downloaded
 * local file is recorded in ``meta.local_media`` so the reader renders the
 * actual photos/videos instead of only text.
 */
export function normalizeRow(line, localMedia = {}) {
  const row = parseJsonLossless(line);
  if (!row || typeof row !== 'object' || Array.isArray(row)) {
    throw new StoreError('invalid_source', MESSAGES_FILE + ' contains a line that is not a JSON object.', 500);
  }
  // A managed-library projection may carry canonical alias IDs and unioned
  // attachment metadata alongside the untouched source fields. Ordinary app
  // exports never use this explicit, versioned envelope.
  const projection = row._library_projection;
  if (projection?.version === 1) {
    const fields = ['msg_uuid', 'conversation_id', 'conversation_name', 'sender',
      'sender_name', 'time', 'type_name', 'text_original', 'text_lower', 'meta_json'];
    if (!fields.every(key => typeof projection[key] === 'string')
      || ![projection.created_time, projection.message_type].every(value => value === null || Number.isSafeInteger(value))
      || !parseJsonLossless(projection.meta_json)) {
      throw new StoreError('invalid_source', 'Invalid library projection.', 500);
    }
    return Object.fromEntries([...fields, 'created_time', 'message_type'].map(key => [key, projection[key]]));
  }
  const meta = row._export && typeof row._export === 'object' && !Array.isArray(row._export)
    ? row._export
    : {};
  const text = typeof meta.text === 'string' ? meta.text : '';
  const isXhs = row.msg_uuid === undefined
    && (row.uuid !== undefined || row.content_type !== undefined);
  if (isXhs) {
    const groupKey = asText(row.local_group_chat_id);
    const directKey = asText(row.local_chat_user_id);
    const ownerField = groupKey.includes('@') ? groupKey
      : (directKey.includes('@') ? directKey : '');
    const at = ownerField.indexOf('@');
    const owner = at >= 0 ? ownerField.slice(at + 1) : '';
    const key = at >= 0 ? ownerField.slice(0, at) : (groupKey || directKey);
    const isGroup = asInteger(row.is_group_chat) === 1 || (!directKey && !!groupKey);
    const localPaths = [];
    const seenFiles = new Set();
    for (const url of collectXhsMediaUrls(row)) {
      const base = url.split('?')[0];
      const file = localMedia[url] || localMedia[base] || '';
      if (file && !seenFiles.has(file)) {
        seenFiles.add(file);
        localPaths.push(file);
        if (localPaths.length >= 8) break;
      }
    }
    return {
      msg_uuid: asText(row.uuid),
      conversation_id: key && owner ? key + '@' + owner : key,
      conversation_name: asText(meta.conversation_name),
      sender: asText(row.sender_id),
      sender_name: asText(meta.sender_name),
      created_time: asInteger(row.create_time),
      time: asText(meta.time),
      message_type: asInteger(row.content_type),
      type_name: asText(meta.type_name),
      text_original: text,
      text_lower: text.toLowerCase(),
      meta_json: stringifyJson({
        ...meta,
        local_media: localPaths,
        _xhs: { owner, key, is_group: isGroup },
      }),
    };
  }
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

/* Xiaohongshu media extraction ------------------------------------------- */

const XHS_URL_RE = /https:\/\/[^\s"'\\]+/g;
const XHS_MEDIA_HOST_RE = /(msg-img\.|msg-video\.|sns-avatar-|sns-img\.|picasso-static|sns-na-i|image-url-2-feature|fe-video-qc|fe-platform\.)/;

/** Yield every CDN media URL reachable from a parsed row.
 *
 * Xiaohongshu nests payloads several JSON layers deep (a ``content`` string
 * holding JSON holding JSON...), where URLs appear escaped as
 * ``https:\/\/...``; each string that parses as JSON is decoded and scanned
 * so URLs are found at any depth with their real characters.
 */
function* collectXhsMediaUrls(node, depth = 0) {
  if (depth > 12) return;
  if (Array.isArray(node)) {
    for (const value of node) yield* collectXhsMediaUrls(value, depth + 1);
  } else if (node && typeof node === 'object') {
    for (const value of Object.values(node)) yield* collectXhsMediaUrls(value, depth + 1);
  } else if (typeof node === 'string') {
    for (const match of node.matchAll(XHS_URL_RE)) {
      const url = match[0].replace(/[",\]}]+$/, '');
      if (XHS_MEDIA_HOST_RE.test(url)) yield url;
    }
    const text = node.trim();
    if (text.startsWith('{') || text.startsWith('[')) {
      try {
        yield* collectXhsMediaUrls(parseJsonLossless(text), depth + 1);
      } catch {
        /* not JSON after all; the plain scan above already ran */
      }
    }
  }
}

/** Build ``{url: attachments-relative path}`` from the export's
 * ``media_manifest.json`` (query-less base keys included). */
function localMediaMap(outputDir) {
  try {
    const manifest = JSON.parse(
      fs.readFileSync(path.join(outputDir, 'media_manifest.json'), 'utf8'));
    const media = manifest && typeof manifest.media === 'object' ? manifest.media : {};
    const map = {};
    for (const [url, record] of Object.entries(media)) {
      if (!record || record.status !== 'ok' || typeof record.file !== 'string') continue;
      map[url] = record.file;
      map[url.split('?')[0]] = record.file;
    }
    return map;
  } catch {
    return {};
  }
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
  // Xiaohongshu layout: { user_table: [{user_id, nickname, ...}], ... }
  if (Array.isArray(contacts?.user_table)) {
    for (const contact of contacts.user_table) {
      if (!contact || typeof contact !== 'object') continue;
      if (asText(contact.user_id) !== uid) continue;
      const name = asText(contact.nickname) || asText(contact.account_name);
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
  /** Cached export timezone name (IANA) used for calendar days. */
  #timezoneName;
  /** Cached formatter for the `created_time` fallback path. */
  #dayFormatter;

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
    try {
      // Best effort: ask SQLite to overwrite freed cell content. This makes a
      // deleted message harder to recover from the database file, but it is not
      // a forensic guarantee, and the exported originals are never modified.
      try {
        this.#db.exec('PRAGMA secure_delete = ON');
      } catch {
        /* builds without the pragma still delete the rows */
      }
      this.#db.exec('PRAGMA busy_timeout = 5000');
      if (this.#databasePath !== ':memory:') {
        try {
          this.#db.exec('PRAGMA journal_mode = WAL');
        } catch {
          /* WAL is an optimisation; a read-only location still works. */
        }
      }
      this.#migrate();
    } catch (error) {
      // A failed migration must not leave a half-open handle or its -wal/-shm
      // files behind for the next process to trip over.
      try {
        this.#db.close();
      } catch {
        /* the handle may already be unusable */
      }
      throw error;
    }
  }

  /** Absolute export root this store serves. */
  get outputDir() {
    return this.#outputDir;
  }

  /** Exporting app recorded in `export_summary.json` ('Duoshan'|'Xiaohongshu'|''), so the UI can label itself instead of assuming Duoshan. */
  appLabel() {
    try {
      const summary = JSON.parse(
        fs.readFileSync(path.join(this.#outputDir, SUMMARY_FILE), 'utf8'));
      return summary && typeof summary === 'object' ? asText(summary.app).trim() : '';
    } catch {
      return '';
    }
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
    // A summary-only timezone change must take effect, so the resolved zone and
    // its formatter are rebuilt on every initialize.
    this.#timezoneName = undefined;
    this.#dayFormatter = undefined;

    const force = normalizeFlag(options.force);
    // A managed library is updated through additive imports. Its JSONL files
    // are projections, so a stale/interrupted projection must never replace
    // the authoritative database or erase history absent from a new backup.
    if (this.#getMeta('library_mode') === 'incremental-v1') {
      return { ...this.#counts(), imported: false, fingerprint: this.#getMeta('fingerprint') };
    }
    const importable = this.#importableAccounts();
    const counts = this.#counts();
    if (!importable.length) {
      // No JSONL at all: serve the existing index untouched. This is the
      // normal case for a relocated/portable export whose sources were pruned.
      this.#syncTimezone();
      return { ...counts, imported: false, fingerprint: this.#getMeta('fingerprint') };
    }
    const fingerprint = await this.#fingerprint(importable);
    const stored = this.#getMeta('fingerprint');
    // The completed-import marker (not the row count) decides whether the index
    // is current: permanently deleting every message must not look like an
    // empty database that needs a fresh import on the next restart.
    if (!force && stored && stored === fingerprint && this.#getMeta('imported_at')) {
      this.#syncTimezone();
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
             COALESCE((SELECT app FROM library_accounts l WHERE l.uid=a.uid), '') AS source_app,
             (SELECT COUNT(*) FROM messages m WHERE m.uid = a.uid) AS message_count,
             (SELECT COUNT(*) FROM conversations c WHERE c.uid = a.uid) AS conversation_count
      FROM accounts a
      ORDER BY a.uid ASC`).all());
    return rows.map((row) => ({
      uid: row.uid,
      name: row.name || row.uid,
      source_app: row.source_app || '',
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
   *          includeHidden?:boolean, date?:string}} [options]
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
    const date = assertDay(options.date);

    const scope = this.#messageScope(account, { conversationId, query, includeHidden });
    if (date) {
      scope.where.push('m.day = ?');
      scope.params.push(date);
    }
    const filter = scope.where.join(' AND ');

    const total = this.#guard(() => this.#db.prepare(
      `SELECT COUNT(*) AS total ${MESSAGE_JOIN} WHERE ${filter}`,
    ).get(...scope.params));
    const rows = this.#guard(() => this.#db.prepare(`
      SELECT ${MESSAGE_COLUMNS} ${MESSAGE_JOIN}
      WHERE ${filter}
      ORDER BY m.created_time ASC, m.msg_uuid ASC
      LIMIT ? OFFSET ?`).all(...scope.params, limit, offset));

    return {
      items: rows.map(mapMessage),
      total: asInteger(total?.total) ?? 0,
      limit,
      offset,
    };
  }

  /**
   * Recorded days for a month, with counts that honour the same scope as
   * `listMessages` (account, conversation, search over effective text and
   * note, and hidden visibility).
   *
   * Days are the exporter's local calendar day, so the viewer's timezone never
   * shifts a message into another day.
   *
   * @param {string} uid
   * @param {{month:string, conversationId?:string, q?:string,
   *          includeHidden?:boolean}} options
   * @returns {{month:string, timezone:string,
   *            days:Array<{date:string, message_count:number}>, total:number,
   *            first_date:string|null, last_date:string|null}}
   */
  getCalendar(uid, options = {}) {
    this.#assertOpen();
    const account = assertUid(uid);
    const month = assertMonth(options.month);
    const conversationId = asText(options.conversationId).trim();
    const query = asText(options.q).trim().toLowerCase();
    const includeHidden = normalizeFlag(options.includeHidden);

    const scope = this.#messageScope(account, { conversationId, query, includeHidden });
    const filter = scope.where.join(' AND ');

    // Day strings sort lexicographically, so a month is an inclusive range and
    // never needs a LIKE scan.
    const rows = this.#guard(() => this.#db.prepare(`
      SELECT m.day AS day, COUNT(*) AS total ${MESSAGE_JOIN}
      WHERE ${filter} AND m.day >= ? AND m.day <= ?
      GROUP BY m.day
      ORDER BY m.day ASC`).all(...scope.params, month + '-01', month + '-31'));

    const days = rows
      .filter((row) => row.day && isRealDay(row.day))
      .map((row) => ({ date: row.day, message_count: asInteger(row.total) ?? 0 }));
    const total = days.reduce((sum, day) => sum + day.message_count, 0);

    // The overall extent ignores the month filter so the UI can hint at where
    // recorded data exists, including months it is not currently showing.
    const bounds = this.#guard(() => this.#db.prepare(`
      SELECT MIN(m.day) AS first_date, MAX(m.day) AS last_date ${MESSAGE_JOIN}
      WHERE ${filter} AND m.day <> ''`).get(...scope.params));

    return {
      month,
      timezone: this.#timezone(),
      days,
      total,
      first_date: bounds?.first_date ? asText(bounds.first_date) : null,
      last_date: bounds?.last_date ? asText(bounds.last_date) : null,
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

  /**
   * Permanently delete one message and its annotation.
   *
   * @param {string} uid
   * @param {string} msgUuid
   * @param {number} expectedRevision
   * @returns {{deleted:true, account_uid:string, deleted_messages:number,
   *            deleted_conversations:number, msg_uuids:string[], msg_uuid:string}}
   */
  deleteMessage(uid, msgUuid, expectedRevision) {
    const result = this.deleteMessages(uid, [{
      msg_uuid: msgUuid,
      expected_revision: expectedRevision,
    }]);
    return { ...result, msg_uuid: result.msg_uuids[0] };
  }

  /**
   * Permanently delete one conversation, every message it holds (hidden ones
   * included) and their annotations.
   *
   * @param {string} uid
   * @param {string} conversationId
   * @param {number} expectedMessageCount
   * @returns {{deleted:true, account_uid:string, deleted_messages:number,
   *            deleted_conversations:number, conversation_ids:string[],
   *            conversation_id:string}}
   */
  deleteConversation(uid, conversationId, expectedMessageCount) {
    const result = this.deleteConversations(uid, [{
      id: conversationId,
      expected_message_count: expectedMessageCount,
    }]);
    return { ...result, conversation_id: result.conversation_ids[0] };
  }

  /**
   * Permanently delete several messages atomically.
   *
   * Every entry is validated (existence and revision) inside the write
   * transaction before anything is removed, so a stale or unknown entry leaves
   * the database exactly as it was.
   *
   * @param {string} uid
   * @param {Array<{msg_uuid:string, expected_revision:number}>} items
   * @returns {{deleted:true, account_uid:string, deleted_messages:number,
   *            deleted_conversations:number, msg_uuids:string[]}}
   */
  deleteMessages(uid, items) {
    this.#assertOpen();
    const account = assertUid(uid);
    const requests = this.#readMessageBatch(items);

    let removedConversations = 0;
    this.#db.exec('BEGIN IMMEDIATE');
    try {
      this.#assertAccountExists(account);

      const read = this.#db.prepare(`
        SELECT m.conversation_id AS conversation_id,
               COALESCE(e.revision, 0) AS revision
        ${MESSAGE_JOIN}
        WHERE m.uid = ? AND m.msg_uuid = ?`);
      const found = [];
      for (const request of requests) {
        const row = read.get(account, request.msg_uuid);
        if (!row) {
          throw new StoreError('not_found',
            'Message not found: ' + request.msg_uuid, 404);
        }
        if ((asInteger(row.revision) ?? 0) !== request.expected_revision) {
          throw new StoreError('conflict',
            'This message was changed by someone else; reload it and retry.', 409);
        }
        found.push({ msg_uuid: request.msg_uuid, conversation_id: row.conversation_id });
      }

      const removeMessage = this.#db.prepare('DELETE FROM messages WHERE uid = ? AND msg_uuid = ?');
      const removeEdit = this.#db.prepare('DELETE FROM message_edits WHERE uid = ? AND msg_uuid = ?');
      const recordDeletion = this.#db.prepare(
        'INSERT OR REPLACE INTO deletions (kind, key_hash, deleted_at) VALUES (?, ?, ?)');
      const deletedAt = new Date().toISOString();
      const conversations = new Set();

      for (const row of found) {
        removeMessage.run(account, row.msg_uuid);
        removeEdit.run(account, row.msg_uuid);
        recordDeletion.run('message', deletionHash('message', account, row.msg_uuid), deletedAt);
        // Include an empty conversation id too: the import groups messages with
        // no conversation under '', and its row must be refreshed or removed
        // like any other.
        conversations.add(asText(row.conversation_id));
      }
      removedConversations = this.#refreshConversations(account, conversations);

      this.#db.exec('COMMIT');
    } catch (error) {
      try {
        this.#db.exec('ROLLBACK');
      } catch {
        /* the transaction may already be gone */
      }
      throw this.#asStoreError(error);
    }

    // Runs only after a committed delete, and never throws: a best-effort purge
    // must not turn a successful deletion into an error response.
    this.#purgeDeletedBytes();

    return {
      deleted: true,
      account_uid: account,
      deleted_messages: requests.length,
      deleted_conversations: removedConversations,
      msg_uuids: requests.map((request) => request.msg_uuid),
    };
  }

  /**
   * Permanently delete several conversations atomically. Hidden messages are
   * removed as well, because a whole conversation leaves the database.
   *
   * @param {string} uid
   * @param {Array<{id:string, expected_message_count:number}>} items
   * @returns {{deleted:true, account_uid:string, deleted_messages:number,
   *            deleted_conversations:number, conversation_ids:string[]}}
   */
  deleteConversations(uid, items) {
    this.#assertOpen();
    const account = assertUid(uid);
    const requests = this.#readConversationBatch(items);

    let removedMessages = 0;
    this.#db.exec('BEGIN IMMEDIATE');
    try {
      this.#assertAccountExists(account);

      const read = this.#db.prepare(`
        SELECT message_count AS message_count FROM conversations
        WHERE uid = ? AND conversation_id = ?`);
      const countMessages = this.#db.prepare(
        'SELECT COUNT(*) AS total FROM messages WHERE uid = ? AND conversation_id = ?');
      const checked = [];
      for (const request of requests) {
        const row = read.get(account, request.id);
        if (!row) {
          throw new StoreError('not_found',
            'Conversation not found: ' + request.id, 404);
        }
        if ((asInteger(row.message_count) ?? 0) !== request.expected_message_count) {
          throw new StoreError('conflict',
            'This conversation changed since it was loaded; reload it and retry.', 409);
        }
        const counted = countMessages.get(account, request.id);
        checked.push({ id: request.id, messages: asInteger(counted?.total) ?? 0 });
      }

      const removeEdits = this.#db.prepare(`
        DELETE FROM message_edits WHERE uid = ? AND msg_uuid IN (
          SELECT msg_uuid FROM messages WHERE uid = ? AND conversation_id = ?)`);
      const removeMessages = this.#db.prepare(
        'DELETE FROM messages WHERE uid = ? AND conversation_id = ?');
      const removeConversation = this.#db.prepare(
        'DELETE FROM conversations WHERE uid = ? AND conversation_id = ?');
      const recordDeletion = this.#db.prepare(
        'INSERT OR REPLACE INTO deletions (kind, key_hash, deleted_at) VALUES (?, ?, ?)');
      const deletedAt = new Date().toISOString();

      for (const row of checked) {
        removeEdits.run(account, account, row.id);
        removeMessages.run(account, row.id);
        removeConversation.run(account, row.id);
        recordDeletion.run('conversation', deletionHash('conversation', account, row.id), deletedAt);
        removedMessages += row.messages;
      }

      this.#db.exec('COMMIT');
    } catch (error) {
      try {
        this.#db.exec('ROLLBACK');
      } catch {
        /* the transaction may already be gone */
      }
      throw this.#asStoreError(error);
    }

    // Runs only after a committed delete, and never throws: a best-effort purge
    // must not turn a successful deletion into an error response.
    this.#purgeDeletedBytes();

    return {
      deleted: true,
      account_uid: account,
      deleted_messages: removedMessages,
      deleted_conversations: requests.length,
      conversation_ids: requests.map((request) => request.id),
    };
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
   * Create or upgrade the schema *additively and atomically*.
   *
   * Older reader databases gain the new columns, tables and indexes in place:
   * no index table is ever dropped, so an index-only export (its JSONL pruned)
   * keeps every record through the upgrade. The whole upgrade, including the
   * column backfill and the version/timezone markers, shares one transaction,
   * so a failure can never leave a `day` column present but unfilled: the added
   * column is rolled back with everything else and the next run retries.
   */
  #migrate() {
    this.#db.exec('BEGIN IMMEDIATE');
    try {
      this.#db.exec(TABLES_SCHEMA);
      const added = [
        ...this.#addMissingColumns('messages', MESSAGE_COLUMN_ADDITIONS),
        ...this.#addMissingColumns('conversations', CONVERSATION_COLUMN_ADDITIONS),
      ];
      this.#db.exec(INDEXES_SCHEMA);
      this.#backfill(added);
      // Record a zone only when none is stored yet. Overwriting it here would
      // hide a summary-only timezone edit from initialize(), which is what
      // reconciles the zone-dependent days.
      if (!this.#getMeta('timezone')) this.#setMeta('timezone', this.#timezone());
      this.#setMeta('format_version', FORMAT_VERSION);
      this.#db.exec('COMMIT');
    } catch (error) {
      try {
        this.#db.exec('ROLLBACK');
      } catch {
        /* the transaction may already be gone */
      }
      throw this.#asStoreError(error);
    }
  }

  /**
   * Add columns a database predates. Table and column identifiers come from the
   * module's own constants, never from request data. Runs inside the migration
   * transaction.
   */
  #addMissingColumns(table, additions) {
    const existing = new Set(
      this.#db.prepare(`PRAGMA table_info(${table})`).all().map((row) => row.name));
    const added = [];
    for (const [column, definition] of additions) {
      if (existing.has(column)) continue;
      this.#db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
      added.push(column);
    }
    return added;
  }

  /**
   * Fill columns that this run just added, so older rows stay queryable. Called
   * inside the migration transaction, which is what makes the upgrade atomic.
   */
  #backfill(added) {
    if (!added.length) return;
    if (added.includes('text_lower')) this.#backfillTextLower();
    if (added.includes('name_lower')) this.#backfillNameLower();
    if (added.includes('day')) this.#backfillDays();
  }

  #backfillTextLower() {
    const rows = this.#db.prepare('SELECT uid, msg_uuid, text_original FROM messages').all();
    const update = this.#db.prepare(
      'UPDATE messages SET text_lower = ? WHERE uid = ? AND msg_uuid = ?');
    for (const row of rows) {
      update.run(asText(row.text_original).toLowerCase(), row.uid, row.msg_uuid);
    }
  }

  #backfillNameLower() {
    const rows = this.#db.prepare('SELECT uid, conversation_id, name FROM conversations').all();
    const update = this.#db.prepare(
      'UPDATE conversations SET name_lower = ? WHERE uid = ? AND conversation_id = ?');
    for (const row of rows) {
      update.run(asText(row.name).toLowerCase(), row.uid, row.conversation_id);
    }
  }

  /** Compute `day` for every existing row, including an index-only database. */
  #backfillDays() {
    const rows = this.#db.prepare('SELECT uid, msg_uuid, time, created_time FROM messages').all();
    const update = this.#db.prepare(
      'UPDATE messages SET day = ? WHERE uid = ? AND msg_uuid = ?');
    for (const row of rows) {
      update.run(this.#dayValue(row.time, row.created_time), row.uid, row.msg_uuid);
    }
  }

  // ---------------------------------------------------------------- internals

  #assertOpen() {
    if (this.#closed) {
      throw new StoreError('closed', 'The store has been closed.', 500);
    }
  }

  /** Convert unexpected driver failures into StoreError without leaking SQL. */
  #asStoreError(error) {
    if (error instanceof StoreError) return error;
    return new StoreError('database_error', 'The reader index could not be read.',
      500, { cause: error });
  }

  #guard(operation) {
    try {
      return operation();
    } catch (error) {
      throw this.#asStoreError(error);
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

  /**
   * Export timezone used for calendar days.
   *
   * Precedence: the root summary (authoritative and part of the fingerprint),
   * then the zone persisted by an earlier import, then the `timezone` field the
   * exporter stored in each row's `_export` metadata (this is what lets a
   * version-2 database whose JSONL and summary are both gone keep its calendar
   * zone), and only then UTC.
   */
  #timezone() {
    if (this.#timezoneName !== undefined) return this.#timezoneName;
    let zone = this.#summaryTimezone()
      || this.#storedTimezone()
      || this.#metadataTimezone()
      || 'UTC';
    this.#timezoneName = zone;
    return zone;
  }

  /** Timezone recorded in `export_summary.json`, when usable. */
  #summaryTimezone() {
    try {
      const summary = JSON.parse(
        fs.readFileSync(path.join(this.#outputDir, SUMMARY_FILE), 'utf8'));
      const zone = summary && typeof summary === 'object'
        ? asText(summary.timezone).trim() : '';
      return isUsableZone(zone) ? zone : '';
    } catch {
      return '';
    }
  }

  /** Timezone persisted in `store_meta` by an earlier import or migration. */
  #storedTimezone() {
    const zone = this.#getMeta('timezone');
    return zone && isUsableZone(zone) ? zone : '';
  }

  /** Timezone carried by the exported `_export` block of a stored row. */
  #metadataTimezone() {
    let row;
    try {
      row = this.#db.prepare(
        `SELECT meta_json FROM messages WHERE instr(meta_json, '"timezone"') > 0 LIMIT 1`,
      ).get();
    } catch {
      return '';
    }
    if (!row || !row.meta_json) return '';
    const meta = parseJsonLossless(row.meta_json);
    const zone = meta && typeof meta === 'object' ? asText(meta.timezone).trim() : '';
    return isUsableZone(zone) ? zone : '';
  }

  /**
   * Keep the persisted zone and the zone-dependent days in step with the export.
   *
   * Only rows whose exported `time` text does not carry a real day depend on the
   * zone; the rest are skipped even though they are read. A change therefore
   * costs one pass, and only when `export_summary.json` was edited.
   */
  #syncTimezone() {
    const zone = this.#timezone();
    if (this.#getMeta('timezone') === zone) return;
    this.#db.exec('BEGIN IMMEDIATE');
    try {
      this.#setMeta('timezone', zone);
      this.#refreshZoneDependentDays();
      this.#db.exec('COMMIT');
    } catch (error) {
      try {
        this.#db.exec('ROLLBACK');
      } catch {
        /* the transaction may already be gone */
      }
      throw this.#asStoreError(error);
    }
  }

  /** Recompute `day` for rows that had to fall back to `created_time`. */
  #refreshZoneDependentDays() {
    const rows = this.#db.prepare(
      'SELECT uid, msg_uuid, time, created_time, day FROM messages').all();
    const update = this.#db.prepare(
      'UPDATE messages SET day = ? WHERE uid = ? AND msg_uuid = ?');
    for (const row of rows) {
      if (dayFromTimeText(row.time)) continue; // the exported day decides
      const day = this.#formatDay(row.created_time);
      if (day !== asText(row.day)) update.run(day, row.uid, row.msg_uuid);
    }
  }

  /** Lazily created formatter for the `created_time` fallback path. */
  #formatter() {
    if (this.#dayFormatter === undefined) {
      this.#dayFormatter = new Intl.DateTimeFormat('en-CA', {
        timeZone: this.#timezone(),
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
      });
    }
    return this.#dayFormatter;
  }

  /**
   * Calendar day for one row: the exporter's own local `time` when it is a real
   * date, otherwise `created_time` rendered in the export timezone. Never the
   * server's or the browser's local timezone.
   */
  #dayValue(time, createdTime) {
    const fromTime = dayFromTimeText(time);
    if (fromTime) return fromTime;
    return this.#formatDay(createdTime);
  }

  #formatDay(createdTime) {
    const milliseconds = asInteger(createdTime);
    if (milliseconds === null) return '';
    let parts;
    try {
      parts = this.#formatter().formatToParts(new Date(milliseconds));
    } catch {
      return '';
    }
    const pick = (type) => parts.find((part) => part.type === type)?.value ?? '';
    const text = `${pick('year')}-${pick('month')}-${pick('day')}`;
    return isRealDay(text) ? text : '';
  }

  /**
   * Shared WHERE clause for message queries: account, optional conversation,
   * search over effective text and note, and hidden visibility.
   */
  #messageScope(account, { conversationId = '', query = '', includeHidden = false }) {
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
    return { where, params };
  }

  /**
   * Recompute count/first/last for the conversations touched by a deletion and
   * drop the ones that no longer hold any message. Returns how many were
   * removed.
   */
  #refreshConversations(account, conversationIds) {
    const aggregate = this.#db.prepare(`
      SELECT COUNT(*) AS total, MIN(created_time) AS first_time,
             MAX(created_time) AS last_time
      FROM messages WHERE uid = ? AND conversation_id = ?`);
    const update = this.#db.prepare(`
      UPDATE conversations SET message_count = ?, first_time = ?, last_time = ?
      WHERE uid = ? AND conversation_id = ?`);
    const remove = this.#db.prepare(
      'DELETE FROM conversations WHERE uid = ? AND conversation_id = ?');
    let removed = 0;
    for (const id of conversationIds) {
      const row = aggregate.get(account, id);
      const total = asInteger(row?.total) ?? 0;
      if (total === 0) {
        remove.run(account, id);
        removed += 1;
      } else {
        update.run(total, asInteger(row.first_time), asInteger(row.last_time), account, id);
      }
    }
    return removed;
  }

  /**
   * Drop the write-ahead log after a permanent deletion.
   *
   * Best effort: `secure_delete` overwrites the freed cells inside the database
   * pages, but the WAL still holds the pre-delete frames until it is
   * checkpointed. Truncating it here removes that copy for the common case. It
   * is not a guarantee, and it is deliberately infallible: the delete is already
   * committed, so a purge problem must never surface as a failed deletion. The
   * next checkpoint (or a clean close) truncates the log anyway.
   */
  #purgeDeletedBytes() {
    if (this.#databasePath === ':memory:') return;
    try {
      this.#db.exec('PRAGMA wal_checkpoint(TRUNCATE)');
    } catch {
      /* another reader may hold the log open; the next checkpoint retries */
    }
  }

  /** An account is known once it has been imported; deletions of it are 404. */
  #assertAccountExists(account) {
    const row = this.#db.prepare('SELECT 1 AS ok FROM accounts WHERE uid = ? LIMIT 1')
      .get(account);
    if (!row) throw new StoreError('not_found', 'Account not found.', 404);
  }

  /** Validate a message deletion batch before any database work. */
  #readMessageBatch(items) {
    if (!Array.isArray(items) || items.length === 0) {
      throw badArgument('Select at least one message.');
    }
    if (items.length > MAX_BATCH) {
      throw new StoreError('invalid_batch',
        `At most ${MAX_BATCH} messages can be deleted at once.`, 400);
    }
    const seen = new Set();
    const requests = [];
    for (const item of items) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        throw badArgument('Each message entry must be an object.');
      }
      const key = assertKey(item.msg_uuid, 'msg_uuid');
      const revision = item.expected_revision;
      if (typeof revision !== 'number' || !Number.isInteger(revision) || revision < 0) {
        throw badArgument('expected_revision must be a non-negative integer.');
      }
      if (seen.has(key)) throw badArgument('Duplicate msg_uuid in the request.');
      seen.add(key);
      requests.push({ msg_uuid: key, expected_revision: revision });
    }
    return requests;
  }

  /** Validate a conversation deletion batch before any database work. */
  #readConversationBatch(items) {
    if (!Array.isArray(items) || items.length === 0) {
      throw badArgument('Select at least one conversation.');
    }
    if (items.length > MAX_BATCH) {
      throw new StoreError('invalid_batch',
        `At most ${MAX_BATCH} conversations can be deleted at once.`, 400);
    }
    const seen = new Set();
    const requests = [];
    for (const item of items) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        throw badArgument('Each conversation entry must be an object.');
      }
      const key = assertKey(item.id, 'conversation id');
      const count = item.expected_message_count;
      if (typeof count !== 'number' || !Number.isInteger(count) || count < 1) {
        throw badArgument('expected_message_count must be a positive integer.');
      }
      if (seen.has(key)) throw badArgument('Duplicate conversation id in the request.');
      seen.add(key);
      requests.push({ id: key, expected_message_count: count });
    }
    return requests;
  }

  /** Load the durable deletion ledger as digests grouped by kind. */
  #loadDeletionHashes() {
    const message = new Set();
    const conversation = new Set();
    for (const row of this.#db.prepare('SELECT kind, key_hash FROM deletions').all()) {
      if (row.kind === 'message') message.add(row.key_hash);
      else if (row.kind === 'conversation') conversation.add(row.key_hash);
    }
    return { message, conversation };
  }

  /** Account directories: `<output>/<decimal uid>/`. */
  #accountDirs() {
    return accountDirectories(this.#outputDir).map(account => account.uid);
  }

  /**
   * Accounts that actually carry messages. Only these are reimported, so an
   * account whose export was pruned keeps its previously indexed rows instead
   * of silently disappearing from the reader.
   */
  #importableAccounts() {
    return this.#accountDirs().filter((uid) => {
      const file = path.join(accountDirectory(this.#outputDir, uid), MESSAGES_FILE);
      return fs.existsSync(file) && fs.statSync(file).isFile();
    });
  }

  /**
   * Importable files, relative to the export root, in a stable order.
   *
   * The root summary is included because it carries the export timezone that
   * decides how a `created_time`-only row is assigned to a calendar day: a
   * timezone-only edit must invalidate the import instead of leaving stale days
   * behind.
   */
  #sourceFiles(accounts) {
    const files = [];
    for (const uid of accounts) {
      for (const name of [MESSAGES_FILE, STATISTICS_FILE, CONTACTS_FILE]) {
        const file = path.join(accountDirectory(this.#outputDir, uid), name);
        if (fs.existsSync(file) && fs.statSync(file).isFile()) {
          files.push(path.relative(this.#outputDir, file).replace(/\\/g, '/'));
        }
      }
    }
    const summary = path.join(this.#outputDir, SUMMARY_FILE);
    if (fs.existsSync(summary) && fs.statSync(summary).isFile()) {
      files.push(SUMMARY_FILE);
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
    const localMedia = localMediaMap(this.#outputDir);
    const insertMessage = this.#db.prepare(`
      INSERT OR REPLACE INTO messages (
        uid, msg_uuid, conversation_id, conversation_name, sender, sender_name,
        created_time, time, day, message_type, type_name, text_original,
        text_lower, raw_json, meta_json)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
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

    // Permanently deleted rows must never come back from an unchanged source,
    // so the ledger is consulted while importing. Hashing is skipped entirely
    // when a kind has no recorded deletions.
    const deleted = this.#loadDeletionHashes();

    this.#db.exec('BEGIN IMMEDIATE');
    try {
      for (const uid of accounts) {
        const directory = accountDirectory(this.#outputDir, uid);
        const messagesPath = path.join(directory, MESSAGES_FILE);

        // Replace this account's rows; other accounts and all annotations in
        // message_edits are left alone.
        clearMessages.run(uid);
        clearConversations.run(uid);

        const statistics = readJsonFile(path.join(directory, STATISTICS_FILE));
        const contacts = readJsonFile(path.join(directory, CONTACTS_FILE));
        insertAccount.run(uid, accountName(uid, statistics, contacts));

        for await (const line of readJsonLines(messagesPath)) {
          const row = normalizeRow(line, localMedia);
          if (!row.msg_uuid) continue;
          if (deleted.message.size
            && deleted.message.has(deletionHash('message', uid, row.msg_uuid))) continue;
          if (deleted.conversation.size && row.conversation_id
            && deleted.conversation.has(deletionHash('conversation', uid, row.conversation_id))) {
            continue;
          }
          insertMessage.run(
            uid,
            row.msg_uuid,
            row.conversation_id,
            row.conversation_name,
            row.sender,
            row.sender_name,
            row.created_time,
            row.time,
            this.#dayValue(row.time, row.created_time),
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
      this.#setMeta('timezone', this.#timezone());
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

// The incremental importer shares the exact legacy normalization, calendar,
// ID and deletion contracts instead of maintaining a second adapter.
export { accountName, dayFromTimeText, deletionHash, hashFile, localMediaMap,
  parseJsonLossless, readJsonFile, readJsonLines, stringifyJson };

export default ChatStore;
