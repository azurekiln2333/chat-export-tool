# Troubleshooting

Machine-facing names (files, directories, manifests, report keys, console
messages) are English; the HTML reader interface is Simplified Chinese. When a
message below quotes a JSON key or a file name it is literal; when it describes
something you see in the browser, it refers to the Chinese interface text by its
purpose, not by an exact string. Reader text is never part of the exported data,
so changing the interface cannot change your chat content.

Start with `duoshan-export doctor`. It reports the Python version, whether each
dependency is importable, and whether `ffmpeg`/`ffprobe` were found.

## "Encrypted Duoshan databases need the SQLCipher extension"

The backup holds SQLCipher-encrypted databases and the binding is not installed.

```powershell
pip install "sqlcipher3-wheels"
# or, without pip installing into the environment:
duoshan-export export SOURCE -o OUTPUT --sqlcipher-module-path "C:\path\to\site-packages"
```

`--sqlcipher-module-path` must point at the folder that *contains* the
`sqlcipher3` package. Plain SQLite databases need no extra dependency.

## "Zstandard-compressed backups need the 'zstandard' package"

The source is a `*.tar.zst` archive. Install the declared dependency:

```powershell
pip install zstandard
```

## "Unknown timezone"

`--timezone` needs an IANA zone name such as `Asia/Shanghai`, `UTC` or
`Europe/Berlin`. Install `tzdata` if the system has no timezone database.

## "Output directory is not empty"

A new export needs an empty (or non-existent) directory. Either choose another
`-o` path, or, if this output was created by this tool from the same source, add
`--resume`.

## "The source backup has changed or differs from this export"

`--resume` refuses to mix two different backups in one folder, because the
databases, indexes and manifests would contradict each other. Export the changed
backup into a new directory.

## "The account selection differs from this export"

The output was created with a different `--account` list. Re-run with the same
accounts, or export into a new directory.

## "Refusing to extract outside the work directory" / skipped archive members

Archives are treated as untrusted. Members with absolute paths, `..` traversal,
Windows reserved names, drive letters, links/devices or absurd sizes are skipped
and listed in the skip statistics; nothing is written outside the work directory.
A legitimate backup never contains such members, so a skip almost always means a
broken or hostile archive.

## "The work directory must be outside the source path"

Extraction and snapshots must not write into the backup you are reading.
Point `--work-dir` somewhere else, or leave it at the default `OUTPUT/.work`.

## Pages show a "bubble asset not preserved" or `missing_url` note

Those notes are reader-interface text (Simplified Chinese by default); the
`missing_url` value is the manifest `status`.

The bubble artwork or media never existed in the backup, or its URL has expired.
That is a data limitation, not a tool failure: the message text, the bubble ID and
the original URL are still in JSONL and the database. Re-run with
`--download-media` if the URL may still be alive, or import verified files from a
previous export with `--reuse-media`.

## HEIC/HEIF media is skipped

Some backups store images as HEIC. Install the optional decoder:

```powershell
pip install "pillow-heif"
```

## Voice messages do not play in the browser

The file is available offline; the browser codec is the restriction. Use the
voice message's fallback link (the local file link next to the player) to play it
in a local player. Converting is optional and requires ffmpeg:

```powershell
duoshan-export media OUTPUT --ffmpeg "C:\ffmpeg\bin\ffmpeg.exe"
```

## `validate --decode-media` fails with "ffmpeg not found"

Install ffmpeg and make sure it is on `PATH`, or pass the executable with
`--ffmpeg`. Without ffmpeg, plain `validate` (no `--decode-media`) still checks
existence, byte size and SHA-256 for every media file.

## Validation reports `dimensions_mismatch` or `format_expected`

These are warnings, not integrity failures: a stored file's intrinsic size or
format label may differ from the display size recorded for the original resource
(for example a converted HEIC->PNG). Integrity checks are existence, byte size,
SHA-256 and successful decoding.

## Long numbers look wrong in a spreadsheet or JavaScript

Message and server IDs exceed JavaScript's safe integer range and spreadsheets
may round them. JSONL and SQLite keep the exact values; use Python, SQLite or a
big-integer-aware JSON reader. CSV cells may carry a leading apostrophe to stop a
spreadsheet from mangling an ID or executing message text as a formula.

## The times are in the wrong zone

Times are formatted with `--timezone`; the raw epoch values are unchanged in the
database and JSONL. Re-run the export (or `media`, which reuses the stored
timezone) with the zone you want.