# Export Contract

## 1. Scope / Trigger

Read before changing source discovery, export orchestration, generated paths,
media manifests, database indexes, or validation. The tool reads existing
Android Duoshan backups; it does not acquire device data.

## 2. Signatures

Entry point: `duoshan-export` (also `python -m duoshan_export`). The authoritative
parser is `cli.build_parser()`:

```text
inspect SOURCE [--work-dir DIR] [--sqlcipher-module-path DIR] [--json]
export SOURCE -o OUTPUT [--account UID ...] [--resume] [--download-media]
       [--reuse-media EXPORT] [--timezone NAME] [--page-size N]
       [--work-dir DIR] [--workers N] [--decode-media] [--json]
media OUTPUT [--source SOURCE] [--download-media] [--reuse-media EXPORT]
      [--work-dir DIR] [--workers N] [--decode-media] [--json]
validate OUTPUT [--decode-media] [--ffmpeg PATH] [--json]
doctor [--sqlcipher-module-path DIR] [--ffmpeg PATH] [--ffprobe PATH] [--json]
```

`export` and `media` also accept SQLCipher module and FFmpeg/FFprobe paths.
Library boundaries:

```python
prepare_source(source, work) -> SourceLayout
discover_accounts(layout) -> list[AccountSource]
open_database(source, uid, work_dir, *, password=None, compatibility=3,
              sqlcipher_module_path=None, required_tables=None)  # context manager
export_chats(config, layout) -> dict
rerender_export(config) -> dict
download_media(config) -> dict
prepare_decorations(config, layout) -> dict
index_media(config) -> dict
validate_export(output, *, decode_media=False, ffmpeg="ffmpeg") -> dict
```

Configuration is the immutable `ExportConfig` in `config.py`. Do not introduce
module-level source/output/work paths or account-specific defaults.

## 3. Contracts

- Generated directory/file names use ASCII English names from `paths.py`;
  account directories use ASCII numeric UIDs. The HTML interface is Simplified
  Chinese and declares `lang="zh-CN"`. Original message/contact text is never
  translated. Code symbols and machine-readable keys remain English.
- Required configuration: `source`, `output`, `work`. Defaults: no account
  filter, `Asia/Shanghai`, 600 messages/page, 6 workers, downloads disabled.
- No environment variables are required. Paths, timezone, binary locations,
  and optional SQLCipher module location come from configuration/CLI arguments.
- Source files are read only. Copy databases and committed WAL/journal sidecars
  into private snapshots before opening. Hash full database/archive contents
  and sidecars when deciding whether cached extraction/snapshots can be reused.
- `export_state.json` owns a tool-created output. Version 1 stores `source`,
  `source_signature`, `accounts`, `timezone`, `page_size`, `work`, and `status`.
  Write `completed` only after validation passes. For `media`, work precedence
  is explicit argument, saved work, then `OUTPUT/.work`.
- Each account has `chat.sqlite`, `messages.jsonl`, `messages.csv`, contacts,
  metadata/statistics, and conversation HTML/TXT. JSONL retains every `msg`
  column and adds `_export`; CSV may prefix a quote to neutralize formulas or
  preserve long IDs. SQLite and raw JSONL retain original values.
- `media_manifest.json` stores shared `resources` and per-message `messages`
  keyed by `uid:msg_uuid`. Reuse requires matching original messages, contained
  paths, size, and SHA-256. Flush every completed download to the journal so
  interruption does not lose it. Same size alone does not prove integrity.
- Network access requires `--download-media`; HTTPS/redirects pass the known-CDN
  policy. Missing/expired resources are reported, never invented.
- Indexing owns only `export_*` objects: `export_local_attachment`,
  `export_bubble_resource`, indexes, and `export_chat_with_media`. Preserve
  original tables/rows. Store media paths relative to export root and database.
- `validate_export` writes `validation_results.json` and reader reports and
  returns `status`, `accounts`, `totals`, `problem_count`, and `problems`.
  Decode reports are opt-in. Warnings differ from integrity failures. UI
  labels never determine validation results.
- Summary `voice_ok` counts successful manifest voice messages. Total rendered
  audio counts every audio element, including original audio attachments;
  these counts may legitimately differ.

## 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Non-ASCII/non-numeric UID, page size outside 1–5000, workers outside 1–32 | `ExportError` before processing |
| Output/work overlaps source; work equals or contains output | Reject; never alter source |
| Nonempty unrelated output | Reject, even with `--resume` |
| Resume signature or account selection differs | Reject; request new output |
| Archive traversal, reserved paths, links, device files | Skip unsafe members; never escape owned work |
| Cleanup target unowned or a link/reparse point | Reject recursive cleanup |
| Wrong key, missing dependency, corrupt archive, unsupported database | Actionable error and nonzero CLI result |
| Media path escape, same-size hash mismatch, invalid redirect | Reject reuse/download; retain explicit failure |
| Raw data/count mismatch or broken local link | Validation fails; do not mark completed |
| Original audio attachment adds an audio element | Keep it; compare voice counter only to manifest voices |

CLI exit codes: success 0, processing/validation error 1, argument usage error 2,
interruption 130. With `--json`, progress goes to stderr and one result/error
object goes to stdout. Console JSON uses ASCII Unicode escapes so legacy
Windows GBK pipes preserve every value; exported files remain UTF-8. Catch
ordinary exceptions at the CLI boundary; do not
catch `BaseException` or suppress failures in library functions.

## 5. Good / Base / Bad Cases

- Good: arbitrary accounts export independently from an archive; verified local
  voice/decorations render in Chinese pages linked by English paths.
- Base: plaintext database without contacts/media exports every row offline,
  preserving unknown message types and reporting unavailable assets.
- Bad: fixed IDs/paths/versions; English UI inferred from English filenames;
  stale snapshot after middle-of-file changes; removing real attachments to
  force aggregate counters to match.

## 6. Tests Required

Run `python -m unittest discover -s tests -v` with no delivery-order skips.
`test_cli` exercises temporary Android data, WAL, archives, account selection,
and independent exports. `test_runtime` covers ownership, full-content hashes,
reuse, indexes, and connection closure. `test_media` mocks interrupted downloads.
`test_validation` independently produces exports and deliberately corrupts them.
See `docs/testing.md` for assertion points.

Assert Chinese UI, ASCII paths, unchanged original text, field-for-field
JSONL/database equality, stable `msg` fingerprints across indexing, working
media links, and failure on corruption. Real data stays outside the repository
and build artifacts.

## 7. Wrong vs Correct

Wrong: `with sqlite3.connect(path) as db:` alone; it manages a transaction but
leaves the connection open, causing Windows file-lock/cleanup failures.

Correct:

```python
from contextlib import closing
import sqlite3

with closing(sqlite3.connect(path)) as db:
    with db:
        db.execute("DELETE FROM export_local_attachment WHERE account_uid=?", (uid,))
```

Wrong: translating buttons because output filenames are English.
Correct: `index.html` contains `<html lang="zh-CN">` and Chinese controls;
original message text remains verbatim and escaped for HTML.
