# Tests

The suite uses only the Python standard library (`unittest`) and runs fully
offline.

```powershell
python -m unittest discover -s tests -v
python -m unittest tests.test_validation -v
python -m unittest tests.test_cli -v
```

Current status: **108 tests, 0 skips** in a full install (Pillow, requests,
zstandard, tzdata, SQLCipher, pillow-heif, and `ffmpeg` on `PATH`).

| Module | Tests | Focus |
| --- | --- | --- |
| `tests/test_validation.py` | 36 | `validate_export`: data integrity, tamper detection, reader counters, decode path |
| `tests/test_cli.py` | 29 | end-to-end CLI: `export`/`media`/`validate`/`doctor`, resume, account filters, language boundary |
| `tests/test_runtime.py` | 38 | runtime safety: configuration, locations, resume identity, media reuse, media index, CLI error behaviour |
| `tests/test_media.py` | 5 | media and decoration handling against mocked HTTP |

The only tests that can skip in a minimal environment are the
`validate --decode-media` cases, which need `ffmpeg` and Pillow. Everything else
runs offline with the standard library alone.

The derived aggregate results of a complete real-backup verification are in
[docs/verification.md](verification.md); this page describes the automated suite.

## What is covered

`tests/synthetic.py` generates every fixture at run time. No chat content,
account ID, media file or absolute analysis path is stored in the repository.

It builds:

- **App-data sources** -- `databases/encrypted_<uid>_im.db` plus a plaintext
  `relation_uc_v3_<uid>` contact cache, message rows covering text, HTML/script
  text, spreadsheet formulas, inline emoji, stickers, voice, an unknown message
  type, a deleted-flag row and an unavailable media resource.
- **A real WAL case** -- rows committed with checkpointing disabled, so the newest
  data exists only in the `-wal` sidecar, exactly like a phone backup.
- **DataBackup archives** -- `.tar.gz`/`.tar` payloads in the Android layout, with
  hostile members (absolute paths, `..` traversal, drive letters, backslash
  traversal) to prove they are skipped.
- **Export trees** -- a complete, independently produced export (`chat.sqlite`,
  JSONL, CSV, HTML pages, manifests) used to test the validator without trusting
  the exporter.

`tests/test_validation.py` checks the validator both ways: every clean fixture
passes, and each deliberate corruption is detected -- tampered SQLite rows, extra
JSONL rows, unrendered messages, broken/escaping/remote links, CSV formula and
long-ID protection, missing media files, media-index path drift, reader counter
drift, unlisted account directories, summary total drift, and (with decoders)
size, SHA-256, frame-count and file-missing failures.

`tests/test_cli.py` drives the real pipeline from a real SQLite source:

- export -> rerender -> validate, with English names only in the generated tree
- ASCII account IDs used verbatim, plus an account filter that exports one account
- two sequential exports of different sources that stay independent
- `--resume` for the same source, and refusal for a changed source, a different
  account selection, or an output the tool did not create
- raw-row equality between `chat.sqlite` and `messages.jsonl` for every column
- HTML escaping and CSV neutralisation of the dangerous fixture text
- rows that exist only in the `-wal` sidecar
- archive traversal members skipped while the legitimate databases still export
- a single-database-file source
- CLI `inspect`, `validate`, `doctor` and `--version` behaviour and exit codes
- the language boundary: the reader interface is Simplified Chinese
  (`lang="zh-CN"`, Chinese labels), every generated directory/file name stays
  English, original chat text is copied verbatim, and `validate` passes on a
  Chinese-UI page exactly like on an English one (it checks structure, never
  label text)

The CLI `export`/`media` tests drive the real pipeline, not a stub: a synthetic
Android source is exported, rerendered, indexed and validated through the same
entry points a user calls.

`tests/test_runtime.py` protects a user's files and the run contract:

- configuration: paths resolved, defaults, ASCII-digit account IDs, numeric
  bounds, and an actionable error for an unknown timezone
- locations: an output/work directory inside the source is refused, a source
  inside the output is refused, work cannot equal or contain the output, and the
  default work directory inside the output is accepted
- output ownership and `--resume`: a fresh output writes state without touching
  the source; a non-empty output requires `--resume`; an output that is a file or
  a missing source is rejected; resume is allowed only for the same source and
  the same account selection; the source signature covers full content, so a
  single edited row invalidates it
- media reuse: verified media is copied and its paths rewritten, content that
  does not match the manifest is refused, overlapping export directories are
  refused, only messages identical in both exports are matched, a missing
  manifest is tolerated, and legacy manifest/JSONL names are still found
- media indexing: only `export_` objects are added and `msg` is left unchanged;
  a local media path that escapes the export or is non-ASCII is rejected
- resource handling: `open_database` closes its connection on exit
- CLI error behaviour: every failure returns a nonzero exit code with a clean
  message and no traceback (missing source, missing directory, corrupt archive,
  output inside the source), `--json` errors are structured on stdout, and
  `doctor` reports dependencies as JSON
- CLI encoding regressions: `--json` output keeps exact Unicode values on a
  strict legacy Windows code page (GBK) by emitting ASCII JSON escapes, for both
  success and error results; exported files themselves stay UTF-8

`tests/test_media.py` covers media and decoration handling with a mocked HTTP
session (no external host is contacted):

- an offline rebuild keeps previously imported media and decorations instead of
  erasing them
- `valid_ok` rejects a same-size hash tamper and unsafe paths
- a valid journal recovers over a stale failed manifest
- an interrupted download keeps an incremental journal usable
- HTTPS redirect handling and the host allow policy

## Language boundary in tests

Tests must never assert English reader labels. Assert one of:

- generated **names** (English/ASCII) -- for example an ASCII check over
  `output.rglob("*")`,
- **structure** (CSS classes, element IDs, links, manifest keys) -- what
  `validate` itself checks,
- the **interface language** (`<html lang="zh-CN">` and the presence of Chinese
  interface text) where the UI itself is under test,
- **chat content** (verbatim original text) where data fidelity is under test.

The synthetic export fixture in `tests/synthetic.py` intentionally stays
English-labelled: it is an independent producer used to prove that validation
does not depend on UI labels (`test_reader_interface_language_does_not_affect_validation`
localizes it and still expects `passed`).

## Adding a fixture

Add rows with `synthetic.message_row(...)`, then pass them through
`synthetic.build_app_source(..., rows_by_uid={uid: rows})` (for end-to-end tests)
or `synthetic.build_export(...)` (for validator tests). Keep new IDs synthetic and
keep every path relative to a temporary directory.

## Vue/Node reader

Run `npm ci`, `npm run build` and `npm test` from `web/` for the optional
database reader. Its independent fixtures test exact large integer imports,
reversible persistent edits, revision conflicts, media migration/ranges,
authentication/CSRF/proxy origins, contained paths and bounded shutdown.
See [web-reader.md](web-reader.md) for the test contract, data model and HTTP API,
and [deployment.md](deployment.md) for the deployment paths and procedures.

## Unified importer and WeChat

`test_library.py` exercises source detection, separate app imports, repeated and
overlapping snapshots, preserved annotations/history, source path containment,
content-addressed media and failed metadata publication. `test_wechat.py`
constructs plaintext and SQLCipher/4000/no-HMAC databases, committed WAL rows,
negative binary UIN key material, account-isolated media and zstd archives.
It also runs real SILK encode/decode and WXGF/HEVC-to-PNG conversion fixtures,
then verifies the original bytes and actual decoded media. Wrong keys,
unsupported schemas and corrupted caches must fail or recover explicitly.

`test_database_retention.py` and `test_source_retention.py` inject partial copy,
malformed archive member and metadata failures. Retries must retain incomplete
artifacts and previous outputs without permanently deleting them. All fixtures
live in the OS temporary directory outside the protected analysis workspace;
real message/media samples must never be copied into the repository.

`test_library_media.py` intercepts every network request. It verifies live
three-app reader imports through the actual Node metadata patcher, signed-URL
fallbacks, redirect/domain boundaries, byte limits, expired/encrypted resources,
cancellation and recovery, deletion races, source-folder protection, cached
local verification and post-import Live Photo media. Generated video/audio
fixtures test decoder validation, retained originals, and successful publication
without a second media copy in work. It also proves stale JSONL does not restore
deleted messages and that raw JSON, annotations and deletion records survive.
Fresh-directory concurrency tests verify that successful workers still journal
and attach when another fails, and that retry handles only the failed resource.
Path rejection tests verify local diagnostics and isolation between apps. SILK
decoder output must not undergo a redundant WAV conversion; image conversion
must close its source before atomic publication on Windows.
Offline report tests cover bounded journal snapshots during appends, repeated
resource entries, torn lines, current/deleted message references, complete signed
queries, unknown historical attempts and lock-free reads beside an active
mutation lock. Mock responses verify HTTP 403, invalid image bytes, unsupported
hosts and rejected redirect destinations. Success patches must exclude all URL
diagnostics. Reports are HTML-escaped local text with no remote links/content.
