# Web reader (Vue 3 + Node.js)

A local web app for reading and annotating an export produced by the Python
exporter. It adds a browser UI, full-text search and per-message notes on top of
the same export directory. The server is Node.js; **no Python runtime is needed
to run it**, and it is not a Python web frontend (Flask/FastAPI) in any form.

- Frontend: Vue 3.5 + Vite 8, built into static assets.
- Backend: Node.js >= 24.15.0, built-in `node:http` and `node:sqlite`, one
  runtime dependency (`json-bigint`).
- Storage: `OUTPUT/reader.sqlite`, a local SQLite database (not MySQL).

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
| `store_meta` | `format_version`, `fingerprint`, `imported_at`, `output_dir`. |

Key properties:

- `raw_json` stores the **entire original JSONL line as TEXT**. Identifiers such
  as `9007199254740993` are preserved exactly; the UI reads them as strings and
  never re-rounds them to a JavaScript number.
- An annotation row exists only after the first edit. `text_override = NULL`
  means "show the original text"; `hidden = 1` removes the message from normal
  lists without touching the source rows.
- **Original export files are never modified.** Only `reader.sqlite` (and its
  temporary `-wal`/`-shm` files) is written. Delete `reader.sqlite` to rebuild
  from the export; that discards local edits and notes.
- A format upgrade recreates the derived tables but keeps `message_edits`, so
  annotations survive.

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

Relative media paths keep working after the export is copied. Stop the service
or take a live SQLite snapshot before copying `reader.sqlite` (see Backups).
Leaving out that database rebuilds the index and omits saved edits/notes.

## Uploading a full export

Upload or copy the **whole export directory**, keeping this shape:

```
OUTPUT/
  export_summary.json, media_manifest.json, decoration_manifest.json, ...
  index.html, reader.css
  <account uid>/messages.jsonl, chat.sqlite, statistics.json, contacts.json, ...
  attachments/voice/, stickers/, bubbles/, inline_emoji/, media/
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
| GET | `/api/messages?account=UID` | Messages; `conversation`, `q`, `limit` (1-500), `offset`, `include_hidden=1` |
| GET | `/api/message?account=UID&uuid=UUID` | One message |
| PATCH | `/api/message?account=UID&uuid=UUID` | `{text_override?, note?, hidden?, expected_revision}` |
| DELETE | `/api/message?account=UID&uuid=UUID` | `{expected_revision}`; restores the original |
| GET | `/api/decorations` | Decoration manifest for rendering |
| GET/HEAD | `/media/attachments/...` | Attachment bytes with range support |

Authentication and safety:

- With a token configured, every `/api/*` and `/media/*` request needs the
  session cookie. Mutations also need `X-CSRF-Token` from `/api/session`.
- `expected_revision` is mandatory and starts at `0`; a stale value returns
  `409` and nothing is written. `DELETE` also bumps the revision, so an old
  revision cannot be replayed afterwards.
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
- `reader.sqlite` is disposable: deleting it only loses local annotations and
  the import cache, never the export.

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
| `tests/build.test.mjs` | Vue 3 + Vite manifest, `lang="zh-CN"`, relative `base`, local bundles only. |
| `tests/client.test.mjs` | Chinese error messages, stale account responses, out-of-order conversation searches. |

The suite includes canonical default-port origins, rejecting forwarded-host
overrides, secure logout cookies and a paused media stream during shutdown.
The verified Node 24.15 run passed **66 tests, 0 failures, 0 skips**.
Tests that need to create a symlink or a directory junction skip only
where the platform forbids creating one; a missing module or build fails
instead of skipping.

## Language boundary

- The browser UI is Simplified Chinese (`<html lang="zh-CN">`).
- Generated directory names, file names, database tables and columns, API
  fields, manifest keys and console output are English.
- Chat text, nicknames and file names from the export are never translated.

## Related documentation

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
- 备份前先停止服务，或者用 `VACUUM INTO` 生成快照。
