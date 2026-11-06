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
- The media patch boundary changes only media metadata on matching live
  account/message rows. Preserve raw JSON, message columns, edits and deletion
  suppression. Multiple media resources must have a stable primary so a repeat
  operation does not rewrite the same account projections unnecessarily.
- Desktop branding is `Chat Archive`. Settings write to
  `%LOCALAPPDATA%/ChatArchive/settings.json`; read the legacy
  `AzureKilnChatArchive/settings.json` only as a fallback and retain it.

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

## Wrong vs Correct

Wrong: set own WeChat sender to `me`, use `msgSvrId` globally, and rebuild all
messages whenever another app is imported.

Correct: set own sender to account UID, scope source keys by account, resolve
canonical aliases, and transactionally merge only the imported account rows.

Wrong: rerun a legacy exporter over the library to attach newly downloaded
files. Correct: read current live rows and apply guarded media metadata patches,
then refresh only affected account projections.
