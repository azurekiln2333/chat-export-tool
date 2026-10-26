# Export format

All generated paths are English and stable. The canonical names live in
`src/duoshan_export/paths.py` (`ROOT_FILES`, `ACCOUNT_FILES`, `DIRECTORIES`), and
old localized names are mapped through `LEGACY_NAMES` so an older export can be
read or refreshed without renaming anything by hand.

## Root of an export

| Path | Contents |
| --- | --- |
| `index.html` | Entry page: accounts, totals and the media summary |
| `reader.css` | Stylesheet used by every page (plus decoration CSS) |
| `media_manifest.json` | Per-resource result, stored path, `bytes`, `sha256`, MIME, dimensions/frames, and the per-message association |
| `decoration_manifest.json` | Bubble IDs with sides/corners and properties, plus the inline-emoji token map |
| `export_summary.json` | Machine-readable totals, account summaries, media/presentation statistics, source and timestamp |
| `validation_results.json` | Data-integrity report |
| `reader_media_validation_results.json` | Reader/media report |
| `media_validation_results.json` | Decode report (only with `--decode-media`) |
| `README.txt` | Export-local notes written during the export (Simplified Chinese content, English file name) |
| `export_state.json` | Run state used by `--resume` (source signature, accounts, timezone, page size) |
| `attachments/` | Shared local media: `voice/`, `stickers/`, `bubbles/<bubble_id>/`, `inline_emoji/`, files copied from the backup |
| `.work/` | Private extraction/snapshot directory (default location; outside the source) |

## One folder per account (`<uid>/`)

| Path | Contents |
| --- | --- |
| `chat.sqlite` | Standalone plaintext database with all original rows and side tables |
| `messages.jsonl` | One complete `msg` row per line plus an `_export` block |
| `messages.csv` | Readable text, time, sender and identifiers (UTF-8 with BOM) |
| `contacts.json` / `contacts.csv` | Contact cache used to complete display names |
| `conversation_metadata.json` | Conversation list, group info, participants, attachment rows |
| `statistics.json` | Counts, message types, date range, per-conversation index |
| `conversation_index.csv` | Conversation index with message counts and time range |
| `conversations/conversation_NNN_page_NNN.html` | Offline reader pages |
| `transcripts/conversation_NNN.txt` | Plain-text transcript per conversation |

### `messages.jsonl`

Each line is the raw `msg` row (columns unchanged, e.g. `msg_uuid`,
`msg_server_id`, `conversation_id`, `type`, `created_time`, `sender`, `content`,
`ext`, `local_info`, ...) plus an `_export` object:

```json
{
  "msg_uuid": "...",
  "content": "{\"text\": \"hello [smile]\"}",
  "_export": {
    "account_uid": "...",
    "conversation_name": "...",
    "time": "2025-09-14 22:42:42",
    "timezone": "Asia/Shanghai",
    "sender_name": "...",
    "type_name": "Text",
    "text": "hello [smile]",
    "local_media": ["attachments/stickers/example.png"],
    "media": {"kind": "emoji", "status": "ok", "path": "..."},
    "bubble": {"id": "...", "side": "1", "source": "a:chat_bubble", "available": true, "applied": true}
  }
}
```

`content` and `ext` stay JSON strings exactly as stored, so nothing is lost if a
message type is not recognised.

### Manifests

`media_manifest.json`:

```json
{
  "resources": [{"resource_id": "...", "kind": "voice", "status": "ok", "path": "attachments/voice/....m4a",
                 "bytes": 1234, "sha256": "...", "mime": "audio/mp4", "codec": "...", "duration": 1.5}],
  "messages": {"<uid>:<msg_uuid>": {"kind": "emoji", "status": "missing_url"}},
  "statistics": {"voice": {"messages_downloaded": 0}, "emoji": {"messages_downloaded": 0}}
}
```

`status` is `ok` for a stored local file; anything else records why the resource
is unavailable (for example `missing_url`, an expired URL, or an HTTP error), and
the reader shows that reason instead of inventing media.

`decoration_manifest.json`:

```json
{
  "bubbles": {"<bubble_id>": {"sides": {"1": {"path": "attachments/bubbles/<id>/self.png",
                                             "width": 96, "height": 96,
                                             "nine_patch": {"padding": [], "slice": [], "scale": 3}},
                              "0": {"path": "attachments/bubbles/<id>/guest.png"}},
              "properties": {}, "corners": {"top_left_self": {"path": "..."}}}},
  "inline_emoji": {"[smile]": {"path": "attachments/inline_emoji/....png"}}
}
```

Sides are `1` (self) and `0` (guest); corners are optional animated overlays.

## Reader markup

The pages are self-contained: no remote assets, a strict Content-Security-Policy,
and local relative links only. Element classes used by the styles and by
`validate`:

- `article.message` with `id="msg-<msg_uuid>"` -- one message
- `div.bubble` / `div.bubble-content` -- the message bubble
- `original-bubble`, `theme-<bubble_id>`, `side-0|1` -- restored bubble styling
- `img.bubble-corner` -- animated corner artwork
- `img.sticker` -- sticker file, inside `a.sticker-link`
- `img.inline-emoji` -- inline emoji replacing a `[token]`
- `audio` -- voice message, with a fallback link to the local file
- `details.links` -- original web references kept from the message content

### Interface language

The reader interface is Simplified Chinese: pages declare `<html lang="zh-CN">`
and every user-visible label, heading, button and hint is Chinese. Everything
that is a code or file contract stays English:

| Kept English | Localized (Simplified Chinese) |
| --- | --- |
| File and directory names | Page headings and section titles |
| CSS classes, element IDs, `data-*` attributes | Button and link labels |
| Manifest keys, report keys, JSON fields | Column headers, placeholders, hints |
| Console and error messages | Media-unavailable and bubble-state notes |

Original chat text, contact names and sender names are copied verbatim and are
never translated, reordered or filtered by the interface language. `validate`
does not depend on the UI language: it checks structure (classes, IDs, links,
media) rather than label text, so a localized page passes exactly like an
English one.

## Guarantees

- Text, identifiers and JSON blobs are never truncated or rewritten; only the
  CSV presentation is neutralised against spreadsheet formulas.
- Every media reference in the pages points inside the export folder.
- The export is reproducible from the same backup: no timestamps leak into
  filenames, and `--resume` refuses anything that is not the same source.