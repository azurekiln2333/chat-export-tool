# Verification

This page records what has been verified on a **complete real backup export** and
what is still open. It contains aggregate numbers only: no account ID, message
text, contact name or absolute backup path is reproduced here. You can reproduce
every number on your own export with the commands below.

## How the results were produced

1. A complete, unmodified Android DataBackup of a real account pair was exported
   with the tool in offline mode, reusing verified media from an earlier export
   through `--reuse-media`. No new network requests were made for this run.
2. `duoshan-export validate OUTPUT` checked data integrity, raw-row equality,
   local links and reader/media consistency.
3. Full decode validation, also exercised by `media --decode-media --json`,
   decoded every successful manifest voice resource and every image frame,
   including decoration assets, with `ffmpeg` and Pillow.
4. The previous export's reader was restored to Chinese. Its databases and
   media manifests were compared before and after by SHA-256. Both old and new
   exports have identical original `msg` table fingerprints. The source backup
   signature is unchanged, and the new tool run state is `completed`.

## Results

| Check | Result |
| --- | --- |
| Messages exported | 65,724 |
| Accounts | 2 |
| Conversations | 75 |
| `chat.sqlite` vs `messages.jsonl` raw-field differences | 0 |
| Validation problems (`validation_results.json`) | 0 (`status: passed`) |
| Voice messages decoded | 469 |
| Sticker/emoji image resources decoded | 4,409 |
| Image frames decoded in total | 44,104 |
| Animated images (frame count verified) | 1,318 |
| Decoration assets decoded (bubbles, corners, inline emoji) | 488 |
| Media resources explicitly marked unavailable | 121 |
| `<audio>` elements rendered by the reader | 471 |
| -- of which manifest voice messages | 469 |
| -- of which original local audio attachments | 2 |
| HTML pages declaring `lang="zh-CN"`, in each export | 172 (all) |
| ASCII paths in the restored previous export | 5,658 (all); new export paths also all ASCII |
| Previous `chat.sqlite` and media manifests after reader restoration | byte-identical (hash unchanged) |

The 2 extra audio elements are legitimate **original local audio attachments**
copied from the backup, not voice resources. The validator therefore compares the
summary `voice_ok` counter (469) with the number of manifest voice messages, and
reports the total audio element count (471) separately. Real attachments are
never removed to force the counters to match.

## What could not be recovered

121 media resources are explicitly recorded as unavailable because the backup did
not contain them, the original URL had expired, or the server returned an error.
For each of them:

- the reader shows the reason instead of inventing media,
- the message text, bubble ID and original URL stay in `messages.jsonl` and
  `chat.sqlite`,
- `media_manifest.json` keeps the failed entry with its status.

These files cannot be reconstructed from the export alone. Media that the app
never cached, history removed from the database, and expired server-side URLs are
outside what any offline tool can recover. Stickers or bubbles whose asset was
not preserved fall back to the default style; nothing is fabricated.

## Reproduce on your own export

```powershell
duoshan-export export SOURCE -o OUTPUT
# To reuse media that has already been recovered:
duoshan-export export SOURCE -o NEW_OUTPUT --reuse-media EXISTING_EXPORT
duoshan-export validate OUTPUT --json
duoshan-export validate OUTPUT --decode-media --json
```

- `validation_results.json` reports `status`, `problem_count` and the per-account
  `raw_fields_match` flag (raw `msg` equality against `messages.jsonl`).
- `media_validation_results.json` reports the per-file decode results and the
  summary counters (`voice_decoded`, `images_decoded`, frame counts and so on).
- `reader_media_validation_results.json` reports the summary-vs-rendered reader
  counters, including `audio_elements` as a separate total.

No rerender or re-export is needed to run `validate`.

## Encoding

Exported files (`messages.jsonl`, `messages.csv`, the HTML pages, manifests and
reports) are UTF-8. The CLI `--json` output preserves exact Unicode values even on
a strict legacy Windows code page (GBK) by emitting ASCII JSON escapes, so the
data survives a console that cannot represent the characters directly.

## Installation and packaging

- Wheel and source distributions build successfully. Inventory checks found
  only the package/metadata in the wheel and package/docs/install scripts/
  synthetic tests in the source distribution, with no backups, personal IDs,
  absolute analysis paths or Trellis scaffolding.
- The wheel was installed in a fresh virtual environment without an editable
  source checkout on its import path. Both module and console entry points
  worked. Offline synthetic exports from a `tar.zst` archive and an independent
  app-data directory passed validation, followed by offline media processing.
- All 16 isolated installation/export checks passed, including Chinese HTML,
  ASCII paths, raw SQLite/JSONL equality, unknown types, preserved original text,
  owned media-index objects and absence of remote media loading.
- The full synthetic suite passed: **108 tests, 0 skips**.

## Vue/Node SQLite reader verification

The optional reader was verified on October 27, 2026 with Node.js 24.15.0.
Its runtime uses Vue 3, Node and SQLite without Python. `npm run build`
succeeded; **66 Node tests passed with 0 failures and 0 skips**.

The independent fixture covers unsafe integer identifiers, exact raw JSON,
query/search/pagination, persistent reversible edits, conflicts and restore,
reindex preserving edits, index-only operation, directory migration, local
media ranges, auth/CSRF, proxy origins, traversal/junction rejection and
bounded shutdown with a stalled media client. Client tests also verify Chinese
error messages and reject stale responses after account/search changes.

A separate real-export probe verified:

| Check | Observed result |
| --- | --- |
| Imported accounts / conversations / messages | 2 / 75 / 65,724 |
| Source JSONL lines compared exactly with `messages.raw_json` | 65,724, all equal |
| SQLite `integrity_check` | `ok` |
| Real archive edits created by verification | 0 |
| Media samples checked over HTTP against file bytes | 9, all equal |
| Unchanged-source restart | Import skipped |
| Original export files before/after reader verification | 5,632, full hashes unchanged |
| Built Vue page delivered by Node | HTTP success; Chinese page language and relative bundles |

The real archive was never edited during testing. Only the new `reader.sqlite`
was created. Verification scripts and detailed real-data reports remain outside
the repository. Browser automation was stopped by the user; no visual browser
verification is claimed. The local service was checked through HTTP.

## Compatibility limits

- Only the verified Android app layout is supported; other platforms and app
  versions are untested. The supplied backup reported Duoshan version 40.1.0;
  this is an observed compatibility point, not a hardcoded runtime requirement.
- Behaviour on macOS/Linux is untested. Development and this verification ran on
  one Windows environment with a full `.venv` install (Pillow, requests,
  zstandard, tzdata, SQLCipher, pillow-heif, and `ffmpeg`).

## Privacy

Only aggregate counts appear on this page. No account identifier, message text,
contact name, media URL or absolute path from a real backup is recorded here or in
the repository's fixtures, and the reproduction commands use your own paths.
