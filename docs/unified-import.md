# Unified chat library

The `import` command accepts WeChat (`com.tencent.mm`), Duoshan
(`my.maya.android`) and Xiaohongshu (`com.xingin.xhs`) backups and merges them
into one reader library. Use the same output directory when importing another
app, another account, or a newer backup. The [desktop GUI](gui.md) uses this
same pipeline.

## Identify and inspect backups

```powershell
python -m duoshan_export scan D:\Backups\com.tencent.mm D:\Backups\my.maya.android D:\Backups\com.xingin.xhs --json
python -m duoshan_export scan D:\Backups --inspect --work-dir D:\ChatImportWork --json
```

`scan` identifies sources without extracting them. Add `--inspect` to read
private database snapshots and report account/message counts. Supported input
layouts include extracted app folders, DataBackup archives, folders containing
several apps, and package folders containing `user_0` / `user_999`. Android
users are processed independently so similarly named archives do not collide.
Single-app exports containing `export_summary.json` and account JSONL files
can also be imported as an interchange format.

Keep output and work directories separate from the selected source roots and
from one another. A nested database/archive selection also protects the
enclosing package folder; a standalone file protects its parent folder.
The importer does not modify original backups.

## Import separately and update later

```powershell
python -m duoshan_export import D:\Backups\com.tencent.mm -o D:\ChatLibrary --work-dir D:\ChatImportWork
python -m duoshan_export import D:\Backups\my.maya.android -o D:\ChatLibrary --work-dir D:\ChatImportWork
python -m duoshan_export import D:\Backups\com.xingin.xhs -o D:\ChatLibrary --work-dir D:\ChatImportWork
python -m duoshan_export import D:\NewBackup\com.tencent.mm -o D:\ChatLibrary --work-dir D:\ChatImportWork
```

Without `--work-dir`, unified import uses the sibling directory `OUTPUT.work`.
The older single-app `export` command has different resume/work semantics;
use `import` for a shared library and subsequent backups.

The importer hashes complete source contents. A previously completed,
unchanged import is skipped. New snapshots merge by account and stable message
identity, keep older messages absent from the new snapshot, and preserve
reader notes, text edits, hidden flags and deletion suppression. A source whose
database merge completed but whose JSONL projection was interrupted is retried
until that projection is complete. An OS lock prevents two importers from
writing the same library simultaneously.

Each source commits independently. If a later source fails or the operation
is cancelled between stages, earlier completed imports remain available.
The work directory and `import_history.jsonl` retain diagnostic history.
Successful local attachments are retained when an offline update lacks them.
Networking is disabled by default; `--download-media` opts into the existing
adapters' supported media downloads. It cannot recover media absent from a
backup or a resource no longer available from its original service.

## Download media after import

The desktop library page has an independent **补下载媒体** action. Enable its
online-download checkbox explicitly, then run it for the current library.
The original backup paths are not needed. The command-line equivalent is:

```powershell
python -m duoshan_export library-media D:\ChatLibrary --download-media --work-dir D:\ChatImportWork --workers 6
```

This command defaults to all live accounts. Repeat `--account UID` to limit the
CLI operation. Without `--download-media` it exits before creating download
artifacts or making a request. The Python API also defaults to
`download_media=False`; the GUI passes `True` only for the explicit action.

Media references come from the current `reader.sqlite`, including committed
reader changes. Stale account JSONL files are never used to restore messages.
Successful files are associated only with a still-present message whose app
and raw-message hash still match. Raw JSON, message IDs, text, annotations,
hidden state and the deletion ledger remain unchanged.

Supported recorded resources include Duoshan voice/stickers/photos and direct
video URLs, Xiaohongshu photos, video/live-photo video, stickers and recognized
card covers, and identifiable unencrypted WeChat media URLs. Quality variants
already recorded in the message are tried in order; signed query strings are
preserved. No fresh URL is requested from a login/API endpoint. A Duoshan video
with only a poster URL can yield the poster while the absent video address is
reported separately. WeChat encrypted CDN tokens/AES-key resources and messages
without URLs are reported as unsupported, even if the original app could fetch
them. Expired or unavailable HTTP resources are failures, not empty successes.

Requests use per-app HTTPS CDN allowlists and validate every redirect. Limits
are 64 MiB for images, stickers and voice, 512 MiB for video, and 128 MiB for
supported file attachments. Images are decoded before publication; audio and
video are probed locally with network protocols disabled. Voice receives a WAV
playback copy when needed, with original bytes retained. Downloaded media is
placed in `attachments/<app>/online/` by full SHA-256. The operation preserves
good existing local media and checks full hashes before journal reuse.

Downloads and conversions stage inside that library's attachments directory,
then publish by rename on the same volume, so a normal successful download
does not leave another full copy in the work directory. Necessary original
and playback derivatives are both retained. Failed partials and occasional
same-content duplicate downloads remain in `attachments/<app>/online/.incoming/`
for diagnosis; they are never automatically deleted. Work retains patch JSONL,
logs/results and other small task files. Any later cleanup still follows the
workspace's confirmation and recycling rules.

Staging directories are created and validated before starting parallel downloads.
Individual worker failures are journaled while other completed resources still
attach to the reader. Directory validation failures include local root/resolved
path diagnostics and a Chinese progress message; they do not expose URLs or
signed tokens. Rerunning the action retries the failed resources.

`library_media_manifest.jsonl` is an append-only recovery journal. Every completed
download is flushed before the operation continues. Cancelled operations keep
completed assets, finish attaching those results, and return `cancelled`.
Repeating the action resumes from verified successful records and repairs any
unfinished reader projection. Existing local media already referenced by a
message is counted without needlessly regenerating its account export.

Results include `totals.downloaded`, `existing`, `failed` and `unsupported`,
a failure list with reasons, and the Node patch result. Counts are resources,
not messages; one resource may be referenced by many messages, and one message
may have both a photo and video. Download URLs, signed tokens and key material
are omitted from logs, results and the recovery journal.

## Account names and media

```text
ChatLibrary/
  reader.sqlite
  wx_<account-id>/messages.jsonl
  xhs_<account-id>/messages.jsonl
  dy_<account-id>/messages.jsonl
  attachments/
  export_summary.json
  import_history.jsonl
  .import-history/
```

The folder prefix records the source. Internal account IDs remain unchanged,
so existing reader annotations and message identities continue to work.
Legacy unprefixed folders remain readable. To organize an existing reader
library, close active imports and run:

```powershell
python -m duoshan_export organize D:\ChatLibrary
```

This adopts the existing database and labels account directories; it is not
an import of previously omitted data. The library rejects a conflicting
account ID assigned to another app rather than merging unrelated accounts.
An older custom WeChat conversion with different message IDs needs its legacy
identity aliases before the first raw-backup update; organizing folder names
alone cannot infer those aliases.

Reader media paths are relative to `attachments/`. Imported assets use content
hashes and verified bytes, preventing same-name overwrites. WeChat images,
video, voice and attachments are resolved within the owning account. Voice
conversion retains the original SILK/AMR bytes and adds WAV playback when
decoding succeeds. Missing media is reported explicitly.
WXGF images retain their original bytes and receive a PNG preview of the first
frame when FFmpeg can decode them. This preview does not preserve animation.
Videos retain their original encoding; playback support depends on the
browser's installed codecs, including HEVC support.

Move or back up the complete library, especially `reader.sqlite`, account
JSONL, `attachments/` and manifests. The database holds annotations, aliases,
import state and deletion suppression. Removing it loses those reader changes
even if the original messages remain available elsewhere. The unified reader
uses this database; a unified import is not a regenerated legacy static HTML
export or a copy of every original app database.

## WeChat decryption and limitations

The adapter reads plaintext `EnMicroMsg.db` and the tested Android SQLCipher
format with 1024-byte pages, PBKDF2-SHA1/4000 and disabled page HMAC. It copies
the database and committed WAL/journal sidecars before opening them. Key
material is normally discovered in shared preferences, MMKV or configuration
files. Unsupported schema, missing key material and failed decryption are
reported as errors, not empty successful accounts.

`scan --inspect`, `import`, `inspect` and `export` accept optional
`--wechat-password`, `--wechat-uin` and `--wechat-imei`. A password overrides
derived keys. The GUI offers session-only fields for the same options; the
CLI does not save them in import history. `--sqlcipher-module-path` selects an
existing Python SQLCipher binding when needed.

Server message IDs supply stable identities across overlapping snapshots.
Messages without a server ID use local ID, timestamp, type, sender direction
and content; a reinstall or backup format that changes all those values can
produce separate records. Unknown message types retain their raw fields.
The adapter preserves raw WeChat values under `_wechat_raw`; `_export` holds
reader display metadata.

## Working files and cleanup

Work contains retained archive metadata, private databases, adapter export
stages and reader staging JSONL. WeChat streams referenced media directly into
the library so it does not need two full media copies. `.import-history`
retains prior manifests and summary files. These retained files explain why
working/export directories can be much larger than the final reader data.

Folder sizes alone do not establish whether a directory is disposable: older
exports can contain additional accounts, unreferenced source media, or
hardlinks shared with the library. Confirm full referenced-media coverage,
account/message coverage and library independence before proposing cleanup.
This importer performs no automatic cleanup. The workspace's confirmation and
recycle-bin rules still apply to any later cleanup.
