# Xiaohongshu (com.xingin.xhs) export

The tool exports local Android **Xiaohongshu/RED** backups alongside Duoshan
backups. The same commands (`inspect`/`export`) work for both: with the
default `--app auto` policy the Duoshan pipeline is tried first and is only
bypassed when a backup contains Xiaohongshu app data, so Duoshan behaviour is
unchanged.

```powershell
# Discover accounts (no export)
python -m duoshan_export inspect "D:\backup\com.xingin.xhs\user_0"

# Full export
python -m duoshan_export export "D:\backup\com.xingin.xhs\user_0" -o "D:\XhsChatExport"
```

Xiaohongshu account ids are lowercase hex (`68cf0e5d000000002102b928`), so
`--account` takes hex ids for XHS backups and decimal ids for Duoshan
backups.

## How the app stores its data

A DataBackup archive contains the usual `data.tar.zst`/`user.tar.zst`
members; the chat data lives in the app-scoped tree `com.xingin.xhs/`:

| Path | Contents |
| --- | --- |
| `databases/msgDB` | IM database: `message`, `chat`, `group_chat`, `user`, `chat_set`, `msgheader` |
| `databases/localRelationDB` | contact/relation cache (`local_relation_user`) |
| `databases/hedwig.db` | conversation metadata (not needed for the export) |
| `files/mmkv/com.xingin.xhs_preferences` | MMKV instance holding database passphrases |
| `files/mmkv/login_user_info_kv` | last logged-in account id |
| `files/mmkv/com.xingin.xhs` | current account profile (nickname, red_id) |

### Encryption

`msgDB` and `localRelationDB` are encrypted by the app's WCDB build — a
SQLCipher 4.0-era fork configured through `SQLiteCipherSpec(pageSize=1024,
kdfIteration=64000)` with fork-specific algorithm defaults:

- KDF: PBKDF2-HMAC-SHA1, 64000 iterations, 16-byte salt from page 1
- Cipher: AES-256-CBC, per-page random IV
- Integrity: HMAC-SHA256 over the page image plus a little-endian page
  number, keyed with `PBKDF2-HMAC-SHA1(aes_key, salt XOR 0x3a, 2)`
- Page layout: page 1 = `[salt 16][ciphertext 960][iv 16][hmac 32]`,
  page N = `[ciphertext 976][iv 16][hmac 32]`

The stock `sqlcipher3` wheel (SQLCipher 4.12) derives page HMACs differently
from this fork and cannot open these files, so the tool decrypts the pages
manually (`duoshan_export/apps/wcdb.py`) and re-assembles a plain SQLite
database. This needs the `cryptography` package:

```powershell
pip install "duoshan-chat-export-tool[xhs]"
```

A failed `sqlcipher3` open attempt also rewrites or removes `-wal` sidecars;
the manual pipeline never writes to the source tree.

### Passphrases

The app rotates database passphrases at runtime and stores them as MMKV
byte-array values:

| Database | MMKV instance | Key | Built-in default |
| --- | --- | --- | --- |
| `msgDB` | `com.xingin.xhs_preferences` | `msg_db_password_updated` | `xhsdev` |
| `localRelationDB` | `com.xingin.xhs_preferences` | `relation_db_password_updated` | *(generated)* |

The exported `relations`/password values are read by
`duoshan_export/apps/mmkv.py`, a dependency-free parser for the MMKV
append-log format (including the `0x0FFFFFFF` item-size holder and the inner
`[varint length][bytes]` prefix the app writes inside byte-array values).
When a database has no stored passphrase, the app default is used.

### Multiple accounts in one msgDB

`msgDB` accumulates rows across logins; each row carries its owner account
in the `@` suffix of `local_chat_user_id` (direct chats) and
`local_group_chat_id` (group chats). The export splits those rows per
account, so every account directory only contains its own conversations.
The last logged-in account comes from `login_user_info_kv`
(`login_user_id_key`) and is labelled on the account page.

## What lands in `OUTPUT`

The layout mirrors the Duoshan export (`index.html`, `reader.css`,
`export_summary.json`, `validation_results.json`, `README.txt` and one
directory per account with `chat.sqlite`, `messages.jsonl`, `messages.csv`,
contacts, `conversation_metadata.json`, statistics, per-conversation HTML
pages and TXT transcripts). Differences worth knowing:

- **Media stays remote.** Xiaohongshu keeps IM images, videos and stickers
  on its CDN (`msg-img.xhscdn.com`, ...); the backup contains no IM media
  files, so nothing is downloaded and pages embed the original URLs
  (displayed when online). Every URL is preserved verbatim in the JSONL,
  CSV and SQLite output.
- **`chat.sqlite` is the whole msgDB.** One decrypted database serves every
  exported account of the backup; rows of other accounts remain in it even
  though the HTML/CSV/JSONL views are filtered per account.
- **`--download-media` / `media` do not apply.** The Xiaohongshu pipeline
  finishes at export time and writes its own validation report; the
  Duoshan-only media, decoration and web-reader stages are skipped.

## Verified against

Real DataBackup backup, app package `com.xingin.xhs` (Xiaohongshu 8.x,
2026-11): two accounts, 3017 messages, 11 conversations decrypted and
exported; `messages.jsonl`/CSV/TXT/HTML cross-checked against the decrypted
`msgDB`; `validation_results.json` all checks passed. The fixture in
`tests/test_xhs.py` builds WCDB-ordered encrypted databases with the
`sqlcipher3` wheel and round-trips them through the same manual pipeline.
