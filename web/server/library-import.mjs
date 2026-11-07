/** Additive imports into a portable reader database; never clear old history. */
import fs from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { isDeepStrictEqual, parseArgs } from 'node:util';
import { pathToFileURL } from 'node:url';
import { APP_PREFIX, accountDirectories } from './account-layout.mjs';
import { ChatStore, accountName, dayFromTimeText, deletionHash, hashFile,
  localMediaMap, normalizeRow, readJsonFile, readJsonLines } from './store.mjs';

const SCHEMA = `
CREATE TABLE IF NOT EXISTS import_message_aliases (
  uid TEXT NOT NULL, source_key TEXT NOT NULL, msg_uuid TEXT NOT NULL,
  conversation_id TEXT NOT NULL, PRIMARY KEY(uid,source_key));
CREATE TABLE IF NOT EXISTS import_conversation_aliases (
  uid TEXT NOT NULL, source_key TEXT NOT NULL, conversation_id TEXT NOT NULL,
  PRIMARY KEY(uid,source_key));
CREATE TABLE IF NOT EXISTS import_batches (
  fingerprint TEXT PRIMARY KEY, app TEXT NOT NULL, source_id TEXT NOT NULL,
  imported_at TEXT NOT NULL, accounts_json TEXT NOT NULL, result_json TEXT NOT NULL,
  projection_status TEXT NOT NULL DEFAULT 'pending');
`;

const COLUMNS = ['uid', 'msg_uuid', 'conversation_id', 'conversation_name', 'sender',
  'sender_name', 'created_time', 'time', 'day', 'message_type', 'type_name',
  'text_original', 'text_lower', 'raw_json', 'meta_json'];

/** Progress is observational: it must never turn a committed transaction into a failure. */
function progressStage(progress, stage, message, { total = null, unit = '条' } = {}) {
  let completed = 0, last = -Infinity;
  const emit = (state = 'running', force = false) => {
    if (typeof progress !== 'function') return;
    const now = performance.now();
    if (!force && now - last < 2000) return;
    last = now;
    const boundedTotal = total !== null && completed <= total ? total : null;
    const text = `${message}：${completed.toLocaleString('zh-CN')}`
      + (boundedTotal === null ? '' : `/${boundedTotal.toLocaleString('zh-CN')}`)
      + ` ${unit}` + (state === 'failed' ? '；中断' : state === 'completed' ? '；阶段完成' : '');
    try { progress({ message: text, stage, completed, total: boundedTotal, unit, state }); }
    catch { /* A display callback cannot participate in storage transactions. */ }
  };
  emit('running', true);
  return {
    update(value) { completed = value; emit(); },
    setTotal(value) { total = value; emit('running', true); },
    finish() { emit('completed', true); },
    fail() { emit('failed', true); },
  };
}

function checkApp(app) {
  if (!Object.hasOwn(APP_PREFIX, app)) throw new Error('Choose wechat, duoshan, or xhs.');
  return app;
}

function appForAccount(root, account) {
  if (account.app) return account.app;
  const stats = readJsonFile(path.join(root, account.directory, 'statistics.json'));
  const label = String(stats?.app || '').toLowerCase();
  if (label.includes('wechat')) return 'wechat';
  if (label.includes('xiaohongshu') || label === 'xhs') return 'xhs';
  if (label.includes('duoshan')) return 'duoshan';
  if (/^\d+$/.test(account.uid)) return 'duoshan';
  if (account.uid.length === 24) return 'xhs';
  if (account.uid.length === 32) return 'wechat';
  throw new Error(`Cannot identify the app for account directory ${account.directory}`);
}

async function openLibrary(outputDir) {
  fs.mkdirSync(outputDir, { recursive: true });
  const store = new ChatStore(outputDir);
  try { await store.initialize(); } finally { store.close(); }
  const db = new DatabaseSync(path.join(outputDir, 'reader.sqlite'));
  db.exec('PRAGMA busy_timeout=30000');
  db.exec('PRAGMA journal_mode=WAL');
  db.exec(SCHEMA);
  return db;
}

function setMeta(db, key, value) {
  db.prepare('INSERT INTO store_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(key, value);
}

function assertSeparate(source, output) {
  for (const [a, b] of [[source, output], [output, source]]) {
    const relative = path.relative(a, b);
    if (!relative || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative))) {
      throw new Error('Import source and library must be separate directories.');
    }
  }
}

/** Rename only account folders. Account IDs, message IDs and references stay stable. */
export async function organizeLibrary({ outputDir, apps = {}, project = false, progress } = {}) {
  const root = path.resolve(outputDir);
  const db = await openLibrary(root);
  try {
    const accounts = accountDirectories(root);
    const plan = accounts.map(account => {
      const app = checkApp(apps[account.uid] || appForAccount(root, account));
      return { ...account, app, destination: `${APP_PREFIX[app]}_${account.uid}` };
    });
    for (const account of plan) {
      const destination = path.join(root, account.destination);
      if (account.directory !== account.destination && fs.existsSync(destination)) {
        throw new Error(`Destination already exists: ${destination}`);
      }
    }
    const moved = [];
    const status = progressStage(progress, 'reader-organize', '整理阅读器账号目录', { total: plan.length, unit: '个账号' });
    let processed = 0;
    db.exec('BEGIN IMMEDIATE');
    try {
      const save = db.prepare(`INSERT INTO library_accounts(uid,app,directory) VALUES(?,?,?)
        ON CONFLICT(uid) DO UPDATE SET app=excluded.app,directory=excluded.directory`);
      for (const account of plan) {
        if (account.directory !== account.destination) {
          fs.renameSync(path.join(root, account.directory), path.join(root, account.destination));
          moved.push(account);
        }
        save.run(account.uid, account.app, account.destination);
        status.update(++processed);
      }
      setMeta(db, 'library_mode', 'incremental-v1');
      db.exec('COMMIT');
    } catch (error) {
      status.fail();
      db.exec('ROLLBACK');
      for (const account of moved.reverse()) fs.renameSync(path.join(root, account.destination), path.join(root, account.directory));
      throw error;
    }
    status.finish();
    if (project) await projectAccounts(db, root, plan.map(account => account.uid), { progress });
    return { status: 'completed', organized: moved.length, accounts: plan.map(({ uid, app, destination }) => ({ uid, app, directory: destination })) };
  } finally { db.close(); }
}

function dayFor(row, zone) {
  const text = dayFromTimeText(row.time);
  if (text) return text;
  if (row.created_time === null || !Number.isFinite(Number(row.created_time))) return '';
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: zone,
    year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(Number(row.created_time)));
  return ['year', 'month', 'day'].map(type => parts.find(part => part.type === type)?.value || '').join('-');
}

function mergedMeta(previous, incoming) {
  const old = previous ? JSON.parse(previous) : {};
  const next = JSON.parse(incoming);
  const combined = { ...old, ...next };
  combined.local_media = [...new Set([...(old.local_media || []), ...(next.local_media || [])])];
  if (old.media?.status === 'ok' && next.media?.status !== 'ok') combined.media = old.media;
  return JSON.stringify(combined);
}

function sameMetadata(a, b) {
  return a === b || isDeepStrictEqual(JSON.parse(a), JSON.parse(b));
}

async function sourceFingerprint(source, accounts, app, progress) {
  const hash = createHash('sha256').update('library-import-v1\0' + app + '\0');
  const files = [];
  for (const account of accounts) {
    for (const name of ['messages.jsonl', 'statistics.json', 'contacts.json']) {
      const file = path.join(source, account.directory, name);
      if (fs.existsSync(file)) files.push({ file, label: account.uid + '/' + name });
    }
  }
  for (const name of ['export_summary.json', 'media_manifest.json']) {
    const file = path.join(source, name);
    if (fs.existsSync(file)) files.push({ file, label: name });
  }
  const total = files.reduce((sum, item) => sum + fs.statSync(item.file).size, 0);
  const status = progressStage(progress, 'reader-fingerprint', '校验阅读器导入文件', { total, unit: '字节' });
  let completed = 0;
  try {
    for (const { file, label } of files) {
      const digest = createHash('sha256');
      for await (const chunk of fs.createReadStream(file)) {
        digest.update(chunk);
        completed += chunk.length;
        status.update(completed);
      }
      hash.update(label + '\0' + digest.digest('hex') + '\n');
    }
    status.finish();
    return hash.digest('hex');
  } catch (error) { status.fail(); throw error; }
}

function ensureTargetAccount(db, root, account, app, source, preserveExisting) {
  const known = db.prepare('SELECT app,directory FROM library_accounts WHERE uid=?').get(account.uid);
  if (known && known.app !== app) throw new Error(`Account ${account.uid} belongs to ${known.app}, not ${app}`);
  const existing = accountDirectories(root).find(item => item.uid === account.uid);
  if (existing && appForAccount(root, existing) !== app) throw new Error(`Account directory app conflict: ${account.uid}`);
  const directory = existing?.directory || `${APP_PREFIX[app]}_${account.uid}`;
  const stats = readJsonFile(path.join(source, account.directory, 'statistics.json'));
  const contacts = readJsonFile(path.join(source, account.directory, 'contacts.json'));
  const name = accountName(account.uid, stats, contacts);
  db.prepare(`INSERT INTO accounts(uid,name) VALUES(?,?) ON CONFLICT(uid) DO UPDATE
    SET name=CASE WHEN ?=1 AND accounts.name<>'' THEN accounts.name
      WHEN excluded.name<>'' THEN excluded.name ELSE accounts.name END`).run(account.uid, name, preserveExisting ? 1 : 0);
  db.prepare(`INSERT INTO library_accounts(uid,app,directory) VALUES(?,?,?) ON CONFLICT(uid)
    DO UPDATE SET app=excluded.app,directory=excluded.directory`).run(account.uid, app, directory);
}

function rebuildConversations(db, uid, progress) {
  const status = progressStage(progress, 'reader-conversations', '更新阅读器会话统计', { unit: '个会话' });
  let completed = 0;
  try {
    const rows = db.prepare(`SELECT conversation_id,COUNT(*) AS message_count,MIN(created_time) AS first_time,
      MAX(created_time) AS last_time FROM messages WHERE uid=? GROUP BY conversation_id`).all(uid);
    const nameQuery = db.prepare(`SELECT conversation_name FROM messages WHERE uid=? AND conversation_id=?
      AND conversation_name<>'' ORDER BY created_time DESC,msg_uuid DESC LIMIT 1`);
    const save = db.prepare(`INSERT INTO conversations(uid,conversation_id,name,name_lower,message_count,first_time,last_time)
      VALUES(?,?,?,?,?,?,?) ON CONFLICT(uid,conversation_id) DO UPDATE SET name=excluded.name,name_lower=excluded.name_lower,
      message_count=excluded.message_count,first_time=excluded.first_time,last_time=excluded.last_time`);
    status.setTotal(rows.length);
    for (const row of rows) {
      const name = nameQuery.get(uid, row.conversation_id)?.conversation_name || '';
      save.run(uid, row.conversation_id, name, name.toLowerCase(), row.message_count, row.first_time, row.last_time);
      status.update(++completed);
    }
    status.finish();
  } catch (error) { status.fail(); throw error; }
}

function validateLocalMedia(meta, root) {
  const candidates = [...(Array.isArray(meta.local_media) ? meta.local_media : []), meta.media?.status === 'ok' ? meta.media.path : ''];
  for (const relative of candidates.filter(Boolean)) {
    if (typeof relative !== 'string' || !relative.startsWith('attachments/') || relative.includes('\\')
      || relative.split('/').some(part => part === '..' || part === '.')) throw new Error('Invalid local media path in import');
    const file = path.resolve(root, relative);
    const canonicalRoot = fs.realpathSync(root);
    const attachments = fs.realpathSync(path.join(root, 'attachments'));
    const canonicalFile = fs.realpathSync(file);
    if (!attachments.startsWith(canonicalRoot + path.sep)
      || !canonicalFile.startsWith(attachments + path.sep) || !fs.statSync(file).isFile()) {
      throw new Error(`Local media is unavailable or outside library: ${relative}`);
    }
  }
}

/** Import pre-copied attachments and reader-format JSONL in one SQL transaction. */
export async function importLibrary({ sourceDir, outputDir, app, sourceId = '', project = true, preserveExisting = false, progress } = {}) {
  checkApp(app);
  const source = fs.realpathSync(path.resolve(sourceDir));
  const root = path.resolve(outputDir);
  assertSeparate(source, root);
  const accounts = accountDirectories(source).filter(account => fs.existsSync(path.join(source, account.directory, 'messages.jsonl')));
  if (!accounts.length) throw new Error('No account messages.jsonl files found in import source.');
  for (const account of accounts) if (account.app && account.app !== app) throw new Error('Import contains an account from a different app.');
  const baseFingerprint = await sourceFingerprint(source, accounts, app, progress);
  const fingerprint = preserveExisting ? createHash('sha256').update(baseFingerprint + ':preserve').digest('hex') : baseFingerprint;
  const db = await openLibrary(root);
  try {
    const prior = db.prepare('SELECT * FROM import_batches WHERE fingerprint=?').get(fingerprint);
    if (prior) {
      if (project && prior.projection_status !== 'completed') {
        await projectAccounts(db, root, JSON.parse(prior.accounts_json), { progress });
        db.prepare("UPDATE import_batches SET projection_status='completed' WHERE fingerprint=?").run(fingerprint);
      }
      return { ...JSON.parse(prior.result_json), repeated: true, inserted: 0, updated: 0 };
    }
    const summary = readJsonFile(path.join(source, 'export_summary.json')) || {};
    const zone = summary.timezone || db.prepare("SELECT value FROM store_meta WHERE key='timezone'").get()?.value || 'UTC';
    new Intl.DateTimeFormat('en', { timeZone: zone });
    const localMedia = localMediaMap(source);
    const previous = db.prepare('SELECT * FROM messages WHERE uid=? AND msg_uuid=?');
    const updateMedia = db.prepare('UPDATE messages SET meta_json=? WHERE uid=? AND msg_uuid=?');
    const lookupAlias = db.prepare('SELECT msg_uuid,conversation_id FROM import_message_aliases WHERE uid=? AND source_key=?');
    const lookupConversation = db.prepare('SELECT conversation_id FROM import_conversation_aliases WHERE uid=? AND source_key=?');
    const saveAlias = db.prepare('INSERT OR IGNORE INTO import_message_aliases(uid,source_key,msg_uuid,conversation_id) VALUES(?,?,?,?)');
    const saveConversation = db.prepare('INSERT OR IGNORE INTO import_conversation_aliases(uid,source_key,conversation_id) VALUES(?,?,?)');
    const deleted = db.prepare('SELECT 1 FROM deletions WHERE kind=? AND key_hash=?');
    const save = db.prepare(`INSERT INTO messages(${COLUMNS.join(',')}) VALUES(${COLUMNS.map(() => '?').join(',')})
      ON CONFLICT(uid,msg_uuid) DO UPDATE SET ${COLUMNS.slice(2).map(column => `${column}=excluded.${column}`).join(',')}`);
    const result = { status: 'completed', fingerprint, app, accounts: accounts.map(account => account.uid),
      inserted: 0, updated: 0, unchanged: 0, suppressed: 0, repeated: false };
    const merging = progressStage(progress, 'reader-merge', '合并阅读器消息（事务提交后生效）');
    let processed = 0;
    db.exec('BEGIN IMMEDIATE');
    try {
      for (const account of accounts) {
        ensureTargetAccount(db, root, account, app, source, preserveExisting);
        for await (const line of readJsonLines(path.join(source, account.directory, 'messages.jsonl'))) {
          const row = normalizeRow(line, localMedia);
          if (!row.msg_uuid) throw new Error('Message has no stable identifier.');
          const meta = JSON.parse(row.meta_json);
          const sourceKey = String(meta.source_key || row.msg_uuid);
          const sourceConversation = String(meta.source_conversation || row.conversation_id);
          const alias = lookupAlias.get(account.uid, sourceKey);
          const conversation = lookupConversation.get(account.uid, sourceConversation);
          const incomingUuid = row.msg_uuid;
          const incomingConversation = row.conversation_id;
          if (alias) { row.msg_uuid = alias.msg_uuid; row.conversation_id = alias.conversation_id; }
          else if (conversation) row.conversation_id = conversation.conversation_id;
          const isDeleted = [['message', row.msg_uuid], ['message', incomingUuid],
            ['conversation', row.conversation_id], ['conversation', incomingConversation]]
            .some(([kind, key]) => key && deleted.get(kind, deletionHash(kind, account.uid, key)));
          if (isDeleted) { result.suppressed++; merging.update(++processed); continue; }
          validateLocalMedia(meta, root);
          const old = previous.get(account.uid, row.msg_uuid);
          if (old && preserveExisting) {
            const oldMeta = JSON.parse(old.meta_json);
            const nextMeta = JSON.parse(row.meta_json);
            const added = (nextMeta.local_media || []).filter(relative => !(oldMeta.local_media || []).includes(relative));
            const betterResource = oldMeta.media?.status !== 'ok' && nextMeta.media?.status === 'ok';
            if (added.length || betterResource) {
              oldMeta.local_media = [...new Set([...(oldMeta.local_media || []), ...added])];
              if (betterResource) oldMeta.media = nextMeta.media;
              updateMedia.run(JSON.stringify(oldMeta), account.uid, row.msg_uuid);
              result.updated++;
            } else result.unchanged++;
            saveAlias.run(account.uid, sourceKey, row.msg_uuid, row.conversation_id);
            if (sourceConversation) saveConversation.run(account.uid, sourceConversation, row.conversation_id);
            merging.update(++processed);
            continue;
          }
          const values = { ...row, uid: account.uid, raw_json: line, day: dayFor(row, zone),
            meta_json: mergedMeta(old?.meta_json, row.meta_json) };
          if (old && old.raw_json === line && old.meta_json === values.meta_json
            && COLUMNS.filter(column => !['raw_json', 'meta_json'].includes(column)).every(column => old[column] === values[column])) {
            result.unchanged++;
          } else {
            save.run(...COLUMNS.map(column => values[column]));
            result[old ? 'updated' : 'inserted']++;
          }
          saveAlias.run(account.uid, sourceKey, row.msg_uuid, row.conversation_id);
          if (sourceConversation) saveConversation.run(account.uid, sourceConversation, row.conversation_id);
          merging.update(++processed);
        }
        rebuildConversations(db, account.uid, progress);
      }
      setMeta(db, 'library_mode', 'incremental-v1');
      setMeta(db, 'imported_at', new Date().toISOString());
      setMeta(db, 'output_dir', root);
      if (!db.prepare("SELECT value FROM store_meta WHERE key='timezone'").get()) setMeta(db, 'timezone', zone);
      db.prepare(`INSERT INTO import_batches(fingerprint,app,source_id,imported_at,accounts_json,result_json)
        VALUES(?,?,?,?,?,?)`).run(fingerprint, app, sourceId, new Date().toISOString(), JSON.stringify(result.accounts), JSON.stringify(result));
      db.exec('COMMIT');
    } catch (error) { merging.fail(); db.exec('ROLLBACK'); throw error; }
    merging.finish();
    if (project) {
      await projectAccounts(db, root, result.accounts, { progress });
      db.prepare("UPDATE import_batches SET projection_status='completed' WHERE fingerprint=?").run(fingerprint);
    }
    return result;
  } finally { db.close(); }
}

/** Retain previous projections before publishing; retries recover from the DB. */
async function publish(root, target, write) {
  const version = new Date().toISOString().replace(/[:.]/g, '-') + '-' + randomUUID();
  const pending = path.join(root, '.import-history', version, path.relative(root, target));
  fs.mkdirSync(path.dirname(pending), { recursive: true });
  write(pending);
  if (fs.existsSync(target) && await hashFile(target) === await hashFile(pending)) return;
  fs.mkdirSync(path.dirname(target), { recursive: true });
  if (fs.existsSync(target)) fs.renameSync(target, pending + '.previous');
  fs.renameSync(pending, target);
}

export async function projectAccounts(db, root, uids, { progress } = {}) {
  const status = progressStage(progress, 'reader-project', '写出阅读器账号文件');
  let completed = 0;
  try {
    const localMedia = localMediaMap(root);
    const lookupAccount = db.prepare('SELECT a.uid,a.name,l.app,l.directory FROM accounts a JOIN library_accounts l ON a.uid=l.uid WHERE a.uid=?');
    const accounts = uids.map(uid => lookupAccount.get(uid)).filter(Boolean);
    const countMessages = db.prepare('SELECT COUNT(*) AS n FROM messages WHERE uid=?');
    status.setTotal(accounts.reduce((sum, account) => sum + Number(countMessages.get(account.uid).n), 0));
    for (const account of accounts) {
      const uid = account.uid;
      const directory = path.join(root, account.directory);
      await publish(root, path.join(directory, 'messages.jsonl'), file => {
        const fd = fs.openSync(file, 'wx');
        try {
          for (const row of db.prepare('SELECT * FROM messages WHERE uid=? ORDER BY created_time,msg_uuid').iterate(uid)) {
            const normalized = normalizeRow(row.raw_json, localMedia);
            const fields = ['msg_uuid', 'conversation_id', 'conversation_name', 'sender', 'sender_name',
              'created_time', 'time', 'message_type', 'type_name', 'text_original', 'text_lower', 'meta_json'];
            let line = row.raw_json;
            if (!fields.every(key => key === 'meta_json' ? sameMetadata(normalized[key], row[key]) : normalized[key] === row[key])) {
              const projection = { version: 1, ...Object.fromEntries(fields.map(key => [key, row[key]])) };
              line = line.trimEnd().slice(0, -1) + ',"_library_projection":' + JSON.stringify(projection) + '}';
            }
            fs.writeSync(fd, line + '\n');
            status.update(++completed);
          }
          fs.fsyncSync(fd);
        } finally { fs.closeSync(fd); }
      });
      const count = countMessages.get(uid).n;
      await publish(root, path.join(directory, 'statistics.json'), file => fs.writeFileSync(file,
        JSON.stringify({ app: account.app, account_uid: uid, nickname: account.name, message_count: count }, null, 2), { flag: 'wx' }));
    }
    status.finish();
  } catch (error) { status.fail(); throw error; }
}

async function main() {
  const { values } = parseArgs({ options: {
    source: { type: 'string' }, output: { type: 'string' }, app: { type: 'string' },
    'source-id': { type: 'string' }, organize: { type: 'boolean' }, json: { type: 'boolean' },
    'no-project': { type: 'boolean' },
    'preserve-existing': { type: 'boolean' },
  } });
  if (!values.output) throw new Error('--output is required.');
  const progress = event => process.stderr.write('CHAT_ARCHIVE_PROGRESS ' + JSON.stringify(event) + '\n');
  const result = values.organize ? await organizeLibrary({ outputDir: values.output, progress })
    : await importLibrary({ sourceDir: values.source, outputDir: values.output, app: values.app,
      sourceId: values['source-id'] || '', project: !values['no-project'], preserveExisting: values['preserve-existing'], progress });
  console.log(JSON.stringify(result));
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => { console.error(JSON.stringify({ status: 'error', error: error.message })); process.exitCode = 1; });
}
