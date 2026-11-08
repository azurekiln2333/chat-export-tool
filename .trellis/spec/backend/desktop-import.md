# Desktop import contract

## Scope / Trigger

Read for `desktop_sources.py`, `apps/desktop.py`, `apps/desktop_crypto.py`,
PC GUI selection, CLI key inputs and reader source labels. PC adapters remain
independent of existing Android WeChat, Duoshan and XHS adapters.

## Signatures

```python
documents_directory() -> Path
discover_desktop_sources(paths=None) -> list[dict]
database_files(source) -> list[Path]
prepare_backup(source, work, *, progress=print, cancel=None)
inspect_backup(source, work, *, progress=print, cancel=None,
               keys=None, sqlcipher_module_path=None)
export_backup(source, output, work, *, timezone="Asia/Shanghai", progress=print,
              cancel=None, keys=None, sqlcipher_module_path=None, media_output=None)
decrypt_snapshot(source_snapshot, output, *, keys=None, account=None,
                 relative_path=None, app=None, progress=print, cancel=None,
                 sqlcipher_module_path=None)
recover_wechat_keys(snapshot_root, *, progress=print, cancel=None)
recover_qq_keys(snapshot_root, *, progress=print, cancel=None)
desktop_media_files(source, *, cancel=None)
DesktopMediaIndex(account_root, output, work, *, progress=print, cancel=None)
```

CLI: `scan [SOURCES...] --local [--inspect --work-dir WORK]`; `scan`, `inspect`
and unified `import` accept `--desktop-keys JSON`. The JSON root must be an object.
GUI `create_window(..., auto_discover=True)` starts a separate discovery worker;
tests disable it or mock discovery so they never enumerate real accounts.

## Contracts

- Resolve Windows Documents with `SHGetKnownFolderPath` and registry fallback;
  do not hardcode a drive, user home or client storage path.
- Discovery emits `app=wechat_pc|qq_pc`, `kind=desktop`, canonical `source`,
  `account`, `uid`, `account_uid`, `format`, counts, encryption state and status.
  It reads directory entries and headers only; no process scans or extraction.
- Formats: `wechat3`, `wechat4`, `qqnt`, `qq_legacy`. Only known DB directories
  and sidecars are snapshotted, never arbitrary received `.db` attachments.
  Direct `nt_qq/nt_db/nt_msg.db` selection maps to the enclosing account, not
  the `nt_qq` folder. Account+DB overlapping selections deduplicate in either order.
- UID = first 32 lowercase hex characters of SHA256(app + NUL + account).
  WeChat 4 canonical account removes a four-hex installation suffix. The PC
  source fingerprint policy includes this UID so identical DB bytes in two
  accounts do not authorize skipping the second account.
- Private work/output roots must not overlap sources or each other. Copy DB,
  WAL and sidecars before opening SQLite or recovering keys; full source and
  destination hashes verify a stable snapshot. Retain all retry attempts.
- Every encrypted page is HMAC-verified. Replay only checksum-valid committed
  WAL frames; ignore a valid uncommitted tail. Failed decrypt/verification
  retains partials and never publishes a successful destination file.
- WeChat 4 and QQ NT memory recovery use only read/query process access and accept
  only candidates verified against snapshot HMAC. Never inject, hook, suspend
  or modify the client, persist a key or save a process-memory dump. Explicit
  keys bypass memory scanning; GUI/CLI logs/settings/history never contain them.
- QQ NT supports SHA1/reserve48 and SHA512/reserve80 codec profiles. Only
  authenticated logical pages, committed WAL and the exact shifted lock page
  determine normalization. Databases crossing the QQ wrapper's 1 GiB lock page
  are streamed through a separate read-only SQLite helper into standard SQLite;
  never alter the GUI process's SQLite global pending-byte configuration.
- WeChat 3 requires supplied keys or plaintext DBs. Classic QQ Msg3.0 uses a
  different private cipher; NT credentials cannot decrypt it. Already decrypted
  classic tables are parsed by `qq_legacy`, including bounded TLV bodies and raw
  unknown fields. Snapshot NT and classic databases together when both exist.
- Mixed QQ message decryption failures yield `status=partial`, with
  `failed_databases=[{database,format,error}]`; readable records can be merged.
  Single-format failure or no readable message database fails explicitly.
  Partial batches use `partial:<source_id>` and cannot authorize the unchanged
  shortcut. Staged `_library_import={source_id,status}` participates in reader
  fingerprinting so newly recovered subsets are retryable and independently committed.
- Outgoing normalized `sender` equals internal UID. Preserve original fields
  and binary values (base64) in `_desktop_raw`; `_export` owns display fields.
  Unknown types/content are retained. Critical message failures propagate;
  optional contact failures produce warnings. Empty known auxiliary shards
  may be ignored, but unknown message schemas cannot report empty success.
- Server message identity is app+conversation+server ID; local fallback also
  includes source/table, message context and raw body digest to avoid sequence
  and unknown-display-placeholder collisions. QQ temporary, dataline, service,
  AI and discussion messages retain separate namespaces from ordinary chats.
  Classic QQ uses a separate `qq_pc:legacy:` namespace: neither Rand/MsgSeq nor
  similar timestamps prove equivalence to NT IDs. Never assume migration.
- QQ direction is established from positive `40033` sender QQ or verified
  `40020` UID mapping; `40013` is not direction. Unknown senders remain unknown.
  Standard QQ faces without media references retain their face ID and do not
  imply an absent attachment. Real media MD5 may be binary field `45406`.
- Account prefixes are `wxpc_` and `qqpc_`; reader source labels are PC 微信
  and PC QQ. Keep PC and Android identities independent. Reader notes, edits,
  absent older history and deletion suppression survive subsequent snapshots.
- QQ media matching uses only the owning account's explicit cache roots, with
  junction/symlink containment checks. Match recorded path/MD5/resource hashes;
  never infer a historical match from an ordinary basename alone. Source and
  E-drive copies are hash verified; decode only copies. Publish content-addressed
  attachments, label `variant=thumbnail` honestly, and retain original voice bytes
  when generating browser playback derivatives. PC WeChat media remains unavailable.
- Full QQ cache contents join the source fingerprint, including same-size and
  same-mtime changes. A cache-only update must rerun import and enrich existing
  records. Cached originals must survive a later snapshot containing only thumbs.
- GUI settings default to `PROJECT/.work/gui/settings.json`, overridden by
  `CHAT_ARCHIVE_RUNTIME`. Legacy LOCALAPPDATA preferences are read-only fallback.
  The source launcher places TEMP/TMP under the same portable runtime.

## Validation & Error Matrix

| Input | Result |
| --- | --- |
| Redirected Documents with known accounts | Separate selectable rows; no writes |
| No selected rows | GUI rejects before creating work |
| Output/work inside source | Reject before creating directories |
| Concurrent source mutation | Retained retry, then explicit failure after 3 tries |
| Wrong key, corrupt page or unsupported WAL | No published decrypted DB |
| Missing active account key | Actionable failure; other completed imports retained |
| Encrypted classic QQ without a decoder / unknown critical schema | Explicit failure; mixed readable QQ may be partial |
| Decrypted classic + NT in one account | Both parsed with independent message identities |
| DB unchanged, new or changed local cache | Reimport and enrich local media |
| Only thumbnail available | Render labelled thumbnail, never claim original |
| Same account, new server IDs | Add records and retain absent history/annotations |
| Different accounts, byte-identical DBs | Two independent imports |

## Good / Base / Bad Cases

Good: select one auto-discovered account, copy to a separate work volume,
decrypt with a verified key, import into the existing library, then add newer
records without rewriting Android rows.

Base: a plaintext synthetic QQ NT database with textual or protobuf bodies
exports through the same private snapshot boundary and reader merger.

Bad: scan Documents and silently import every account, label a failed legacy QQ
decrypt as zero messages, or display PC media as recovered when no asset exists.

## Tests Required

- `test_desktop`: known layouts, redirected Documents, nested selection,
  source hash preservation, WAL-only records, own/group senders, zstd, raw
  integers/blobs, unknown messages, schema failures and stable IDs.
- `test_desktop_crypto`: independently SQLCipher-produced profiles and WAL;
  wrong keys/corrupt pages/uncommitted tails/cancellation leave no publication.
- `test_desktop_integration`: real library-to-Node merge with all five app
  labels, independent same-content accounts, newer snapshot history, mixed
  classic/NT, cache-only updates and partial-to-complete retries.
- `test_qq_legacy`: all MsgContent tables, TLV boundaries, sender/ID correctness,
  unknown/damaged bodies preserved, independent classic identity.
- `test_desktop_media`: recorded path/hash matching, cache-index loss recovery,
  originals/thumbnails, exact copy hashes, containment and failed decoding.
- GUI tests: unchecked automatic rows, checked manual rows, selection retained
  across refresh, checked-only Android siblings, key redaction and no persistence.
- Node import tests: PC prefixes/source labels, edits and direction after reopen.
  Fixtures contain no real IDs, messages, keys or paths.

## Wrong vs Correct

Wrong: run SQLite on the live Documents database and create its WAL beside it.
Correct: copy a verified DB/WAL set into `work`, then open only private files.

Wrong: infer QQ chat key from the unrelated fixed Login.db key or reuse NT keys for classic.
Correct: accept only matching supplied/recovered NT keys verified by page HMAC;
classic encrypted data needs a separately verified codec/key.

Wrong: mark a successfully imported NT subset as completion of a mixed account.
Correct: retain its messages, report unread databases, and retry that source later.
