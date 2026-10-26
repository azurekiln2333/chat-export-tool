# Database Guidelines

Read [Export Contract](./export-contract.md) for APIs, schema ownership, errors,
and executable acceptance cases.

Use standard `sqlite3` for plaintext SQLite. Load SQLCipher lazily through
`database.open_database`; optional encrypted support must not prevent plaintext
exports or help/doctor from running. Open only private source snapshots with
committed WAL data. Source files remain untouched.

Always close connections deterministically. SQLite connection `with` blocks
commit/rollback but do not close. Use package context managers,
`contextlib.closing`, or explicit `finally: db.close()`. Windows cleanup is part
of the contract, including repeated operations in one process.

Do not migrate original Duoshan tables. `decrypt_database` and the exporter
preserve their structure and values; media enrichment owns only `export_*`
objects. `media_index.index_media` refreshes owned rows transactionally, checks
a `msg` fingerprint before/after, checks manifest/index equality, and runs
`PRAGMA integrity_check`. Bind data values as SQL parameters. Dynamic schema
identifiers come from internal known identifiers, never raw message text.

Compare original fields in `messages.jsonl` with every database `msg` row.
Ignore only added `_export` metadata; decoded human-readable CSV/UI text is not
the raw source of truth. Preserve deleted-flag rows and unknown message types;
do not infer a recall from `deleted` alone.

The optional Node reader owns a separate `reader.sqlite`; its import cache and
revisioned annotation tables follow [Node SQLite Reader](./web-reader.md).
They must not be merged into original account databases.
