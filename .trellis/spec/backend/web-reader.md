# Node SQLite Reader Contract

The optional editor lives in `web/`: Vue 3/Vite source, a Node.js >=24.15
service, and built-in `node:sqlite`. Its runtime does not depend on Python.
The extraction CLI remains a separate Python package. See `docs/web-reader.md`
and the store/server/media public interfaces before changing this layer.

## Storage

- Create `reader.sqlite` beside the export, or at the explicit `--database`
  path. Never update source backups, per-account `chat.sqlite`, JSONL or media.
- Import exact JSONL lines into `messages.raw_json` as TEXT. Parse derived
  fields with `json-bigint`; all API identifiers are strings. `JSON.parse`
  followed by serialization cannot preserve unsafe integer IDs.
- Keep reversible text/note/hidden values in `message_edits`. Require
  `expected_revision` on every mutation. Restoring also increments revision;
  resetting to zero would allow stale writes after a restore.
- Bind SQL values. Reindex transactionally, preserve annotation rows and
  fingerprint full source content. An existing index works without JSONL.
- Explicitly close SQLite before deleting a Windows fixture or export.

## HTTP and media

- Default to loopback. Require a token for non-loopback binding. APIs and
  media use the same cookie session; writes require CSRF and matching Origin.
- Compare canonical origins using the request Host and the Origin scheme:
  HTTPS `:443` and HTTP `:80` are equivalent to omitted default ports;
  nondefault ports remain distinct. Do not trust `X-Forwarded-Host` instead.
- Return bounded, parameterized data APIs, never arbitrary SQL or file paths.
  Static files come only from the Vue build; export DBs are not public files.
- Accept only export-relative `attachments/` media. Check lexical paths and
  realpath containment, including a junction at the attachments root.
- Stream audio/video with GET/HEAD and single ranges. Weak If-Range means
  full response; unsatisfiable ranges include Content-Range with status 416.
- Close idle connections during shutdown and force-close active connections
  after five seconds. A paused media stream must not keep SQLite locked
  indefinitely. Repeated termination signals force process exit.

## Verification

Run `npm run build` then `npm test` in `web/`. Tests use independent synthetic
fixtures and exact unsafe integer literals. Missing code/builds fail rather
than skip. OS-forbidden symlink creation is the only capability skip.
Node test cleanup hooks run in registration order: use a single hook that
awaits server closure before removing fixture directories.

Keep real-data probes outside the repository and report only aggregate
counts. Verify directory migration, media bytes/ranges, original file hashes,
and the production build separately from any visual browser inspection.

## Permanent deletion and calendar contracts

### 1. Scope / Trigger

Reader version 0.2.0 adds permanent single/batch SQL deletion and a month
calendar. Schema version 3 must upgrade existing reader databases without
JSONL present. Original per-account SQLite/JSONL and shared media are untouched.

### 2. Signatures

```js
deleteMessage(uid, uuid, expectedRevision)
deleteMessages(uid, [{ msg_uuid, expected_revision }])
deleteConversation(uid, id, expectedMessageCount)
deleteConversations(uid, [{ id, expected_message_count }])
getCalendar(uid, { month, conversationId = '', q = '', includeHidden = false })
listMessages(uid, { date = '', conversationId, q, includeHidden, limit, offset })
```

Schema adds indexed `messages.day` and
`deletions(kind, key_hash, deleted_at)` with primary key `(kind,key_hash)`.
The ledger holds only domain-separated SHA-256 hashes of
`kind + '\0' + JSON.stringify([uid,key])`, with no deleted text, raw JSON,
names or plaintext identifiers. Keep it through migrations and imports.

### 3. Contracts

| HTTP method and endpoint | Body / query |
| --- | --- |
| `DELETE api/message?account=UID&uuid=UUID` | `{confirm_delete:true,expected_revision}` |
| `DELETE api/conversation?account=UID&conversation=ID` | `{confirm_delete:true,expected_message_count}` |
| `POST api/messages/delete?account=UID` | `{confirm_delete:true,messages:[{msg_uuid,expected_revision}]}` |
| `POST api/conversations/delete?account=UID` | `{confirm_delete:true,conversations:[{id,expected_message_count}]}` |
| `POST api/message/restore?account=UID&uuid=UUID` | `{expected_revision}`; restores annotation only |
| `GET api/calendar` | `account,month=YYYY-MM,conversation?,q?,include_hidden?` |
| `GET api/messages` | Existing scope/pagination plus optional `date=YYYY-MM-DD` |

Deletion returns top-level `{deleted:true,account_uid,deleted_messages,
deleted_conversations}` plus `msg_uuids` or `conversation_ids`. Single wrappers
also return `msg_uuid` or `conversation_id`. Calendar returns
`{month,timezone,days:[{date,message_count}],total,first_date,last_date}`.
Empty calendar bounds are null. Counts/bounds follow account, conversation,
effective text/notes search and hidden scope; bounds span all matching months.

Delete messages, raw JSON and corresponding edits in one transaction. Validate
every batch item before mutating; refresh conversation count/time and
remove empty conversations. Whole-conversation deletion includes hidden rows.
An empty account remains valid. SQL deletion has no content recycle bin.
`secure_delete` and post-commit WAL truncation are best effort; a purge failure
must never turn a committed delete into an error response.

Use valid exported `time` date prefixes first; timestamp fallback takes
**milliseconds** and uses the export timezone. Include `export_summary.json`
in source fingerprints, clear timezone/formatter caches on initialize, and
persist the resolved zone in `store_meta`. Without summary use stored zone,
then exported row metadata, then UTC. A changed zone recomputes fallback days.
Undated messages remain visible without a date filter.

Migrate columns, backfill, indexes and version/zone markers atomically. Close
the handle on constructor failure. A completed-import marker, rather than a
nonzero message count, determines whether an unchanged import can be skipped.
Deleting `reader.sqlite` also loses edit/deletion suppression; original records
can then be imported again. Back up the working database as well as originals.

### 4. Validation & Error Matrix

| Condition | Result |
| --- | --- |
| Missing/non-boolean-true `confirm_delete` | 400 `confirmation_required`; no deletion |
| Empty, duplicate, >500 or malformed batch | 400 `invalid_batch` or `invalid_argument` |
| Invalid month/day, including impossible leap dates | 400 `invalid_argument` |
| Unknown account/message/conversation | 404 `not_found` |
| Stale message revision or conversation count | 409 `conflict`; whole batch unchanged |
| Missing login / invalid CSRF or Origin | Existing 401 / 403 contract |

### 5. Good / Base / Bad Cases

- Good: delete a selected batch, reimport changed JSONL, and deleted rows stay
  absent while surviving annotations keep revisions.
- Base: an empty recorded month returns `days:[]` and `total:0`; dayless rows
  remain accessible through the unfiltered paginated list.
- Bad: interpreting the old DELETE restore payload as deletion, dropping tables
  during upgrade, or preserving deleted contents in an auxiliary table.

### 6. Tests Required

`delete.test.mjs` independently inspects SQL rows, edits, raw content and hash
ledger; asserts atomic conflicts, account isolation, reindex/reopen suppression
and index-only upgrades. `calendar.test.mjs` asserts exact buckets, export
timezone, search/hidden/conversation scope and day pagination. `http.test.mjs`
asserts all routes and confirmation/session/CSRF/Origin rejection. Frontend
tests cover selection, confirmations, scope races and authoritative refresh.
Real-scale upgrade probes use isolated snapshots and aggregate reports only.

### 7. Wrong vs Correct

Wrong: `DELETE api/message` with only `expected_revision` restores or removes
content depending on frontend age. Correct: restore uses its explicit POST
route, while permanent DELETE always requires `confirm_delete:true`.

Wrong: add `day`, commit, then backfill. Correct: add/backfill/update markers
in one transaction so an interrupted upgrade retries cleanly.

## Managed-library media updates

`library-media.mjs` is the separate metadata-only boundary used after explicit
media downloading. Do not call the message importer to attach files, and do
not initialize from JSONL: it may still contain messages the user deleted.
Require an existing database with `library_mode=incremental-v1`.

```js
patchLibraryMedia({outputDir, updatesFile, sourceId = '', project = true})
resumeLibraryMedia({outputDir})
```

CLI: `node server/library-media.mjs --output LIBRARY --updates PATCH.jsonl
[--source-id ID] [--json]`. Use `--resume` instead of `--updates` to publish
pending projections without the original patch file. `--no-project` commits
metadata while deliberately leaving its projection pending.

Each JSONL row requires string `uid`/`msg_uuid` and at least one of
`local_media`, `original_media`, `media`, or `media_status`. Optional `app` and
`expected_raw_sha256` guard against a changed message during downloading.
The digest is SHA-256 of the exact UTF-8 `raw_json`. Other fields are rejected.
The resource `media` is a flat object (`kind`, `status`, `path`, optional
`bytes`, `sha256`, `mime`, `format`, codec details). Successful resources need
`kind` and `path`. Local media lists contain strings only; resolve all new
paths under the real library `attachments` directory, rejecting traversal,
alternate streams and escaping junctions. Check supplied size/hash values.

Inside one immediate transaction, resolve only the live `(uid,msg_uuid)` row,
check app/raw digest and message/conversation deletion hashes, and skip
missing, stale or suppressed rows. Never insert messages, change identities,
or modify raw content, other message columns, edits or deletion rows. Use
SQLite `json_set` for the allowlisted media paths so unrelated metadata values
including unsafe integer literals do not pass through a JavaScript numeric
round trip. Keep unchanged `meta_json` byte-for-byte.

Union local/original paths without duplicates. Keep an existing valid primary
resource when later resources belong to the same message (photo/live video,
video/poster), appending the additional resource path to `local_media`.
Repeated resource ordering must not toggle the primary or reproject unchanged
accounts. A missing or corrupt primary may be replaced; its file-specific
kind/codec/format details must not leak into the replacement. Failed attempts
cannot downgrade an existing successful resource.

`library_media_batches` durably records a patch-content fingerprint, source,
result, affected accounts and pending/completed projection status. Repeating
the identical batch does not apply metadata again. A new invocation and
`--resume` both finish prior pending projections from the current database;
messages deleted since the original commit stay absent. The publisher retains
previous files. Report `patched_messages`, `unchanged_messages`,
`skipped_messages`, `repeated`, affected `accounts`, and `projection_status`.

`library-media.test.mjs` independently verifies exact non-media preservation,
unsafe numeric/raw literals, cross-app identity guards, deleted/stale skips,
semantic and fingerprint idempotence, multi-resource primary stability,
corrupt-primary replacement, whole-batch rollback on bad paths/types/hashes,
junction containment, interrupted projection retry and CLI recovery. Use
synthetic temporary directories outside the protected analysis workspace.
The live HTTP integration starts the reader before a separate-process media
patch, then checks fresh message API data and local attachment bytes through
the same running server, with original content and user edits unchanged.
It must not restart the server or reindex to observe the new media.
