# Unified chat library and desktop importer

## User requirements

- Recognize Android backups for WeChat (`com.tencent.mm`), Duoshan (`my.maya.android`), and Xiaohongshu (`com.xingin.xhs`), including separate Android users and DataBackup archives.
- Decrypt supported database formats using backup-contained key material; preserve originals and report unsupported/corrupt data explicitly.
- Import one app at a time or several backups into one portable `ChatExportAll` reader library. Repeating a backup is idempotent; later/overlapping backups append new messages without removing history, annotations, hidden flags, or deletion suppression.
- Provide a PyQt6/Fluent desktop GUI inspired by the user's existing photo-tool project, with selection, progress, logs, settings, import history, and reader launch.
- Use `wx_<uid>`, `xhs_<uid>`, and `dy_<uid>` account directories while preserving internal account/message identities and compatibility with legacy directory layouts.
- Adopt the current library without breaking Duoshan/XHS. Preserve the completed WeChat direction repair. Account for previously excluded WeChat accounts.
- Audit intermediate/export/backup folders, including hardlinks and current dependencies. Do not delete anything; a concrete recycling list requires user confirmation. Never modify original backup directories or archives.

## Acceptance

Synthetic and real read-only checks cover detection, decryption, outgoing/incoming messages, accounts, media, sequential independent imports, repeat imports, later overlapping snapshots, stable identities, preserved edits/deletions, prefixed-directory reopen, interrupted imports, and portable relative media. Test unknown formats/wrong keys without modifying inputs. GUI launches and invokes actual backend work asynchronously. Report remaining limits honestly.

## Follow-up: library media and neutral application title

- Display `Chat Archive` without personal branding. Existing user settings must remain discoverable.
- The library page offers an explicit opt-in to download media after an offline import, without requiring the original backup paths or re-importing chats.
- Read authoritative live library messages; download only recorded, supported media URLs. Store verified local files beneath that library's `attachments`, update reader metadata, and keep existing messages, raw values, edits and deletion suppression unchanged.
- Repeating or resuming the operation reuses valid local assets. Show successful, existing, failed and unsupported resources; cancellation retains completed work. Offline behavior remains the default.
- Report actual Git state and provide consistent commit wording. Do not silently include unrelated files or private backup data in commits.

## Follow-up: explain media outcomes and show URLs

- Explain failed versus unsupported media with readable Chinese reasons.
- Show recorded URLs, attempted URLs when known, and message/account references in a local detailed report and GUI. Preserve full signed URLs as requested by the user.
- A missing recorded URL must display that fact, never invent an address. Historical journals can be matched against current live messages without downloading again.
- Keep the active download running; diagnostic reads do not alter message data. Private URLs/reports stay outside the source repository.

## Follow-up: library selection and source labels

- Select a different library directly on the library page, refresh its account list and reload the GUI-managed reader for that library. Also provide an explicit reader reload action.
- Remove the entire directory-explanation card from the GUI.
- Display account sources as `多闪/抖音`, `微信` or `小红书` in both the desktop library list and reader account list, using stored source metadata.
- Diagnose the reported unavailable emoji using live data and download results. Distinguish recognition, local availability and failed downloads; do not claim a generic failure proves URL expiry.

## Follow-up: per-resource URLs directly in task logs

The user requires visible status-prefixed full URLs in the running download log, rather than only aggregate counts or a separate details dialog. Show successful, reused, failed and unsupported outcomes directly; retain full signed query strings and reasons. Missing URLs remain explicit. Preserve the current running downloader and existing retrieval behavior.
