# Web reader (Vue 3 + Node.js)

A local web app for reading and annotating an export produced by the Python
exporter. It adds a browser UI, full-text search and per-message notes on top of
the same export directory. The server is Node.js; **no Python runtime is needed
to run it**, and it is not a Python web frontend (Flask/FastAPI) in any form.

- Frontend: Vue 3.5 + Vite 8, built into static assets.
- Backend: Node.js >= 24.15.0, built-in `node:http` and `node:sqlite`, one
  runtime dependency (`json-bigint`).
- Storage: `OUTPUT/reader.sqlite`, a local SQLite database (not MySQL).
- Annotations: reversible edits (text override, note, hide) plus **irreversible
  permanent deletion** of single messages, selected messages and whole
  conversations.
- Browsing: paginated messages, substring search, and a month calendar of
  recorded days computed in the export timezone.

Deployment paths, Linux layout, HTTPS proxy and backups are documented
separately in [deployment.md](deployment.md).

```
web/
  index.html          Vue mount page (lang="zh-CN")
  src/                Vue source components and API client
  dist/               built app served by the Node server (generated)
  server/index.mjs    HTTP server, auth, static files
  server/store.mjs    SQLite index and annotation store
  server/media.mjs    attachment resolution and byte-range serving
  tests/              node:test suites and the synthetic export fixture
```

## Requirements

| Requirement | Notes |
| --- | --- |
| Node.js >= 24.15.0 | Provides the bundled `node:sqlite` library without a native build step. `web/package.json` declares this in `engines`. |
| npm | Installs the locked Vue/Vite and `json-bigint` dependencies. |
| Built assets | `npm run build` produces `web/dist`; the server serves it. |
| An export directory | Produced by the Python exporter (see `docs/usage.md`). |
| Python | **Not required.** |

## Why SQLite instead of MySQL

- This personal archive has few concurrent writers. SQLite keeps installation
  and backup simple. MySQL is appropriate if a future version has many users
  writing concurrently or multiple application servers sharing one database.
- The database lives next to the export, so the whole directory stays portable:
  copy or move the folder and the reader follows it.
- `node:sqlite` is part of Node, so there is no compiled dependency, no service
  to start, no credentials and no port to secure.
- The concurrency that does exist (two tabs editing one message) is handled by
  per-row optimistic revisions in `message_edits`, not by a database server.

## Data model

`OUTPUT/reader.sqlite` is created on first start. Tables:

| Table | Contents |
| --- | --- |
| `accounts` | Account id and display name. |
| `conversations` | Per-conversation aggregates (`message_count`, first/last time). |
| `messages` | One row per message: metadata plus `raw_json`. |
| `message_edits` | Annotations: `text_override`, `note`, `hidden`, `revision`, `updated_at`. |
| `deletions` | Permanent-deletion suppression: `kind`, `key_hash` (SHA-256) and `deleted_at`, primary key `(kind, key_hash)`. **No content, no names, no raw JSON.** |
| `store_meta` | `format_version`, `fingerprint`, `imported_at`, `output_dir`. |

Key properties:

- `raw_json` stores the **entire original JSONL line as TEXT**. Identifiers such
  as `9007199254740993` are preserved exactly; the UI reads them as strings and
  never re-rounds them to a JavaScript number.
- An annotation row exists only after the first edit. `text_override = NULL`
  means "show the original text"; `hidden = 1` removes the message from normal
  lists without touching the source rows and can be undone at any time.
- Each message also carries a derived, indexed `day` column (`YYYY-MM-DD`) so a
  calendar day can be filtered and counted without re-parsing timestamps. It is
  computed from the exported local time, falling back to `created_time` with the
  export timezone. The column is backfilled by an additive upgrade, so it also
  works when `messages.jsonl` is gone.
- **Original export files are never modified.** Only `reader.sqlite` (and its
  temporary `-wal`/`-shm` files) is written. Permanent deletion removes rows from
  this database and leaves `<uid>/chat.sqlite`, `messages.jsonl`, CSV/TXT/
  offline HTML and `attachments/` untouched.
- Schema upgrades are **additive**: missing columns and indexes are added and
  backfilled in place and no index table is ever dropped, so an export that has
  already pruned its JSONL keeps every record through the upgrade. The
  `message_edits` annotations and the `deletions` ledger are part of the schema
  and therefore survive every migration and reindex.
- Deleting `reader.sqlite` discards local edits, notes, hidden flags **and the
  `deletions` suppression table**, so the next import brings deleted messages
  back. It is neither a backup nor a proof of deletion.

## Import and reindex

On startup the server scans `OUTPUT/<uid>/messages.jsonl`, `statistics.json` and
`contacts.json`, hashes their contents and compares that fingerprint with the
one in `store_meta`:

| Situation | Behaviour |
| --- | --- |
| Export unchanged | Import is skipped; the existing index is served. |
| Export changed | Changed accounts are reimported; annotations are preserved. |
| `--reindex` | Forces a reimport even when the fingerprint matches. |
| JSONL removed, `reader.sqlite` present | The existing index is served as-is (useful for portable copies). |
| Source row matches a `deletions` hash | The row is skipped, so a permanently deleted message or conversation is not reimported. |

Suppression is keyed on a hash of the account and the message or conversation
identifier, so it works for unchanged, changed and forced imports, for restarts
and for a database-only migration. Deleting every message of an account does not
cause a needless reimport on the next start.

Relative media paths keep working after the export is copied. Stop the service
or take a live SQLite snapshot before copying `reader.sqlite` (see Backups).
Leaving out that database rebuilds the index and omits saved edits/notes.

## Permanent deletion versus hide and restore

Three different actions, with deliberately different blast radius:

| Action | Effect | Reversible |
| --- | --- | --- |
| Hide (`PATCH` with `hidden: true`) | Annotation only; the row stays | Yes, `hidden: false` or restore |
| Restore (`POST /api/message/restore`) | Clears text override, note and hidden; advances the revision | Yes, by editing again |
| **Delete** (`DELETE` / batch `POST`) | **SQL `DELETE`** of the message rows, their annotations, and the conversation aggregates | **No** |

Deletion removes, in one transaction:

- the `messages` rows, including `raw_json`, for the selected messages;
- the matching `message_edits` rows (override, note, hidden, revision);
- the affected `conversations` aggregates: `message_count`, `first_time` and
  `last_time` are recomputed, and conversations left with no messages are
  removed. An `accounts` row may remain with zero counts.
- Deleting a conversation removes **all** of its messages, including hidden ones.

There is no recycle bin and no copy of the deleted content anywhere in the
database. Instead one row per deleted key is written to `deletions`: the kind
(`message` or `conversation`), a SHA-256 hash over the kind, the account and the
key, and a timestamp. Because the kind is part of the hashed instance and the
primary key is `(kind, key_hash)`, the same identifier cannot collide across
kinds. Since only a digest is stored, the table cannot be used to reconstruct a
message, a name or a conversation title.

On every import the ledger is consulted twice: a message whose `(account,
msg_uuid)` hash is recorded is skipped, and so is any row whose `conversation_id`
has a recorded conversation hash. Deleting a conversation therefore also blocks
newly added source rows of that conversation, not just the rows that existed when
the deletion happened.

Limits, validation and atomicity:

- One request targets one account. A batch accepts 1 to 500 items with unique
  keys. Message items carry `expected_revision` (`>= 0`); conversation items
  carry `expected_message_count` (`>= 1`).
- Every item is validated (existence and revision/count) inside a single
  transaction before anything is removed; on any error the transaction rolls back
  and **nothing** is deleted, so the selection can be reviewed and retried.

| Condition | Status | Code |
| --- | --- | --- |
| Missing `confirm_delete: true` | `400` | `confirmation_required` |
| Empty batch, duplicate key, malformed item | `400` | `invalid_argument` |
| More than 500 items | `400` | `invalid_batch` |
| Unknown account, message or conversation | `404` | `not_found` |
| Stale `expected_revision` / `expected_message_count` | `409` | `conflict` |

- The UI asks for explicit confirmation in Chinese, naming the number of selected
  items, the database scope and that the action is irreversible, and disables the
  button while the request is in flight.
- A stale, old-style `DELETE` that carries no `confirm_delete: true` is rejected
  with `400`, never reinterpreted as a permanent delete, so a retried restore can
  never destroy data.

```
DELETE /api/message?account=<uid>&uuid=<msg-uuid>
{ "confirm_delete": true, "expected_revision": 3 }

-> { "deleted": true, "account_uid": "<uid>", "deleted_messages": 1,
     "deleted_conversations": 0, "msg_uuids": ["<msg-uuid>"], "msg_uuid": "<msg-uuid>" }
```

Response envelope: the delete routes return the result object **at the top
level** (not wrapped in `item`), while `PATCH /api/message` and
`POST /api/message/restore` return `{ item: <message> }` and `GET /api/calendar`
returns the calendar object directly. In a message batch `deleted_messages` is
how many messages were selected and `deleted_conversations` is how many
conversations were removed because they became empty. In a conversation batch
`deleted_conversations` is how many conversations were selected and
`deleted_messages` is the total number of message rows removed (hidden ones
included).

The reader UI reloads account, conversation, message and calendar totals after a
successful deletion, clamps an emptied page and clears the selection.

## Calendar and day browsing

`GET /api/calendar?account=UID&month=YYYY-MM` returns the days that actually
contain messages for the current scope:

```
{ "month": "2026-10", "timezone": "Asia/Shanghai",
  "days": [{ "date": "2026-10-01", "message_count": 42 }],
  "total": 1234, "first_date": "2025-09-02", "last_date": "2026-10-26" }
```

- `month` is required and must be a strict `YYYY-MM` (`400` `invalid_argument`
  otherwise); `conversation`, `q` and `include_hidden=1` narrow the scope exactly
  like the message list, and the counts follow those filters together with
  hide/edit/delete changes.
- `days` lists only recorded days, sorted ascending. `total` counts the matching
  messages of the selected month; `first_date` and `last_date` cover the whole
  matching scope and are `null` when nothing matches.
- Days are computed in the **export timezone** (`export_summary.json`; `UTC` only
  when it is missing or invalid), never in the browser timezone. A row uses the
  exporter's own local `time` when it is a real date and otherwise renders
  `created_time` in that zone. Two viewers in different timezones see the same
  day buckets.
- `GET /api/messages?...&date=YYYY-MM-DD` filters the paginated list to one day
  (`400` `invalid_argument` for a non-real date, including impossible ones),
  and supports the same `limit` (1-500), `offset`, `q`, `conversation` and
  `include_hidden` parameters. Clearing the date restores the broader list.

## Uploading a full export

Upload or copy the **whole export directory**, keeping this shape:

```
OUTPUT/
  export_summary.json, media_manifest.json, decoration_manifest.json, ...
  index.html, reader.css
  <account uid>/messages.jsonl, chat.sqlite, statistics.json, contacts.json, ...
  attachments/voice/, stickers/, bubbles/, inline_emoji/, audio/, audioemoji/
```

- Keep the whole export when possible. Once imported, `reader.sqlite` can
  serve without JSONL; without either the database or JSONL the messages are
  unavailable. Missing `attachments/` files mean missing media.
- The directory must be writable by the account that runs the server, because
  `reader.sqlite` is created there. Use `--database PATH` to keep the database
  outside the export instead.
- After uploading, start the server with `--data` pointing at the directory.

## Media

Media are served read-only from `attachments/` using the **export-relative**
paths recorded in `media_manifest.json` and `decoration_manifest.json`:

```
/media/attachments/voice/voice_ab12.m4a
/media/attachments/stickers/emoji_cd34.png
```

- Only paths that start with `attachments/` are reachable. Absolute paths, drive
  letters, backslashes and `..` segments are rejected.
- Symlinks and directory junctions that resolve outside `attachments/` are
  rejected, even when the link itself sits inside the directory.
- Single byte ranges are supported (`Range: bytes=0-1023`), which makes audio
  seeking work. Multiple ranges and unsatisfiable ranges return `416`.
- `If-Range` is only honoured for a strong validator; a weak one (`W/"..."`)
  returns the full file. `If-None-Match` returns `304`.
- Unknown extensions are sent as `application/octet-stream` with
  `X-Content-Type-Options: nosniff`.

Because media URLs are relative to the document base, the same build works at
the site root or behind a proxy prefix.

## HTTP API

JSON over HTTP, UTF-8, `Cache-Control: no-store` for `/api/*`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/session` | `{authenticated, auth_required, csrf_token?}` |
| POST | `/api/login` | `{token}`; sets the session cookie |
| POST | `/api/logout` | Clears the session |
| GET | `/api/accounts` | Accounts with message/conversation counts |
| GET | `/api/conversations?account=UID` | Conversations, supports `q`, `limit`, `offset` |
| GET | `/api/messages?account=UID` | Messages; `conversation`, `q`, `date=YYYY-MM-DD`, `limit` (1-500), `offset`, `include_hidden=1` |
| GET | `/api/calendar?account=UID&month=YYYY-MM` | Recorded days with counts; `conversation`, `q`, `include_hidden=1` |
| GET | `/api/message?account=UID&uuid=UUID` | One message |
| PATCH | `/api/message?account=UID&uuid=UUID` | `{text_override?, note?, hidden?, expected_revision}` |
| POST | `/api/message/restore?account=UID&uuid=UUID` | `{expected_revision}`; clears override/note/hidden |
| DELETE | `/api/message?account=UID&uuid=UUID` | `{confirm_delete:true, expected_revision}`; **permanently deletes** it |
| DELETE | `/api/conversation?account=UID&conversation=ID` | `{confirm_delete:true, expected_message_count}`; **permanently deletes** it |
| POST | `/api/messages/delete?account=UID` | `{confirm_delete:true, messages:[{msg_uuid, expected_revision}]}`; batch, max 500 |
| POST | `/api/conversations/delete?account=UID` | `{confirm_delete:true, conversations:[{id, expected_message_count}]}`; batch, max 500 |
| GET | `/api/decorations` | Decoration manifest for rendering |
| GET/HEAD | `/media/attachments/...` | Attachment bytes with range support |

Authentication and safety:

- With a token configured, every `/api/*` and `/media/*` request needs the
  session cookie. Mutations also need `X-CSRF-Token` from `/api/session`.
- `expected_revision` is mandatory and starts at `0`; a stale value returns
  `409` and nothing is written. Restore also bumps the revision, so an old
  revision cannot be replayed afterwards (no ABA).
- Every destructive route additionally requires `confirm_delete: true` in the
  body; omitting it is a `400` (`confirmation_required`), and a batch that fails
  validation deletes nothing at all. The ledger is consulted during import, so a
  suppressed message hash or conversation id is never reinserted.
- A mutating request whose `Origin` does not match the forwarded `Host` is
  rejected with `403`.
- There is no raw SQL endpoint and no way to read arbitrary files.

## Running it

```powershell
cd web
npm ci
npm run build
npm start -- --data "E:\path\to\Output"
```

Linux layout, production `npm ci --omit=dev`, systemd, HTTPS proxy and backup
procedures are in [deployment.md](deployment.md).

Options:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--data` | required | Export directory to read |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8765` | TCP port (`0` picks a free port) |
| `--token` | `DUOSHAN_ACCESS_TOKEN` | Required when `--host` is not loopback |
| `--database` | `OUTPUT/reader.sqlite` | Put the database elsewhere |
| `--reindex` | off | Force a reimport |

For frontend work, run the API and the Vite dev server separately:

```powershell
npm start -- --data "E:\path\to\Output"   # API on 127.0.0.1:8765
npm run dev                               # Vite on 127.0.0.1:5173, proxies /api and /media
```

`npm run dev` proxies `/api` and `/media` to `127.0.0.1:8765`, so the source
build behaves like the packaged one.

## Network exposure

- The default bind is loopback only.
- Binding a non-loopback address **requires** a token; the process refuses to
  start without one.
- With no token, requests whose `Host` header is not loopback are rejected.
  With a token, a public `Host` is accepted, which is what a reverse proxy
  sends.
- This is a single-user reader, not a multi-user service. Expose it over a
  trusted LAN, VPN or an authenticating reverse proxy, not the open internet.

## Deployment behind a reverse proxy

Set `Host` and `X-Forwarded-Proto` explicitly; the `Origin` check compares
against `Host`, and the cookie is marked `Secure` only when the proxy reports
`https`.

```nginx
server {
    listen 443 ssl;
    server_name reader.example.com;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

- Always forward `Host`; without it the `Origin`/`Host` comparison fails and
  saves are rejected with `403`.
- Forward `X-Forwarded-Proto: https` so the session cookie gets `Secure`.
- For a prefix such as `/chat/`, strip the prefix before proxying. The default
  Vite `base: './'` and relative API/media URLs work unchanged. Redirect
  `/chat` to `/chat/` so browser URL resolution uses the correct base.

```nginx
location = /chat { return 308 /chat/; }
location /chat/ {
    proxy_pass http://127.0.0.1:8765/;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## Built assets versus source

- `web/src/**` and `web/index.html` are the source.
- `web/dist/**` is generated by `npm run build` and is what the Node server
  serves for every non-API, non-media path. Run the build before `npm start`.
- Do not hand-edit `dist`; it is overwritten on the next build.
- Bundles are local: no CDN, no runtime compilation, no network fetches.

## Backups and SQLite safety

The reader keeps SQLite in WAL mode, so `reader.sqlite` alone is **not** a
consistent copy while the server is running.

- Simplest safe backup: stop the server, then copy the whole export directory
  (which includes `reader.sqlite` and any `-wal`/`-shm` sidecars).
- Live snapshot without stopping:

  ```powershell
  @'
  import { DatabaseSync } from 'node:sqlite';
  const db = new DatabaseSync('reader.sqlite');
  try { db.exec("VACUUM INTO 'reader-backup.sqlite'"); }
  finally { db.close(); }
  '@ | node --input-type=module
  ```

- On Windows an open database file cannot be replaced (`PermissionError` /
  `WinError 5`); stop the process before restoring or overwriting it.
- Deleting `reader.sqlite` never touches the export, but it loses local edits,
  notes, hidden flags and the `deletions` suppression rows. Deleted messages are
  then reimported on the next start. Keep the database (or a snapshot of it) if
  permanent deletions must stay permanent.

## Tests

```powershell
cd web
npm run build   # build.test.mjs inspects dist and fails if it is missing
npm test
```

`npm test` runs `node --test tests/*.test.mjs`, the Node built-in runner with no
external test dependency. Everything is offline and loopback-only; the suites
build a synthetic export with placeholder account ids, real media bytes, and
identifiers above `2^53` to prove exact-`raw_json` behaviour.

| Suite | Covers |
| --- | --- |
| `tests/fixture.test.mjs` | The synthetic export itself: English file names, exact big integers, media bytes, hash stability, copy fidelity. |
| `tests/store.test.mjs` | Import and counts, `raw_json` precision, ordering and pagination, limit bounds, search over edited text, hidden handling, revision conflicts and restore, reindex keeping edits, zero writes to original files, SQL-injection safety, serving with JSONL removed. |
| `tests/media.test.mjs` | Contained resolution, traversal/absolute/drive/symlink/junction escapes, MIME mapping, HEAD, byte ranges, `416`, `If-Range` weak versus strong, `304`. |
| `tests/http.test.mjs` | Session and login/logout, `401`/`403` rules, CSRF, proxied `Host` acceptance, `Origin` versus forwarded `Host`, API shapes, error shape, no raw SQL, static isolation, media over HTTP. |
| `tests/delete.test.mjs` | Row-level SQL absence after delete, annotation removal, conversation aggregate recomputation, batch atomicity on a stale item, `confirm_delete` enforcement, hash-only `deletions` rows, suppression across unchanged/changed/forced import and reopen, database-only migration. |
| `tests/calendar.test.mjs` | Day buckets in the export timezone, month filtering with `q`/`conversation`/`include_hidden`, `total` and `first_date`/`last_date` scope, day-filtered pagination, count refresh after hide/edit/delete. |
| `tests/frontend.test.mjs` | Calendar math, selection across pages, single/batch confirmation/cancellation/retry, page clamping, annotation/calendar refresh, account/query races, active-conversation headings across sidebar pages, and clearing a conversation emptied by message deletion. |
| `tests/build.test.mjs` | Vue 3 + Vite manifest, `lang="zh-CN"`, relative `base`, local bundles only. |
| `tests/client.test.mjs` | Chinese error messages, stale account responses, out-of-order conversation searches. |

The suite includes canonical default-port origins, rejecting forwarded-host
overrides, secure logout cookies and a paused media stream during shutdown.
The reader 0.2.0 Node 24.15 run passed 140 tests with 0 failures and 0 skips;
`npm test` prints the current count for the checked-out revision.
Tests that need to create a symlink or a directory junction skip only
where the platform forbids creating one; a missing module or build fails
instead of skipping.

## Language boundary

- The browser UI is Simplified Chinese (`<html lang="zh-CN">`).
- Generated directory names, file names, database tables and columns, API
  fields, manifest keys and console output are English.
- Chat text, nicknames and file names from the export are never translated.

## Related documentation

- `docs/deployment.md` - Linux layout, install, HTTPS proxy, backups and the
  deletion boundary.
- `docs/usage.md` - producing the export the reader consumes.
- `docs/export-format.md` - what each export file contains.
- `docs/verification.md` - end-to-end verification of the exporter.
- `docs/troubleshooting.md` - common export and runtime problems.

## 快速开始

```powershell
cd web
npm ci               # 安装锁定版本的 Vue 3 / Vite / json-bigint
npm run build        # 生成 web/dist
npm start -- --data "E:\导出目录"    # 默认 http://127.0.0.1:8765/
```

- 需要 Node.js 24.15 或更高版本，使用内置 `node:sqlite`，**不需要 Python**。
- 默认只监听本机；要给局域网访问，请加 `--host 0.0.0.0 --token 你的口令`。
- 聊天记录、备注和编辑都存在 `OUTPUT/reader.sqlite`，原始导出文件不会被修改。
- 网页里的「永久删除」只删 `reader.sqlite` 里的行；`<uid>/chat.sqlite`、`messages.jsonl`、
  CSV/TXT/离线页与 `attachments/` 媒体都不受影响。删除 `reader.sqlite` 会丢失编辑、
  备注、隐藏状态以及删除抑制记录，被删消息会在下次导入时重新出现。
- 备份前先停止服务，或者用 `VACUUM INTO` 生成快照。Linux 部署见
  [deployment.md](deployment.md)。
