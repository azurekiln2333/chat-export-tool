# Design and change boundary

The existing Duoshan/XHS adapters remain the baseline. A new library orchestration layer runs them in isolated per-source staging directories, then merges their reader-format messages. A separate WeChat adapter discovers backup snapshots, locates key material, decrypts private copies, preserves raw message values, assigns stable source identities and resolves local media.

The reader store gains additive support for prefixed account folders and a transactional import API. Existing account UID/message UUID values remain stable. Importing one source must never clear unrelated accounts or absent history. Existing annotations and deletion ledger remain authoritative. WeChat legacy IDs need a one-time alias map from the old merged archive before any fresh raw-backup import; retain that map inside the portable library so future imports do not depend on the old archive.

The merge stores import history and aliases, suppresses already deleted messages/conversations, unions media references, and regenerates affected JSONL projections without discarding old records. Original JSONL text is preserved except deliberate adapter metadata. Export/source fingerprint consistency must survive reader restart.

GUI uses the installed PyQt6/Fluent ecosystem, has a worker thread for scans/imports, and routes all actual work through CLI/library functions. All disk paths are configurable; media and library indexes remain portable. Credentials must not appear in logs or task files.

Cleanup audit is separate and read-only. Count logical bytes and unique filesystem identities; hardlinks can make recursive folder totals misleading. Do not automatically prune caches, tests, or backups under the analysis workspace.

Expected changes: new WeChat adapter, source detection/library orchestration, GUI and launch scripts; additive CLI commands/options; reader folder discovery and incremental merge; focused Python/Node tests, documentation and specs. No changes to existing Duoshan/XHS message rendering or bubble-side rules. The reference photo-tool repository remains read-only.

## Follow-up boundary

The missing behavior is post-import media retrieval from the authoritative library, exposed on the desktop library page. Add a Python library-media service plus a focused Node metadata patch transaction. It must read live `reader.sqlite` rows, never recover deleted rows from old JSONL, reuse existing URL extraction/codec helpers where appropriate, retain a resumable download manifest, and patch only media metadata for messages still present. Projections are refreshed through the existing managed-library publisher; old data and annotations stay intact. URL fetching uses the established application CDN boundaries, validates redirects and byte limits, and records unsupported/encrypted/expired resources explicitly. Network activity requires the new GUI opt-in or an explicit CLI media-download flag.

Files: GUI title/library action and docs; new `library_media.py`, additive CLI routing; new Node library-media patch entry and tests. Existing single-app download/export paths remain compatible. Tests use isolated fixtures outside E:\analysis and stub network responses; actual library inspection is read-only until the user invokes downloading.

## Detailed media diagnostics

Store structured per-attempt diagnostics in local media journal/results, keeping URL diagnostics separate from reader media metadata. Provide an offline report service to combine the latest journal status per resource with current live inventory, including old records lacking URL details. GUI surfaces a filterable details list and selectable full URLs, with progress logs explaining failure reasons. Generate uniquely named reports without overwriting or deleting previous artifacts. Never auto-open URLs.

## Library selection and source display

Library selection refreshes the desktop summary and reloads only a reader process owned by the current GUI. Preserve its port so open reader pages can reload against the new library. Stop/restart asynchronously with a bounded wait; never discover and terminate unrelated Node processes. When no reader is managed, selection refreshes the library and the existing open action starts it. Provide an explicit reload for the same library. Ongoing import/download work keeps destination controls disabled.

Account source labels come from `library_accounts.app`; legacy exports may use trustworthy account metadata or source-prefixed directories, with unknown sources remaining explicit. Add a desktop source column and reader account labels. Remove the directory-explanation card; keep storage documentation in the docs. Missing media labels describe recorded state without inventing server causes.

## Direct download log URLs

Emit explicit per-resource/attempt outcome lines through the existing progress callback after completion, alongside throttled aggregate counters. Distinguish recorded candidates from actual requests, redirects, cached files and missing URLs; do not mislabel unattempted fallback candidates. Keep full URLs as local plain text, never reader metadata. Bound GUI document size using the existing retained-journal/report model. No real media requests or process restarts for QA.
