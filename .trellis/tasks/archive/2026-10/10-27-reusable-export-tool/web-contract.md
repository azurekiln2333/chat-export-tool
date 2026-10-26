# Vue SQLite Reader Extension

User steering: build a Vue frontend for browsing/editing, explain SQLite vs
MySQL and preserve media after server upload. Web serving must not require
Python. Use Vue 3 + Vite, Node.js >=24.15 with built-in node:sqlite, and SQLite.
The existing extraction CLI remains an independent Python package.

## Ownership

- Coordinator: web/package.json, lockfile/install, vite.config.js, server/index.mjs
  (HTTP/API/auth orchestration), integration and final review.
- pi p2: web/server/store.mjs (central reader.sqlite import/query/edit layer).
- pi p4: web/src/** and web/index.html (Vue interface and media/bubble rendering).
- pi p5: web/server/media.mjs (contained media paths, MIME, HTTP ranges).
- pi p6: web/tests/** and docs/web-reader.md (synthetic tests and deployment).
Do not edit another owner's files without coordination. No new agents needed.

## Storage and edits

Create OUTPUT/reader.sqlite on first web start/import. It contains accounts,
conversations, normalized messages including original text, raw_json as TEXT
(exact source JSONL line, avoiding numeric ID rounding), meta_json, and a
separate message_edits table. All identifiers exposed to JS are strings.
Import messages.jsonl with lossless integer parsing or equivalent preservation.
Never mutate original account chat.sqlite, JSONL, HTML, media or backup.
On reindex preserve edits; store an input fingerprint and skip unchanged input.
If reader.sqlite already exists and source JSONL is absent, it can still serve
its imported data. Missing assets remain visibly unavailable.

Edits: nullable text_override (null restores original text), note string,
hidden boolean, revision integer, updated_at ISO string. PATCH requires
expected_revision (initial 0); stale revision => 409. Restore uses DELETE with
expected_revision and clears text/note/hidden but advances revision (no ABA).
Hide is reversible and never deletes source rows/files. Query/search effective
text and note. Default omit hidden; include_hidden=1 reveals them.

## Store API

```js
export class StoreError extends Error { status; code; }
export class ChatStore {
  constructor(outputDir, { databasePath } = {})
  async initialize({ force = false } = {}) // import/cache; returns counts
  listAccounts() // [{uid, name, message_count, conversation_count}]
  listConversations(uid, {q='', limit=100, offset=0, includeHidden=false}={})
  // {items:[{id,name,message_count,first_time,last_time}], total}
  listMessages(uid, {conversationId='', q='', limit=100, offset=0,
                     includeHidden=false}={})
  // {items:[message],total,limit,offset}
  getMessage(uid, uuid) // message or StoreError 404
  updateMessage(uid, uuid, {text_override, note, hidden, expected_revision})
  restoreMessage(uid, uuid, expectedRevision)
  close()
}
```

message = {account_uid, msg_uuid, conversation_id, conversation_name, sender,
sender_name, created_time, time, message_type, type_name, text_original, text,
raw_json, meta, edit:{text_override,note,hidden,revision,updated_at}}.
raw_json remains a string, never converted to a JS object in a response.
meta is the source _export block (IDs normalized to strings where needed).
Use parameterized SQL, bounded limits (1..500), valid UIDs, deterministic order
created_time then msg_uuid, explicit closure, transactional reindex/write.

## HTTP API (relative paths, usable behind a stripped proxy prefix)

- GET api/session => {authenticated,auth_required,csrf_token}; token only if authenticated
- POST api/login {token} => session response, HttpOnly SameSite cookie
- POST api/logout => {ok:true}; mutating requests use X-CSRF-Token
- GET api/accounts => {items:[account]}
- GET api/conversations?account=UID&q=&limit=&offset=&include_hidden=1
- GET api/messages?account=UID&conversation=ID&q=&limit=&offset=&include_hidden=1
- GET api/message?account=UID&uuid=UUID => {item:message}
- PATCH api/message?account=UID&uuid=UUID => edit payload above => {item:message}
- DELETE api/message?account=UID&uuid=UUID body {expected_revision} => {item:message}
- GET api/decorations => decoration_manifest.json or {bubbles:{},inline_emoji:{}}
- GET/HEAD media/attachments/... => contained file with byte-range support
- GET / => built Vue app; assets served only from web/dist, not entire export

Errors: {error:{code,message}}, status 400/401/403/404/409/413/416/500.
JSON is UTF-8, Cache-Control no-store on APIs. No raw SQL API or arbitrary file
serving. Safe non-account paths never expose DBs or configuration. Local default
127.0.0.1:8765. Non-loopback bind requires --token or DUOSHAN_ACCESS_TOKEN.
Cookie authentication protects private API/media; CSRF + Origin checks protect
edits. Password/token never appears in URLs. Public deploy uses HTTPS proxy.

## Media helper API (p5)

```js
export function resolveMediaPath(outputDir, relativePath) // real contained file, attachments only
export async function serveMedia(req, res, outputDir, relativePath)
```

relativePath is decoded ONCE by coordinator and starts attachments/. Reject
absolute/drive/backslash/dot traversal and realpath symlink escapes. Failure
must have error.status/error.code (400,403,404,416); no sensitive paths in errors.
Single HTTP byte ranges and HEAD supported, invalid/multiple ranges => 416.
Use streaming, Content-Length, Content-Type, Accept-Ranges, Content-Range,
X-Content-Type-Options nosniff. Do not load whole videos into memory.

## Frontend

Vue 3 composition API with local bundled assets; Chinese UI, English filenames.
Account picker, conversation sidebar with search, paginated messages, full
account/conversation text search, raw original detail, edit text/note/hide and
restore original, visible edited/hidden badges, clear save/error/conflict states.
Use text interpolation, never render raw chat as HTML. Media URLs use
`media/` + encoded relative path (no leading slash or hardcoded domain), local
audio/video/image controls, unavailable feedback, original inline emoji and
bubbles from meta + decorations (port existing algorithm). JS IDs stay strings.
Login form when auth_required; API helper carries CSRF and same-origin cookies.
Assets/API/media relative to document base; Vite base './'. Responsive layout.

## Commands and verification

Within web/: npm install; npm run build; npm start -- --data OUTPUT
Options --host, --port, --token, --database, --reindex. npm test uses Node test
runner and independent synthetic fixtures; no real chats in repository tests.
Default start builds a central SQLite index but does not edit messages.
Test store import/raw large IDs/edit persistence/restoration/conflicts/reindex,
HTTP auth/CSRF/traversal/ranges, Vue build, real-data read-only query counts,
and export migration to a new directory preserving media references.
