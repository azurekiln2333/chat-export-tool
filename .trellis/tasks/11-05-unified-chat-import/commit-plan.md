# Approved work commits

Status: approved by the user on 2026-11-07, including push to origin/master.
The existing history uses `type(scope): description`, for example `feat(web): ...`.
The files below are known changes from the ongoing unified-import task and its
GUI/media follow-up. No unrelated dirty files are currently identified.

## 1. feat(library): support incremental three-app imports and media backfill

- `.trellis/spec/backend/index.md`
- `.trellis/spec/backend/unified-import.md`
- `.trellis/spec/backend/web-reader.md`
- `docs/testing.md`
- `docs/unified-import.md`
- `src/duoshan_export/apps/wechat.py`
- `src/duoshan_export/cli.py`
- `src/duoshan_export/database.py`
- `src/duoshan_export/library.py`
- `src/duoshan_export/library_media.py`
- `src/duoshan_export/source.py`
- `tests/test_database_retention.py`
- `tests/test_library.py`
- `tests/test_library_media.py`
- `tests/test_source_retention.py`
- `tests/test_wechat.py`
- `web/package.json`
- `web/package-lock.json`
- `web/server/account-layout.mjs`
- `web/server/decode-voice.mjs`
- `web/server/library-import.mjs`
- `web/server/library-media.mjs`
- `web/server/store.mjs`
- `web/tests/library-import.test.mjs`
- `web/tests/library-media.test.mjs`

## 2. feat(gui): add Chat Archive importer and post-import media downloads

- `docs/gui.md`
- `docs/usage.md`
- `launch-gui.ps1`
- `pyproject.toml`
- `src/duoshan_export/gui.py`
- `start-gui.pyw`
- `tests/test_gui.py`
- `.trellis/tasks/11-05-unified-chat-import/check.jsonl`
- `.trellis/tasks/11-05-unified-chat-import/design.md`
- `.trellis/tasks/11-05-unified-chat-import/implement.jsonl`
- `.trellis/tasks/11-05-unified-chat-import/implement.md`
- `.trellis/tasks/11-05-unified-chat-import/prd.md`
- `.trellis/tasks/11-05-unified-chat-import/task.json`
- `.trellis/tasks/11-05-unified-chat-import/verification.md`
- `.trellis/tasks/11-05-unified-chat-import/commit-plan.md`

## Exclusions and execution boundary

Exclude all original backups, reader libraries, media, local settings, download
logs and external QA artifacts. Do not stage unrelated changes that appear after
this inventory; report them first. Build outputs remain under existing ignore rules.

Create these approved work commits in order using exact path lists. Do not
amend. The user explicitly authorized pushing to the configured origin/master. Task archive and session bookkeeping, if performed,
belong after the work commits and must not be interleaved with them.

## Follow-up: feat(gui): add library switching and detailed media results

The existing user authorization to commit and push continues for this follow-up.
The commit includes full local media URL diagnostics, offline reports, GUI
filters/copying, library selection/reload, account source labels, clear media
availability labels, and their regression tests/specification updates.

Paths reviewed for this commit:

- `.trellis/spec/backend/unified-import.md`
- `.trellis/spec/backend/web-reader.md`
- `.trellis/spec/frontend/quality-guidelines.md`
- `.trellis/tasks/11-05-unified-chat-import/commit-plan.md`
- `.trellis/tasks/11-05-unified-chat-import/design.md`
- `.trellis/tasks/11-05-unified-chat-import/implement.md`
- `.trellis/tasks/11-05-unified-chat-import/prd.md`
- `.trellis/tasks/11-05-unified-chat-import/task.json`
- `.trellis/tasks/11-05-unified-chat-import/verification.md`
- `docs/gui.md`
- `docs/testing.md`
- `docs/unified-import.md`
- `src/duoshan_export/cli.py`
- `src/duoshan_export/gui.py`
- `src/duoshan_export/library_media.py`
- `tests/test_gui.py`
- `tests/test_library_media.py`
- `web/server/account-layout.mjs`
- `web/server/library-media.mjs`
- `web/server/store.mjs`
- `web/src/components/App.vue`
- `web/src/media.js`
- `web/src/utils.js`
- `web/tests/client.test.mjs`
- `web/tests/library-import.test.mjs`
- `web/tests/library-media.test.mjs`
- `web/tests/store.test.mjs`

All private diagnostic reports, original data, live libraries, download results,
settings and QA artifacts remain outside Git. No cleanup is part of this change.

## Direct task-log correction: fix(media): show full URLs in live download logs

The user clarified that every download outcome needs its full URL directly in
the task log. Include `library_media.py`, `gui.py`, their focused tests, the
unified-import specification, GUI/import/testing docs and this task's updated
artifacts. Existing approval covers commit and push. Original backups, live
libraries, runtime logs and private URLs remain excluded. The running GUI is
not restarted for this logging-only correction.
