# Duoshan chat export tool

Turn a local Android **Duoshan** (`my.maya.android`) backup into a portable,
fully offline archive: a standalone SQLite database, raw JSONL, spreadsheet-safe
CSV, per-conversation text files and a browsable HTML reader with the original
voice messages, stickers, inline emoji and chat-bubble artwork.

**Xiaohongshu (`com.xingin.xhs`) backups are supported too**: the same
`inspect`/`export` commands decrypt its WCDB-encrypted `msgDB`/`localRelationDB`
(using the passphrases stored in the backup's MMKV files) and produce the same
portable reader tree. See [docs/xhs-export.md](docs/xhs-export.md). With the
default `--app auto` policy Duoshan behaviour is unchanged; Xiaohongshu
decryption additionally needs `pip install "duoshan-chat-export-tool[xhs]"`
(the `cryptography` package).

The tool only **reads** the backup you give it. It never logs in, never contacts
the network unless you explicitly ask it to, and never modifies the original
files. The original message content is preserved byte for byte.

**Language boundary:** every generated directory and file name, every database
table/column, every manifest and report key, the code and the console messages
are **English**. The default HTML reader interface (labels, buttons, headings)
is **Simplified Chinese** (`<html lang="zh-CN">`). Original chat text, contact
names and message content are never translated. See
[Interface language](#interface-language).

- Verified against the **Android** app layout. Other platforms and app versions
  may store data differently and are not guaranteed.
- Python **3.10 or newer**.
- Offline by default: `export` copies what is already in the backup. Voice,
  sticker and bubble downloads are opt-in.

## Quick start

A Chinese quick start is available in [README_zh-CN.md](README_zh-CN.md).

## Vue browser and editor

The `web/` application uses Vue 3, Node.js 24.15+ and SQLite. Its web runtime
does not require Python. The extraction CLI remains independently usable.

```powershell
cd web
npm ci
npm run build
npm start -- --data "D:\DuoshanChatExport"
```

The service imports `reader.sqlite` for querying, search, reversible annotation
and permanent deletion. It also shows a month calendar of recorded days, computed
in the export timezone, and filters the paginated list by the selected day.
Original archives remain available. Media stays in `attachments/` and is served
through relative URLs, so moving the complete data directory or changing the
server domain preserves references.

- **Hide, restore and delete are different.** Hide and restore only change an
  annotation row. Delete is a real SQL `DELETE` of the message rows, their
  annotations and the affected conversation aggregates, and it cannot be undone
  from the UI. There is no recycle bin; only a SHA-256 suppression hash is kept
  so a reimport does not resurrect the record.
- **The reader never erases your source archive.** `<uid>/chat.sqlite`,
  `messages.jsonl`, the CSV/TXT/offline HTML files and `attachments/` are
  untouched by any reader action. They still contain the deleted messages, so
  erase them yourself if that is the goal.
- **`reader.sqlite` is not a backup.** Deleting it loses local edits, notes,
  hidden flags *and* the deletion suppressions, so deleted messages come back on
  the next import.

See [docs/web-reader.md](docs/web-reader.md) for the data model, the deletion and
calendar contract and the full HTTP API. See
[docs/deployment.md](docs/deployment.md) for the exact local paths, the Linux
`/opt` + `/srv` layout, `npm ci`/build/start commands, HTTPS reverse-proxy setup
and backup procedures.

```powershell
# 1. Create .venv and install everything (SQLCipher + HEIF + media tools check)
./install.ps1

# 2. See which accounts a backup contains (temporary extraction, no export)
./run.ps1 inspect "D:\backup\DataBackup"

# 3. Export everything to a new folder
./run.ps1 export "D:\backup\DataBackup" -o "D:\DuoshanChatExport"

# 4. Open the reader
start "D:\DuoshanChatExport\index.html"
```

`run.ps1` forwards every argument to `python -m duoshan_export`, so anything
below works with either form:

```powershell
python -m duoshan_export <command> [options]
duoshan-export <command> [options]          # after a normal pip install
```

Install variants:

```powershell
pip install .            # core: Pillow, requests, zstandard, tzdata
pip install ".[full]"    # + sqlcipher3-wheels (encrypted DBs) + pillow-heif (HEIC)
pip install ".[encrypted]"   # only SQLCipher support
./install.ps1 -CoreOnly      # skip the optional extras
```

## Commands

Run `duoshan-export <command> --help` for the built-in reference. Every command
also accepts `--json` to print one machine-readable JSON object (progress goes to
stderr) and exits non-zero on error.

### `inspect SOURCE`

Discover accounts and count messages **without exporting**.

| Option | Meaning |
| --- | --- |
| `SOURCE` | Backup folder, archive, `databases` folder or a single DB file |
| `--work-dir DIR` | Where to extract/snapshot (default: a temporary directory) |
| `--sqlcipher-module-path DIR` | Folder containing an installed SQLCipher binding |
| `--json` | Print the result as JSON |

### `export SOURCE -o OUTPUT`

Create the portable export. Networking stays off unless `--download-media` is
given. After the export the tool rebuilds the reader, refreshes the SQLite media
index and validates the result; a failed validation aborts with a non-zero exit
code.

| Option | Meaning |
| --- | --- |
| `SOURCE` | Backup folder, `*.tar.zst` archive, `databases` folder or a single DB |
| `-o`, `--output DIR` | Destination directory (required) |
| `--account UID` | Export only this account; repeat to select several (default: all) |
| `--timezone TZ` | IANA zone for displayed times (default `Asia/Shanghai`) |
| `--page-size N` | Messages per HTML page, 1-5000 (default 600) |
| `--resume` | Continue/refresh the same unchanged source in an existing output |
| `--download-media` | Download voice, stickers and bubbles from the original URLs |
| `--reuse-media EXPORT` | Copy verified matching media from an existing export |
| `--decode-media` | Also decode audio and every image frame during validation |
| `--work-dir DIR` | Private extraction/snapshot directory (default `OUTPUT/.work`) |
| `--workers N` | Concurrent media requests, 1-32 (default 6) |
| `--sqlcipher-module-path DIR` | Folder containing an installed SQLCipher binding |
| `--ffmpeg`, `--ffprobe` | Executable names or paths (default `ffmpeg`/`ffprobe`) |
| `--json` | Print the result as JSON |

### `media OUTPUT`

Reuse/download media, rebuild the reader and refresh the SQLite media index for
an export that already exists. Refuses to invent data: without `--download-media`
it only uses local resources.

| Option | Meaning |
| --- | --- |
| `OUTPUT` | An existing export directory |
| `--source SOURCE` | Optional original backup, to refresh local decoration assets |
| `--download-media` | Allow network downloads |
| `--reuse-media EXPORT` | Copy verified matching media from another export |
| `--decode-media` | Also decode during validation |
| `--work-dir`, `--workers`, `--sqlcipher-module-path`, `--ffmpeg`, `--ffprobe`, `--json` | As for `export` |

### `validate OUTPUT`

Check the exported data, local links and media integrity. Structural problems
raise an error and exit `1`; other findings are written to the report files.

| Option | Meaning |
| --- | --- |
| `OUTPUT` | The export directory to check |
| `--decode-media` | Decode audio and every image frame as well (needs ffmpeg) |
| `--ffmpeg` | Executable name or path |
| `--json` | Print the result as JSON |

Report files written into `OUTPUT`:

- `validation_results.json` -- data integrity (always)
- `reader_media_validation_results.json` -- reader/media cross-checks (always)
- `media_validation_results.json` -- per-file decode results (with `--decode-media`)

### `doctor`

Report the Python version and whether each dependency and media tool is
available. Exits `1` when a required core dependency is missing.

| Option | Meaning |
| --- | --- |
| `--sqlcipher-module-path DIR` | Folder containing an installed SQLCipher binding |
| `--ffmpeg`, `--ffprobe` | Executable names or paths |
| `--json` | Print the result as JSON |

## Accepted inputs

Point `SOURCE` at any of:

1. An extracted Android app-data root (contains `databases/` or the
   `my.maya.android` package folder).
2. A DataBackup folder containing `*.tar.zst` (or `.tar`, `.tar.gz`, `.tgz`,
   `.zst`) archives.
3. A `databases` folder.
4. A single Duoshan database file (`encrypted_<uid>_im.db`, `<uid>_im`, ...).

Encrypted databases are read with their original SQLCipher parameters. The
account key for the IM database is derived from the account ID; the contact
cache uses its own passphrase. Plain SQLite files need no extra dependency.

Archives are treated as untrusted: absolute paths, `..` traversal, Windows
reserved names, drive letters, links/devices and oversized members are skipped,
and nothing is ever written outside the work directory.

## What lands in `OUTPUT`

```
OUTPUT/
  index.html                      entry page listing accounts and the media summary
  reader.css                      stylesheet used by every page
  media_manifest.json             per-resource download result, hashes, per-message links
  decoration_manifest.json        bubble IDs, configuration and inline-emoji assets
  export_summary.json             machine-readable export and media statistics
  validation_results.json         data-integrity report
  reader_media_validation_results.json
  media_validation_results.json   (with --decode-media)
  README.txt                      export-local notes (Simplified Chinese content, English file name)
  attachments/                    shared local media (voice, stickers, bubbles, ...)
  <uid>/                          one folder per account
    chat.sqlite                   standalone decrypted database (all original rows)
    messages.jsonl                one complete raw msg row per line + _export block
    messages.csv                  readable text/time/sender, spreadsheet-safe
    contacts.json / contacts.csv  contact cache used to complete display names
    conversation_metadata.json    conversation list, groups, members, attachments
    statistics.json               counts, types, date range
    conversation_index.csv        conversation index
    conversations/                conversation_NNN_page_NNN.html
    transcripts/                  conversation_NNN.txt
```

`messages.jsonl`, `chat.sqlite`, `messages.csv` and the TXT files keep the
original text and identifiers untouched. The CSV may prefix a cell with an
apostrophe to stop a spreadsheet from executing message text as a formula or
corrupting a long ID; JSONL and SQLite always keep the raw value.

IDs can exceed JavaScript's safe integer range, so read JSONL with Python,
SQLite or another big-integer-aware JSON reader.

## Behaviour worth knowing

- **Offline by default.** `export` and `media` only touch the network with
  `--download-media`. No URL is ever forged; failures are recorded with their
  reason in the media manifest.
- **Resume is strict.** An existing output may be resumed only for the *same
  unchanged source* and the *same account selection*. A changed backup, a
  different `--account` list, or an output the tool did not create is refused.
- **Account copies are kept separate.** If two accounts hold a copy of the same
  conversation, both are exported; nothing is de-duplicated across accounts.
- **Deleted-flag rows are kept.** Rows with a non-zero `msg.deleted` value are
  exported and marked; this flag is not proof of a recall.
- **Media completion is honest.** Missing or expired URLs appear with the reason,
  and the reader falls back to the default bubble style when an asset was not
  preserved.
- **Reader deletion is database-local.** Permanently deleting messages or whole
  conversations in the web reader changes only `reader.sqlite`. Account
  `chat.sqlite`, `messages.jsonl`, CSV/TXT/offline HTML and shared media in
  `attachments/` keep the original rows, and deleting the database restores every
  deleted message on the next import.

## Interface language

The tool keeps machine-facing names and the human-facing reader separate on
purpose:

| Surface | Language |
| --- | --- |
| Generated directory and file names (`DuoshanChatExport/`, `chat.sqlite`, `messages.jsonl`, `conversation_001_page_001.html`, ...) | English |
| Code, database tables/columns, manifest keys, validation report JSON, CLI and console messages | English |
| Default HTML reader interface: labels, buttons, headings, hints and the `<html lang>` attribute | Simplified Chinese (`zh-CN`) |
| Original chat text, contact names, message content and sender text | unchanged, never translated |

Reader HTML class names and element IDs (`article.message`, `div.bubble`,
`id="msg-..."`, `img.inline-emoji`) stay English because they are code contracts
shared with the stylesheet and the validator; they are not user-visible labels.

Only the interface is localized, so the localized UI can never alter, drop or
reorder any chat data. The interface language is fixed; there is no language
switch, and the export itself does not depend on the chosen UI language. The
manifest and report files stay English so they remain diffable and scriptable.

## Development

```powershell
python -m unittest discover -s tests -v     # standard library only
```

The suite builds real SQLite databases in the verified schema (including a real
`-wal` sidecar), app-data folders and DataBackup archives in a temporary
directory, then drives the library and the CLI exactly as a user would. No real
chat content, account ID or media file is stored in the repository, and nothing
in the tests refers to a fixed analysis path.

See `docs/` for the command reference, the output-format details and
troubleshooting.

## Verification

Aggregated, reproducible results from a complete real backup export -- raw-row
equality, full media decode, reader counters, encoding behaviour, and what could
not be recovered -- are recorded in [docs/verification.md](docs/verification.md).
That page lists only aggregate numbers (no account IDs, message text or absolute
paths), states the limits of unavailable resources, and records successful
wheel/source builds and an offline export from an isolated wheel installation.
