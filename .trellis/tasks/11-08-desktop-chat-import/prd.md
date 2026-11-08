# Selectable desktop chat imports and Windows account discovery

## Goal

Add selectable account sources, Documents discovery, isolated Windows WeChat and QQ parsers and preserve all existing Android adapters.

## Requirements

- Rename import page to 导入聊天记录. Add checkboxes and account column; process only checked sources.
- Query Windows Documents location; discover Tencent Files, xwechat_files and legacy WeChat Files per account without fixed drive/account paths.
- Add independent PC WeChat and QQ parsers with encrypted-format detection, explicit failures, raw retention and stable incremental identities. Preserve three Android adapters.
- D: sources are read-only. All snapshots, decrypted data, parsing, logs, fixtures and exports for this session stay under E:/analysis. No deletion or private data in git.
- User explicitly requested implementation and clarified E-drive processing. Higher-priority developer guidance authorizes reversible implementation without extra planning approvals.
- Follow-up clarification (2026-11-09): legacy QQ Msg3.0 records are required too;
  detection plus an unsupported warning is not the desired end state. Determine
  whether legacy and NT history overlap from actual decrypted records, never
  infer migration merely because both files exist.
- Recover local QQ media even when the QQ client's own media index no longer
  resolves it. Match recorded paths/content identifiers within the correct
  account, copy verified assets to the E-drive library and distinguish original
  images from thumbnails. Newly cached media must be discoverable on updates.

## Acceptance Criteria

- [x] Redirected Documents discovery shows accounts, excludes globals/cache.
- [x] Checked rows only are processed; none selected is an actionable error.
- [x] Supported PC schema fixtures parse with correct sender/group identity and update behavior; unsupported encryption fails explicitly.
- [x] All processing uses verified private snapshots; source bytes unchanged.
- [x] Existing Python/Node tests pass; real-data support limits documented accurately.
- [ ] Legacy QQ decryption/schema recovery is implemented where data and keys
  are available; any external blocker is evidenced and clearly reported.
- [x] Local QQ media references recover from known account cache directories,
  with original/thumbnail identity preserved and later-cache update coverage.

## Notes

- Code checkpoint verified on 2026-11-09: 322 Python tests, 169 Node tests,
  reader build, compileall and dependency doctor pass without skipped tests.
- The actual classic encrypted QQ database is still unresolved: the matching
  classic key and codec component were not available. A decrypted classic body
  parser exists, but that does not establish recovery of the user's old history.
  Keep this requirement open and do not archive the task as fully achieved.
