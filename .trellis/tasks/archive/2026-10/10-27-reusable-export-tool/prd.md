# Reusable Duoshan Chat Export Tool

## Goal
Turn the verified Android Duoshan extraction workflow into a reusable local tool in this repository. The user explicitly requested implementation and coordination of the existing four pi agents. Only directory/file names must be English. The offline HTML interface and its explanatory text default to Simplified Chinese; the user explicitly corrected the earlier unwanted translation of the interface. Original user content remains unchanged. Code symbols and machine-readable schemas retain their conventional/original names.

## Requirements
- Accept a user-supplied backup/app-data/database path and output directory; never depend on the original analysis workspace, account IDs, dates or generated export.
- Discover accounts automatically, support selecting accounts, and support plaintext SQLite plus the verified Duoshan SQLCipher layout with WAL preservation.
- Accept extracted Android app data and DataBackup-style tar/tar.zst backup directories, with safe archive extraction into a separate working directory.
- Export standalone per-account SQLite databases, raw JSONL, spreadsheet-safe CSV, contacts, per-conversation TXT and an offline HTML reader.
- Preserve original database rows and unknown message types. Render local voice, stickers, inline emoji and original bubble resources where available.
- Provide optional resumable media download, reporting unavailable or expired resources. Default extraction operates offline.
- Provide inspect, export, media, validate and doctor commands, useful errors, install instructions and automated synthetic tests.
- Restore the already-generated chat reader to Chinese UI while keeping its English paths and all chat/media/database data intact.
- Provide a Vue 3 frontend and Node.js service for SQLite-backed browsing/editing with reversible display edits. The web runtime must not require Python. Keep attachment paths relative so uploading/moving the export preserves media references.
- Keep original backups untouched. In the previous finished export, restore the reader to Chinese while preserving original data, media and English paths. Do not include private data or account-specific fixtures in the repository.

## Acceptance Criteria
1. Installation exposes `duoshan-export` and `python -m duoshan_export`; help and documentation agree.
2. Two independent synthetic inputs with arbitrary account IDs export correctly without stale shared state. SQLite/raw JSONL equality, page/link checks and ASCII generated filenames pass.
3. A regression run against the available real backup discovers two accounts and exports all 65,724 messages into an isolated output directory outside the repository.
4. Optional media commands reuse existing verified files and keep local paths consistent with SQLite attachment indexes and HTML playback/rendering.
5. Archives cannot escape the work directory or overwrite originals; wrong keys, missing dependencies and unsupported schemas produce actionable English errors.
6. Tests and the real regression results are recorded, and supported backup formats/version limitations are stated honestly.
7. Existing and new HTML readers use `lang="zh-CN"` and Chinese menus, navigation, descriptions and playback prompts. Generated paths remain English/ASCII and original chat data is unchanged.
8. The Vue/Node reader imports a central reader.sqlite, queries/paginates/searches it, persists reversible text/note/hide edits with revision conflict checks, and preserves original exports. Local media support includes HTTP ranges and safe portable paths. Isolated store/HTTP tests and the production Vue build pass; real counts match the export.

## Scope Boundaries
The tool reads already-obtained Android Duoshan backups. Device acquisition, logging in, bypassing access controls, iOS data, WeChat extraction and guarantees for every future Duoshan version are outside this task.
