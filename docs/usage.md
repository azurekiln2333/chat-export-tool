# Command reference

Every command is available as `python -m duoshan_export <command>` or through
the installed `duoshan-export` console script. `run.ps1` forwards to
`python -m duoshan_export`.

Common conventions:

- Paths are resolved and may use `~`.
- `--json` prints a single JSON object to stdout and sends progress to stderr.
- Exit code `0` means success, `1` means an error or a failed validation, and
  `130` means interruption (a resumed export can continue).
- `--version` prints the tool version.
- Generated file and directory names, manifest keys, report keys and all console
  messages are English. The HTML reader interface is Simplified Chinese
  (`<html lang="zh-CN">`); original chat text is never translated. A Chinese
  quick start is in `README_zh-CN.md`.

## inspect

```powershell
duoshan-export inspect SOURCE [--work-dir DIR] [--sqlcipher-module-path DIR] [--json]
```

Discovers accounts and counts messages without exporting. With `--work-dir`, the
archive is extracted there (and kept); otherwise a temporary directory is used
and removed. The work directory must be separate from the source.

JSON result: `source`, `app_directory`, `accounts[]` (with `uid`, `database`,
`contacts_available`, `message_count` or `inspection_error`) and a `note`.

## export

```powershell
duoshan-export export SOURCE -o OUTPUT
    [--account UID ...] [--timezone TZ] [--page-size N] [--resume]
    [--download-media] [--reuse-media EXPORT] [--decode-media]
    [--work-dir DIR] [--workers N] [--sqlcipher-module-path DIR]
    [--ffmpeg NAME] [--ffprobe NAME] [--json]
```

Steps performed automatically, in order:

1. Extract/snapshot the source into the work directory.
2. Decrypt each selected account into `OUTPUT/<uid>/chat.sqlite`.
3. Write JSONL/CSV/contacts/statistics and render the HTML pages.
4. Prepare media and decoration manifests and copy local media.
5. Rebuild the reader and refresh the SQLite media index.
6. Validate the export; abort with exit `1` if validation does not pass.

`--resume` is required to reuse an existing output, and only for the same
unchanged source with the same `--account` selection.

## media

```powershell
duoshan-export media OUTPUT [--source SOURCE]
    [--download-media] [--reuse-media EXPORT] [--decode-media]
    [--work-dir DIR] [--workers N] [--sqlcipher-module-path DIR]
    [--ffmpeg NAME] [--ffprobe NAME] [--json]
```

Updates media for an export that already exists: imports reused media, optionally
downloads new resources, rebuilds the reader, refreshes the SQLite media index and
validates. Without `--source` it does not need the original backup. Passing
`--source` lets it refresh local decoration assets from the backup.

## validate

```powershell
duoshan-export validate OUTPUT [--decode-media] [--ffmpeg NAME] [--json]
```

Independent check of an existing export. It verifies:

- SQLite integrity (`PRAGMA integrity_check`) and exact equality between every
  `chat.sqlite` message row and its JSONL line, by raw-field digest.
- Row counts across JSONL, CSV and SQLite, and page/account statistics.
- Reader pages: every rendered message, page navigation, local links (and that
  no link escapes the export tree), HTML escaping and the absence of remote
  media references in media tags.
- Media manifests: every listed file exists, its byte size and SHA-256 match, and
  each message that declares local media is actually rendered with it.
- Optional `--decode-media`: audio is decoded with ffmpeg and every image frame
  is decoded with Pillow (including animated frame counts).

Reports are written into `OUTPUT`:

| File | Contents |
| --- | --- |
| `validation_results.json` | Data integrity; always written |
| `reader_media_validation_results.json` | Reader/media cross-checks; always written |
| `media_validation_results.json` | Per-file decode results; with `--decode-media` |

Each report contains `status` (`passed`/`failed`), `problem_count`,
`problem_counts`, a bounded sample of `problems[]` and the collected `totals`.
Structural problems (missing directory, missing summary, no accounts) raise an
error instead, so the CLI exits `1` with a clear message.

## doctor

```powershell
duoshan-export doctor [--sqlcipher-module-path DIR] [--ffmpeg NAME] [--ffprobe NAME] [--json]
```

Reports the Python version and, for each dependency, whether it is available and
how to install it if not. Also reports whether `ffmpeg`/`ffprobe` were found.
Exits `1` when a core dependency (Pillow, requests, zstandard, tzdata) is missing.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Error, failed validation, or `doctor` found a missing core dependency |
| `130` | Interrupted; resume with `--resume` from the same source |