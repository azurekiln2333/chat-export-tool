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
