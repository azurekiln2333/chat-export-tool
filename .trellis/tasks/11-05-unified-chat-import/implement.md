# Execution and verification

1. Inspect existing adapters, reader contracts, reference GUI, backup layout and disk capacity; record cleanup inventory.
2. Implement source discovery and WeChat database/key handling, validate representative real account schemas and counts without touching backups.
3. Implement reader-compatible WeChat exports and media resolution with stable identities and diagnostics.
4. Implement incremental merge, old-WeChat alias adoption, source-prefixed directories and import history. Test separate/repeat/newer imports with independent fixtures and preserved edits/deletions.
5. Add GUI, CLI/launchers and user documentation. Verify worker errors/progress and a GUI startup smoke check.
6. Run project Python and Node checks in test directories outside the no-deletion workspace. Preserve all workspace artifacts.
7. Adopt the existing library only after backup and regression checks. Audit final counts, directions, media paths, prefix migration and source independence. Present cleanup candidates with exact paths/reasons/space estimates; require confirmation before recycling.

## Follow-up: neutral title and library media

1. Preserve saved paths through a read-only legacy-settings fallback; display `Chat Archive` and keep online download opt-in off by default.
2. Add a separate library action and worker that needs no original backup selections. Render-check the controls at smaller supported window sizes.
3. Inventory media from live SQLite rows, group recorded alternatives, validate existing assets once per run, and report scanning progress. Do not scrape arbitrary chat links.
4. Fetch only supported app CDN resources with redirect and size checks. Publish verified files under library attachments, retain interrupted artifacts, and avoid full successful-media copies in the work directory.
5. Apply media-only patches to still-existing messages with account/raw-content guards. Preserve raw values, all other columns, user edits and deletion suppression; resume interrupted projections without reimporting messages.
6. Verify synthetic three-app offline-import/download/repeat/cancel cases, invalid or expired URLs, corrupted assets, and photo/video coexistence. Inspect real data read-only; do not start real downloads for QA.
7. Run Python and Node suites, build without clearing prior artifacts, record GUI verification limits, update code contracts and present the concrete conventional-commit plan before committing.
