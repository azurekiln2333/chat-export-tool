/** Attach verified media to live messages without importing message content. */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { isDeepStrictEqual, parseArgs } from 'node:util';
import { pathToFileURL } from 'node:url';
import { APP_PREFIX } from './account-layout.mjs';
import { projectAccounts } from './library-import.mjs';
import { deletionHash, hashFile, readJsonLines } from './store.mjs';

const MEDIA_FIELDS = ['local_media', 'original_media', 'media', 'media_status'];
const INPUT_FIELDS = new Set(['uid', 'msg_uuid', 'app', 'expected_raw_sha256', ...MEDIA_FIELDS]);
// Download diagnostics (including full signed URLs) belong only in local
// reports/journals. A resource patch carries playback/file properties only.
const RESOURCE_FIELDS = new Set(['status', 'kind', 'path', 'sha256', 'bytes', 'format', 'source_format',
  'width', 'height', 'frames', 'duration', 'mime', 'codec']);
const KINDS = new Set(['voice', 'emoji', 'audio', 'image', 'video', 'file']);
const STATUSES = new Set(['ok', 'failed', 'missing_url', 'missing', 'unsupported', 'unavailable']);
const MEDIA_STATUSES = new Set(['available', 'partial', 'missing', 'failed', 'unsupported', 'none']);
const SCHEMA = `CREATE TABLE IF NOT EXISTS library_media_batches (
 fingerprint TEXT PRIMARY KEY, source_id TEXT NOT NULL, applied_at TEXT NOT NULL,
 accounts_json TEXT NOT NULL, result_json TEXT NOT NULL,
 projection_status TEXT NOT NULL DEFAULT 'pending')`;

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function validateUpdate(row) {
  if (!object(row) || Object.keys(row).some(key => !INPUT_FIELDS.has(key))) {
    throw new Error('Media update contains unknown or non-media fields.');
  }
  if (!['uid', 'msg_uuid'].every(key => typeof row[key] === 'string' && row[key].length && !row[key].includes('\0'))) {
    throw new Error('Media update requires string uid and msg_uuid.');
  }
  if (!MEDIA_FIELDS.some(key => Object.hasOwn(row, key))) throw new Error('Media update has no media fields.');
  if (Object.hasOwn(row, 'app') && !Object.hasOwn(APP_PREFIX, row.app)) throw new Error('Invalid media update app.');
  if (Object.hasOwn(row, 'expected_raw_sha256') && (typeof row.expected_raw_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(row.expected_raw_sha256))) {
    throw new Error('Invalid expected raw message hash.');
  }
  for (const field of ['local_media', 'original_media']) {
    if (Object.hasOwn(row, field) && (!Array.isArray(row[field]) || row[field].some(value => typeof value !== 'string'))) {
      throw new Error(`${field} must be an array of local file paths.`);
    }
  }
  if (Object.hasOwn(row, 'media_status') && !MEDIA_STATUSES.has(row.media_status)) throw new Error('Invalid media_status.');
  if (Object.hasOwn(row, 'media')) {
    const resource = row.media;
    if (!object(resource)) throw new Error('media must be a resource object.');
    if (Object.keys(resource).some(key => !RESOURCE_FIELDS.has(key))) throw new Error('media contains report-only or unknown fields.');
    if (Object.hasOwn(resource, 'kind') && !KINDS.has(resource.kind)) throw new Error('Invalid media kind.');
    if (Object.hasOwn(resource, 'status') && !STATUSES.has(resource.status)) throw new Error('Invalid media status.');
    if (Object.hasOwn(resource, 'path') && (typeof resource.path !== 'string' || !resource.path)) throw new Error('Invalid media path.');
    if (resource.status === 'ok' && (!resource.path || !resource.kind)) throw new Error('Successful media requires kind and path.');
    if (Object.hasOwn(resource, 'bytes') && (!Number.isSafeInteger(resource.bytes) || resource.bytes < 0)) throw new Error('Invalid media byte count.');
    if (Object.hasOwn(resource, 'sha256') && (typeof resource.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(resource.sha256))) throw new Error('Invalid media hash.');
    for (const key of ['width', 'height', 'frames']) {
      if (Object.hasOwn(resource, key) && (!Number.isSafeInteger(resource[key]) || resource[key] < 0)) throw new Error(`Invalid media ${key}.`);
    }
    if (Object.hasOwn(resource, 'duration') && (typeof resource.duration !== 'number' || !Number.isFinite(resource.duration) || resource.duration < 0)) {
      throw new Error('Invalid media duration.');
    }
    for (const key of ['mime', 'format', 'source_format', 'codec']) {
      if (Object.hasOwn(resource, key) && typeof resource[key] !== 'string') throw new Error(`Invalid media ${key}.`);
    }
  }
  return row;
}

function validateLocalPath(root, relative) {
  if (typeof relative !== 'string' || !relative.startsWith('attachments/') || /[\\:\x00-\x1f]/.test(relative)
    || relative.split('/').some(part => !part || part === '..' || part === '.')) {
    throw new Error('Invalid local media path.');
  }
  const attachments = fs.realpathSync(path.join(root, 'attachments'));
  const file = fs.realpathSync(path.join(root, relative));
  if (!attachments.startsWith(root + path.sep) || !file.startsWith(attachments + path.sep) || !fs.statSync(file).isFile()) {
    throw new Error('Local media must be an existing file inside library attachments.');
  }
  return file;
}

async function validateFiles(root, row, validated) {
  const paths = [...(row.local_media || []), ...(row.original_media || [])];
  if (row.media?.path) paths.push(row.media.path);
  for (const relative of new Set(paths)) {
    // Resolve containment each time; hashing is shared for repeated references.
    const file = validateLocalPath(root, relative);
    if (relative === row.media?.path) {
      if (row.media.bytes !== undefined && fs.statSync(file).size !== row.media.bytes) throw new Error('Media byte count does not match file.');
      if (row.media.sha256 !== undefined) {
        if (!validated.has(file)) validated.set(file, await hashFile(file));
        if (validated.get(file) !== row.media.sha256) throw new Error('Media hash does not match file.');
      }
    }
  }
}

function mergeObject(previous, next) {
  const result = { ...previous };
  for (const [key, value] of Object.entries(next)) {
    result[key] = object(value) && object(previous?.[key]) ? mergeObject(previous[key], value) : value;
  }
  return result;
}

async function mediaChanges(root, meta, row, validated) {
  const next = {};
  for (const field of ['local_media', 'original_media']) {
    if (Object.hasOwn(row, field)) next[field] = [...new Set([...(Array.isArray(meta[field]) ? meta[field] : []), ...row[field]])];
  }
  if (row.media) {
    const differentResource = row.media.path && meta.media?.path && row.media.path !== meta.media.path;
    let keepPrimary = false;
    if (differentResource && meta.media.status === 'ok') {
      try { await validateFiles(root, { media: meta.media }, validated); keepPrimary = true; }
      catch { /* A missing or corrupt primary can be replaced by the verified new resource. */ }
    }
    if (keepPrimary) {
      // A message can have a photo plus live video, or a video plus poster.
      // Keep its first usable primary stable across repeated multi-resource runs.
      if (row.media.status === 'ok') {
        const existingPaths = next.local_media || (Array.isArray(meta.local_media) ? meta.local_media : []);
        next.local_media = [...new Set([...existingPaths, row.media.path])];
      }
    } else if (!(meta.media?.status === 'ok' && row.media.status && row.media.status !== 'ok')) {
      // Metadata such as kind/codec/size belongs to one file, and must not
      // leak from a missing old primary into a different replacement file.
      next.media = mergeObject(!differentResource && object(meta.media) ? meta.media : {}, row.media);
      if (differentResource && meta.media.status === 'ok') {
        const existingPaths = next.local_media || (Array.isArray(meta.local_media) ? meta.local_media : []);
        next.local_media = [...new Set([...existingPaths, meta.media.path])];
      }
    }
  }
  if (Object.hasOwn(row, 'media_status')) {
    const available = (next.media || meta.media)?.status === 'ok'
      || (next.local_media || meta.local_media || []).length > 0;
    next.media_status = available ? 'available' : row.media_status;
  }
  return Object.fromEntries(Object.entries(next).filter(([key, value]) => !isDeepStrictEqual(meta[key], value)));
}

function openLibrary(outputDir) {
  const root = fs.realpathSync(path.resolve(outputDir));
  const file = path.join(root, 'reader.sqlite');
  if (!fs.statSync(file).isFile()) throw new Error('Existing reader.sqlite is required.');
  const db = new DatabaseSync(file);
  try {
    db.exec('PRAGMA busy_timeout=30000');
    if (db.prepare("SELECT value FROM store_meta WHERE key='library_mode'").get()?.value !== 'incremental-v1') {
      throw new Error('Media updates require a managed library; import or organize the library first.');
    }
    // Never initialize from JSONL here: it may contain already-deleted rows.
    db.exec(SCHEMA);
    return { db, root };
  } catch (error) { db.close(); throw error; }
}

function validateProjectionDirectories(db, root, accounts) {
  for (const uid of accounts) {
    const account = db.prepare('SELECT app,directory FROM library_accounts WHERE uid=?').get(uid);
    if (!account || !Object.hasOwn(APP_PREFIX, account.app)
      || ![uid, `${APP_PREFIX[account.app]}_${uid}`].includes(account.directory)
      || /[\\/:\x00-\x1f]/.test(account.directory)) throw new Error('Invalid account projection directory.');
    const directory = path.join(root, account.directory);
    if (fs.existsSync(directory) && !fs.realpathSync(directory).startsWith(root + path.sep)) {
      throw new Error('Account projection directory is outside library.');
    }
  }
  const history = path.join(root, '.import-history');
  if (fs.existsSync(history) && !fs.realpathSync(history).startsWith(root + path.sep)) throw new Error('Projection history is outside library.');
}

async function finishProjections(db, root) {
  const pending = db.prepare("SELECT fingerprint,accounts_json FROM library_media_batches WHERE projection_status='pending' ORDER BY applied_at,fingerprint").all();
  const accounts = [...new Set(pending.flatMap(batch => JSON.parse(batch.accounts_json)))];
  validateProjectionDirectories(db, root, accounts);
  await projectAccounts(db, root, accounts);
  const complete = db.prepare("UPDATE library_media_batches SET projection_status='completed' WHERE fingerprint=?");
  db.exec('BEGIN IMMEDIATE');
  try {
    for (const batch of pending) complete.run(batch.fingerprint);
    db.exec('COMMIT');
  } catch (error) { db.exec('ROLLBACK'); throw error; }
  return { accounts, resumed_batches: pending.length };
}

/** Recovery needs only the authoritative DB, not the previous patch file. */
export async function resumeLibraryMedia({ outputDir } = {}) {
  const { db, root } = openLibrary(outputDir);
  try {
    return { status: 'completed', projection_status: 'completed', ...await finishProjections(db, root) };
  } finally { db.close(); }
}

/** Update only allowlisted media JSON fields on matching live (uid, UUID) rows. */
export async function patchLibraryMedia({ outputDir, updatesFile, sourceId = '', project = true } = {}) {
  const updates = fs.realpathSync(path.resolve(updatesFile));
  const initialHash = await hashFile(updates);
  const fingerprint = createHash('sha256').update('library-media-v1\0' + initialHash).digest('hex');
  const { db, root } = openLibrary(outputDir);
  try {
    let result;
    db.exec('BEGIN IMMEDIATE');
    try {
      const prior = db.prepare('SELECT result_json FROM library_media_batches WHERE fingerprint=?').get(fingerprint);
      if (prior) {
        result = { ...JSON.parse(prior.result_json), repeated: true, patched_messages: 0 };
      } else {
        const current = db.prepare(`SELECT m.meta_json,m.raw_json,m.conversation_id,a.app FROM messages m
          JOIN library_accounts a ON a.uid=m.uid WHERE m.uid=? AND m.msg_uuid=?`);
        const deleted = db.prepare('SELECT 1 FROM deletions WHERE kind=? AND key_hash=?');
        const updatesByFields = new Map(), validated = new Map(), outcomes = new Map(), accounts = new Set();
        for await (const line of readJsonLines(updates)) {
          const row = validateUpdate(JSON.parse(line));
          const key = JSON.stringify([row.uid, row.msg_uuid]);
          const old = current.get(row.uid, row.msg_uuid);
          const suppressed = old && [['message', row.msg_uuid], ['conversation', old.conversation_id]]
            .some(([kind, value]) => deleted.get(kind, deletionHash(kind, row.uid, value)));
          const stale = old && row.expected_raw_sha256 && createHash('sha256').update(old.raw_json).digest('hex') !== row.expected_raw_sha256;
          if (!old || suppressed || stale || (row.app && row.app !== old.app)) {
            if (!outcomes.has(key)) outcomes.set(key, 'skipped');
            continue;
          }
          await validateFiles(root, row, validated);
          const meta = JSON.parse(old.meta_json);
          if (!object(meta)) throw new Error('Existing message media metadata is not an object.');
          const changed = await mediaChanges(root, meta, row, validated);
          const fields = MEDIA_FIELDS.filter(field => Object.hasOwn(changed, field));
          if (fields.length) {
            const signature = fields.join(',');
            if (!updatesByFields.has(signature)) {
              // JSON1 changes only selected paths, retaining all unrelated JSON values
              // (including exact unsafe integer literals) without a JS round trip.
              updatesByFields.set(signature, db.prepare(`UPDATE messages SET meta_json=json_set(meta_json,
                ${fields.map(field => `'$.${field}',json(?)`).join(',')}) WHERE uid=? AND msg_uuid=?`));
            }
            updatesByFields.get(signature).run(...fields.map(field => JSON.stringify(changed[field])), row.uid, row.msg_uuid);
            accounts.add(row.uid);
            outcomes.set(key, 'patched');
          } else if (outcomes.get(key) !== 'patched') outcomes.set(key, 'unchanged');
        }
        if (await hashFile(updates) !== initialHash) throw new Error('Media update file changed while applying.');
        result = { status: 'completed', fingerprint, patched_messages: 0, unchanged_messages: 0,
          skipped_messages: 0, accounts: [...accounts], repeated: false };
        for (const outcome of outcomes.values()) result[`${outcome}_messages`]++;
        db.prepare(`INSERT INTO library_media_batches(fingerprint,source_id,applied_at,accounts_json,result_json)
          VALUES(?,?,?,?,?)`).run(fingerprint, String(sourceId), new Date().toISOString(), JSON.stringify(result.accounts), JSON.stringify(result));
      }
      db.exec('COMMIT');
    } catch (error) { db.exec('ROLLBACK'); throw error; }
    if (project) await finishProjections(db, root);
    const projection = db.prepare('SELECT projection_status FROM library_media_batches WHERE fingerprint=?').get(fingerprint);
    return { ...result, projection_status: projection.projection_status };
  } finally { db.close(); }
}

async function main() {
  const { values } = parseArgs({ options: {
    output: { type: 'string' }, updates: { type: 'string' }, 'source-id': { type: 'string' },
    resume: { type: 'boolean' }, 'no-project': { type: 'boolean' }, json: { type: 'boolean' },
  } });
  if (!values.output || (!values.updates && !values.resume) || (values.updates && values.resume)) {
    throw new Error('--output and either --updates or --resume are required.');
  }
  const result = values.resume ? await resumeLibraryMedia({ outputDir: values.output })
    : await patchLibraryMedia({ outputDir: values.output, updatesFile: values.updates,
      sourceId: values['source-id'] || '', project: !values['no-project'] });
  console.log(JSON.stringify(result));
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => { console.error(JSON.stringify({ status: 'error', error: error.message })); process.exitCode = 1; });
}
