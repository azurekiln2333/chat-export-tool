# Desktop cipher evidence and verification

## Implementation boundary

`apps/desktop_crypto.py` accepts only private database snapshots prepared by the
desktop adapter. It implements the documented SQLCipher page format using
`cryptography`, authenticates every page, applies only checksum-valid committed
WAL frames, and verifies SQLite before publishing the result. Partial files
are retained on failure. No secret keys or process-memory dumps are persisted.

Supported profiles:

| Profile | File prefix | Page / codec reserve | KDF | Page HMAC |
| --- | --- | --- | --- | --- |
| WeChat 4 | none | 4096 / 80 | SHA512, 256000 | SHA512 |
| WeChat 3 | none | 4096 / 48 | SHA1, 64000 | SHA1 |
| QQ NT | 1024 bytes, `QQ_NT DB` marker | 4096 / 48 | SHA512, 4000 | SHA1 |
| QQ NT newer profile | same wrapper | 4096 / 80 | SHA512, 4000 | SHA512 |

SQLite can reserve more bytes than the codec uses. SQLCipher 4.12 retains an
80-byte pager reserve when changing its HMAC to SHA1 after `PRAGMA key`;
the codec uses 48 bytes. Preserve the larger authenticated SQLite reserve.
Independent fixtures cover both exact and larger reserve sizes.

Automatic key recovery currently covers running 64-bit Windows `Weixin.exe`
processes using read-only query/read access, raw SQLCipher key literals and
WCDB Config.Cipher objects. A key is accepted only after matching a private
snapshot's page HMAC. No process is launched, injected, suspended or modified.
QQ NT automatic recovery locates the documented SQLCipher codec context by
its numeric profile, follows its salt pointer to a copied database and tests
the cipher context's raw AES key against that database page HMAC. This was
verified with the currently running Windows QQ NT client. Legacy WeChat 3
accepts supplied raw keys/passphrases. Classic QQ/TIM Msg3.0 is explicitly
rejected because it is a separate private cipher.

QQ NT allocates zero-filled capacity beyond its authenticated SQLite logical
page count. Those bytes must not be interpreted as encrypted pages. The QQ
wrapper also shifts SQLite's 1 GiB locking-byte page from 262145 to 262144.
Only that exact all-zero reserved page may be exempt from page HMAC. Databases
crossing this boundary are normalized: a separate read-only Python child
adjusts its own SQLite pending-byte location, verifies the source layout and
streams typed rows to a standard SQLite writer. The parent never changes its
SQLite global setting. Raw integers, text including NUL, blobs, rowids,
sequences, indexes and schema metadata are preserved. Cancellation stops only
the owned helper and retains partial files.

## Primary references and licensing

Sources were inspected as format evidence; no upstream implementation or
debugger scripts were vendored into the package.

- SQLCipher implementation and API: `https://github.com/sqlcipher/sqlcipher`,
  `https://www.zetetic.net/sqlcipher/sqlcipher-api/` (BSD-3-Clause).
- Weixin profiles and Config.Cipher research:
  `https://github.com/fangtimmy/wechat4-db-decrypt`, especially
  `src/wxexport/sqlcipher.py` and `src/wxexport/win/{keyscan,profiles}.py`
  (WTFPL as reported by the repository license).
- QQ NT wrapper, page MAC, message and contact schema research:
  `https://github.com/DaizeDong/qq-history-recovery`, especially
  `tools/qqnt_decrypt.py`, `tools/qqnt_decode.py`, `docs/JOURNEY.md` (MIT).
- Cross-check of QQ NT SQLCipher parameters:
  `https://github.com/anpingjing/qqnt-chat-export/blob/main/scripts/decrypt_db.py`
  (custom license; code not reused).
- QQ key acquisition boundaries:
  `https://github.com/QQBackup/QQDecrypt/blob/main/docs/decrypt/extract/NTQQ%20(Windows).md`
  and `docs/research/NTQQ-Linux-DB-Key-Research.md` in that repository;
  `https://github.com/QQBackup/qq-win-db-key` (custom noncommercial license;
  no scripts executed or incorporated).

The QQBackup Windows guide still obtains the chat database passphrase from
the running client's key call. Its Linux research distinguishes `key_meta`
stored in the wrapper from the actual chat key received through an authenticated
MSF/OIDB session. Neither provides a supported offline derivation of the chat
key from the database alone. The fixed Login.db key is unrelated and must not
be confused with a chat database key. Windows applicability of Linux findings
must not be overstated.

## Observed verification

- Twelve crypto unit tests pass, with independent SQLCipher-created fixtures
  for all four profiles, raw keys, plaintext and encrypted committed WAL,
  valid uncommitted WAL tails, malformed WAL, wrong keys, damaged pages,
  cancellation without publishing, classic QQ rejection, QQ preallocation,
  salt-bound context recovery and normalization with raw-field preservation.
- Real current-account Weixin contact snapshot: 1,318 pages authenticated,
  17 tables, full `integrity_check=ok`.
- Real current-account message shard: 79,237,120 encrypted bytes, 19,345 pages
  authenticated, 145 Msg_ tables, 128,513 rows, full `integrity_check=ok`.
  Actual columns include Name2Id(user_name,is_session), real_sender_id,
  message_content, compress_content, packed_info_data and WCDB_CT fields.
- A second account did not have a corresponding key in running Weixin memory;
  it failed explicitly. Logging into the corresponding account or supplying
  its key remains necessary.
- Real source database and WAL SHA-256 values were unchanged before/after
  validation. All copies, decrypted files, private reports, research downloads
  and fixtures were placed outside the repository under the E-drive workspace.
- A later validation found a running QQ NT client and recovered matching keys
  for both copied profile and message databases without injection or a
  debugger. The contact database was authenticated, merged with 22 WAL pages,
  and passed full integrity_check; profile_info_v6 contains 103,668 rows.
- The real 4 GB QQ NT message database was authenticated and merged with 28
  committed WAL pages. Its shifted lock-page layout passed quick_check in the
  isolated source reader. Standard SQLite normalization copied 6,503,105 rows
  across all tables in approximately 82 seconds; the result passed standard
  SQLite quick_check and full integrity_check and retained 236,455 private-chat and 6,236,280 group-chat
  records. Source-copy hashes were stable during copying. The normalized
  private snapshot was supplied to the media parser for real-data inspection.

## Classic QQ dependency status at the earlier checkpoint

The current installation contains QQ NT, not the old KernelUtil.dll runtime.
The checked C/D Tencent installation locations and existing E-drive workspace
contained no usable classic runtime. The NT wrapper references Msg3.0 and
old-client discovery/migration services, but no verified standalone classic
codec entry point was found. This is not evidence that migration completed.

QQBackup's documented classic offline path requires the corresponding
16-byte classic database key plus a matching 32-bit KernelUtil.dll. Its key
acquisition examples use hooks or debugger breakpoints, which were not run.
The modern SQLCipher raw key does not establish the classic key. An isolated
helper using a supplied classic runtime/key on E-drive copies remains a
possible follow-up; neither missing input was guessed or silently substituted.
Classic message-field parsing is implemented independently by qq_legacy.py.

This dependency finding was superseded by the user's supplied 9.7.23.29348
installer. See `legacy-qq-component.md` for the verified local x86 codec bridge,
actual encrypted fixture tests and remaining real account-key blocker.

Real paths, account identifiers, messages, and keys are intentionally absent
from this note. Private reports remain in the workspace validation directory.
