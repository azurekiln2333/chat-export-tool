# Unified import contract

## Scope / Trigger

Read this when changing `library.py`, multi-app CLI routing, the WeChat
adapter, GUI importer calls or reader merge integration. Existing Duoshan/XHS
single-app export contracts remain in effect.

## Signatures

```python
discover_sources(paths) -> list[dict]
inspect_sources(paths, work, *, progress=print, cancel=None,
                sqlcipher_module_path=None, password=None, imei=None, uin=None)
import_sources(paths, output, work, *, timezone="Asia/Shanghai", progress=print,
               cancel=None, download_media=False, sqlcipher_module_path=None,
               node="node", password=None, imei=None, uin=None)
organize_library(output, *, node="node", progress=print)
import_history(output) -> list[dict]
download_library_media(output, work, *, accounts=None, progress=print,
                       cancel=None, node="node", workers=6,
                       download_media=False)
inspect_library_media(output, *, accounts=None, progress=print,
                      cancel=None, active_download=None)
ProgressEvent(text, *, stage, completed=None, total=None, unit="个",
              state="running")  # str subclass; state also completed/failed/cancelled
```

`scan`, `import` and `organize`/`adopt` are additive CLI commands. Preserve
legacy command behavior. Import returns `status`, `imports`, `output` and
`reader_database`; inspect returns `status` and `sources`. CLI JSON remains a
single stdout object with progress on stderr.

Post-import downloads use `library-media OUTPUT --work-dir WORK
--download-media [--account UID ...] [--workers 1..16] [--node NODE] [--json]`.
The result includes `status`, `totals` (`downloaded`, `existing`, `failed`,
`unsupported`), `failures`, `manifest`, `patch_result`, `recovery_result` and
the retained work run. The GUI uses the same explicit opt-in service.

Offline details use `library-media-report OUTPUT [--account UID ...]
[--active-download] [--json]`. `inspect_library_media` returns `status`,
`as_of`, `completed_at`, `data_status`, `snapshot_note`, `journal_snapshot`,
`totals`, `resources`, and unique `report_html`, `report_text`, `report_json`
paths under `OUTPUT/media_reports/`. Resource details include `recorded_urls`,
`attempts`, `attempts_known`, `attempted_urls`, `reason_text`, and current
`messages` references. `attempted_urls` includes only actual requests; a
redirect denied before networking is recorded as `requested: false`.
Legacy entries without attempts keep `attempts_known: false`, even when
recorded candidates can be recovered from current messages. HTML renders
escaped text with no external resource loads. GUI details use plain text,
filters, pagination and explicit URL copying; their separate read worker may
run while downloading and never changes the network opt-in.

The Node boundary is `importLibrary({sourceDir,outputDir,app,sourceId,
project=true,preserveExisting=false})` and `organizeLibrary({outputDir})`.
The reader persists `library_accounts(uid,app,directory)`, message aliases
`(uid,source_key,msg_uuid,conversation_id)`, conversation aliases
`(uid,source_key,conversation_id)`, and `import_batches` with fingerprint,
source, account, result and projection state. `store_meta.library_mode` is
`incremental-v1`: the SQLite database is authoritative even after `--reindex`.

## Contracts

- Validate original selections before discovery splits `user_0`/`user_999`.
  Protect enclosing package roots for nested selections and the parent of a
  standalone file. Output/work cannot overlap a protected source or each
  other. Never create work files before these checks pass.
- Use a fresh retained per-source stage for legacy adapters. Do not run a
  resume/rerender branch against the live unified library.
- Source identity hashes complete contents; only committed `import_batches`
  with completed projections authorize the unchanged shortcut. A history log
  entry alone is insufficient.
- Media is contained under `attachments/`, content addressed and hash
  verified. Write a uniquely named partial then rename only after validation.
  WeChat may write verified final media directly into the library; stage
  rewriting accepts those library-relative paths without a second copy.
- Rewrite only adapter metadata and manifests, preserving raw JSON integers
  and original message columns. Offline failures cannot replace good assets.
- Hold the OS library lock for the mutation. The Node merger owns message
  transactions and projection recovery; Python retains old root metadata and
  restores it only when no DB commit occurred. Never erase earlier sources
  when a later source fails.
- Imported account directories use `wx_`, `xhs_`, `dy_`; internal IDs do not
  change. Preserve legacy identity aliases before importing raw WeChat over an
  older custom export. Folder organization alone cannot derive aliases.
- Credentials are passed in memory, never persisted in events/logs/settings.
  Discovery is offline; downloading is explicit opt-in.
- Desktop launcher dependency probes must support Windows PowerShell 5.1:
  use hidden processes, drain stdout/stderr concurrently, bound each probe,
  and choose by exit code. Native stderr must not prevent Python fallback.
- No automatic deletion or pruning. Retain stages and previous metadata for
  diagnosis and require workspace-compliant confirmation for later cleanup.
- Long import stages emit string-compatible `ProgressEvent` callbacks, including
  start/end and five-second liveness during blocked I/O or codec calls. Count only
  actual processed units; use `total=None` for unknown archive members/merge rows.
  Report bytes where exact record totals are unavailable without another scan.
  The GUI reads attributes before string redaction and uses queued Qt signals;
  it shows phase counts/percentage separately from overall task completion.
  Stop and join heartbeat threads before leaving the phase, even on error or
  cancellation. Never replay an old phase heartbeat after the next phase begins.
- Reader subprocess stderr streams prefixed structured events while stdout stays
  one result JSON. Drain both pipes concurrently, keep transactions running if a
  display callback fails, and join readers before returning. Only a zero exit
  code and a result object with `status=completed` authorize final completion.
- Retrying a cancelled unified import starts a new retained run; prior final
  attachments are hash-verified for reuse, but private scan/conversion markers
  are run-scoped. Do not describe this as full checkpoint resume or imply that
  attachments alone establish a completed reader import.
- WeChat source identity is account-scoped: server ID plus talker when
  available; otherwise local ID, timestamp, type, direction, talker and raw
  content. Outgoing `sender` must equal the internal account UID, because
  the reader's bubble direction compares those values. Preserve all original
  WeChat columns under `_wechat_raw` and display metadata under `_export`.
- Hash metadata/snapshot caches before reuse. Publish complete media files
  atomically after SHA-256 verification; retain failed partials. Original
  SILK/AMR remains available alongside optional WAV playback derivatives.
- Projection preserves original JSON text where possible. A versioned
  `_library_projection` records canonical fields when aliases or merged media
  require an override, while the raw original remains unchanged.
- Post-import media retrieval reads live `reader.sqlite` rows. A missing backup
  selection is valid; old JSONL cannot authorize restoration of a deleted row.
  Networking requires explicit opt-in, including from the library GUI action.
  Never persist that opt-in across launches.
- Group recorded media alternatives by app/resource. Preserve signed query
  strings, validate every redirect against app CDN policy, bound streamed
  bytes, and validate the asset before publishing. Arbitrary links in chat
  text are not media download requests. Report unavailable URLs and unsupported
  encrypted resources explicitly.
- Durable `library_media_manifest.jsonl` records download results and hashes.
  Validate reused assets by full content hash, cache repeated checks within a
  run, and preserve completed files on cancellation. Scan progress must remain
  visible for large libraries.
- Final online assets use `attachments/<app>/online/<hash-prefix>/<sha256>.<ext>`.
  Stage downloads/conversions on the library volume and publish by rename;
  the work directory holds patches/results, not duplicate successful media.
  Preserve original audio alongside playback derivatives. Failed/duplicate
  incoming files remain available for explicit later cleanup.
- Prepare and validate incoming directories before starting parallel workers;
  serialize shared-directory publication and retain containment checks. An
  individual worker/path failure must be recorded without losing successful
  results from other workers. Record resolved-path diagnostics locally without
  exposing signed resource URLs in exception messages.
- User-requested media details and direct download logs display full recorded
  and attempted URLs, preserving signed query strings. Keep them in local task
  logs, diagnostic reports and journal/result fields, never in source-controlled fixtures, arbitrary
  exception text or reader display metadata. Provide Chinese reason labels
  alongside stable machine codes. Missing URLs remain explicitly absent.
- Emit status-prefixed per-attempt/resource URL lines directly through the
  download progress callback, not only aggregate counters or a report link.
  Use `[✓]` for a validated download, `[x]` for a failed attempt, `[!]` for
  unsupported/no URL, and `[=]` for local reuse. Put the full URL immediately
  after the prefix and the reason afterward. Redirects and candidates that
  were not requested must not be presented as completed requests; reused
  files must not replay historical failures as current attempts. Aggregate
  progress stays throttled; direct outcome lines remain observable. The GUI
  appends them as plain text without replacing compact aggregate status labels.
- Offline diagnostic snapshots use the latest journal entry per resource and
  current live inventory. For legacy entries, reconstruct recorded candidates
  only; do not claim they were attempted. A partial final journal line is
  ignored. Viewing/exporting details must not fetch URLs, acquire the importer
  mutation lock, restart the downloader, or write to message tables.
- The media patch boundary changes only media metadata on matching live
  account/message rows. Preserve raw JSON, message columns, edits and deletion
  suppression. Multiple media resources must have a stable primary so a repeat
  operation does not rewrite the same account projections unnecessarily.
- Desktop branding is `Chat Archive`. Settings write to
  `%LOCALAPPDATA%/ChatArchive/settings.json`; read the legacy
  `AzureKilnChatArchive/settings.json` only as a fallback and retain it.
- Library-page selection refreshes accounts and reloads a running reader owned
  by the current GUI. Same-library reload is explicit. Stop only the saved
  `Popen` handle and poll asynchronously with a bounded wait; never enumerate
  or stop unrelated Node processes. Reuse the managed reader port after exit.
  Reader launch requires the selected library and Node, not backup/work paths.
- New reader ports and reused ports must exclude 64967–65066 inclusive. Closing
  the GUI cancels its tasks and stops only its saved reader Popen handle; wait
  asynchronously, escalate that same handle after a bounded grace period, and
  keep the window open with an error if shutdown fails. Disable new actions
  while closing. Never enumerate or kill unrelated Node processes.
- Desktop account rows display a source column with `多闪/抖音`, `微信`, or
  `小红书`. Prefer authoritative `library_accounts.app`, then explicit legacy
  directory/metadata evidence; never infer source from an account ID's shape.
  The library page has no directory-explanation card.

## Validation / Error Matrix

| Condition | Behavior |
| --- | --- |
| Selected package is split into Android users | Protect the whole selected package before creating work |
| Output/work/media intersects source | Fail before writing source files |
| Unsupported schema / wrong key | Explicit error, never an empty successful account |
| Same-size media or cached DB corruption | Fail or quarantine and rebuild private cache; never reuse silently |
| Repeated completed backup | Skip unchanged import; no duplicate rows |
| New backup omits older messages or media | Keep prior history and successful assets |
| Existing edit or deletion ledger | Preserve edit and suppression by canonical alias |
| Projection interrupted after DB commit | Mark pending and retry projection without repeating DB changes |
| Managed library is reopened with force reindex | Read authoritative DB, do not replace it from stale JSONL |
| Offline import followed by explicit library download | No source selection needed; download and attach supported recorded resources |
| Download cancelled or projection interrupted | Keep completed assets and durable state; resume without importing messages |
| Row deleted or raw content changed after inventory | Skip its media patch; never restore stale message content |
| Existing local photo plus missing video in one message | Reuse the photo, retrieve the missing video, keep a stable primary |
| A concurrent download worker fails | Record its failure; journal and attach other completed downloads |
| Details are requested while downloading | Read a finite journal snapshot and current SQLite snapshot; label ongoing results and allow a later refresh |
| A legacy journal lacks attempted URLs | Show recorded candidates and explicitly unknown historical attempts |
| A resource is absent from current message rows | Label its journal entry historical; do not restore messages or treat historical references as current |
| A signed URL contains HTML markup | Preserve the full URL as escaped/selectable text; never render it as markup or load it |
| A selected library differs from the managed reader | Refresh account data, stop only that reader and relaunch against the selected library |
| A managed reader does not exit before timeout | Report that switching did not complete; retain its ownership and do not launch a second process |
| A known-count stage is running | Show actual phase completed/total and percentage, never a guessed whole-import percentage |
| Archive traversal or a codec blocks between updates | Keep a five-second liveness log with last confirmed counts and current action |
| A cancelled first import left attachments but no reader DB | Preserve files; report incomplete import and the fresh-run retry limitation |
| Reader exits zero with invalid JSON/null/error result | Fail without emitting final merge completion |
| GUI closes while it owns a reader | Stop that handle, wait asynchronously, and leave unrelated readers alone |

## Good / Base / Bad Cases

- Good: import three apps independently, then a newer backup; only new message
  identities appear and other apps' rows and annotations stay unchanged.
- Base: plaintext WeChat fixture with no media still exports raw rows and
  correct sender direction, reporting unavailable media.
- Bad: deduplicating server IDs across distinct accounts, regenerating UUIDs
  before legacy alias adoption, or inferring disk savings from hardlink totals.
- Good: import offline, then enable library downloading and retrieve supported
  media into the same library without access to the old backup.
- Bad: toggling the checkbox starts networking, or replaying old JSONL restores
  a message the reader has already deleted.

## Tests Required

`test_library.py` covers multi-app/user discovery, full content fingerprints,
source-root protection, verified media, locks, independent app imports,
repeat/newer imports and retained edits/history. `test_wechat.py` independently
constructs plaintext and SQLCipher/4000/no-HMAC fixtures, negative binary UIN,
WAL-only rows, account-scoped image/video/voice/files, zstd archives, unknown
schemas, wrong keys and actual reader imports. Original fixture hashes must
remain unchanged. Run the full Python suite and the Node importer tests after
changes to the merge boundary.

`test_library_media.py` covers live-library media extraction, download/reuse,
interruption, expiry, bounded redirects, asset integrity and actual Node
metadata patches with preserved raw rows/edits/deletions. GUI checks cover
legacy settings, opt-in gating, worker errors/cancellation and no-source use.
Run Qt cases with the GUI dependencies available; backend-only installations
may skip only those optional GUI cases. Node media-patch assertions are listed
in the reader contract.

Details tests cover missing URLs, signed query preservation, denied redirects,
HTTP and asset-validation reasons, latest-entry selection, torn journal lines,
removed messages, unique report names, no networking/DB mutations, and exclusion
of diagnostic fields at both Python and Node media patch boundaries. Qt checks
exercise independent details/download workers, filters, pagination and copying.
Reader-control checks cover library selection, same-library reload, bounded
shutdown failure, invalid destinations and independent Node processes. Source
labels must prefer registered identity over conflicting legacy metadata.

Progress tests cover blocked archive/codec callbacks, exact totals, cache reuse,
unknown totals, cancellation and error shutdown with no heartbeat remaining.
Library tests independently reproduce fingerprints, preserve BOM/raw integers
while measuring bytes, and stream Node stderr before completion. Qt tests cover
phase changes and final status, reserved-port boundaries, shutdown escalation
and refusal to start new work while closing. Use isolated fixtures; never restart
a live import to test progress.

## Wrong vs Correct

Wrong: set own WeChat sender to `me`, use `msgSvrId` globally, and rebuild all
messages whenever another app is imported.

Correct: set own sender to account UID, scope source keys by account, resolve
canonical aliases, and transactionally merge only the imported account rows.

Wrong: rerun a legacy exporter over the library to attach newly downloaded
files. Correct: read current live rows and apply guarded media metadata patches,
then refresh only affected account projections.

Wrong: present current recorded candidates as proven historical HTTP attempts.
Correct: distinguish recorded, requested, denied and unknown attempt data, and
keep full diagnostic URLs in local reports rather than reader metadata.

Wrong: log one extraction banner, leave an indeterminate bar through all later
stages, or call 100% extracted files a completed import. Correct: show each stage
and its actual counters; only the final successful service result finishes the task.
