# Verification record

## Follow-up scope

- Neutral `Chat Archive` title, legacy-settings fallback and explicit library
  media download action without backup selection.
- Live SQLite media inventory, bounded app-specific download handling,
  verified local publication, durable resume and metadata-only reader patches.
- Fix the reported staging failure boundary: sequential incoming-directory
  preparation, serialized publication, retained path diagnostics and isolation
  of individual worker failures.

## Observed checks

- Python compile check passed for `src` and `tests`.
- Full Python suite: 181 tests, 177 passed and 4 optional Qt controls skipped
  in the backend environment. The separate installed-Qt run passed all 6 GUI
  tests, covering those controls. Media-specific tests passed 24 of 24,
  including six-worker publication, isolated staging failure, retry, SILK
  playback and image-handle closure before rename.
- Runtime doctor reported core, encrypted-database and audio-conversion
  dependencies ready.
- Qt desktop regression tests: 6 passed in the installed GUI environment.
- Full Node suite: 159 passed, zero failures or skips.
- Production Vite build passed using `--emptyOutDir false`.
- Real Qt worker -> Python media service -> Node patch integration passed on
  an isolated synthetic library with intercepted HTTP. First run downloaded a
  resource; repeat reused it with zero requests. Raw JSON and annotations were
  preserved.
- A running reader HTTP integration test observed a new local media reference
  on the next request without restart or reindex, served identical bytes, and
  preserved text edits, notes and hidden state. Browser UI still needs a fresh
  request/reload; no automatic push was added.
- Qt-rendered screenshots were visually inspected, including 1180 x 780 layout
  with the library download controls visible. Windows UI inventory verified
  the real `Chat Archive` title. Native window capture timed out, so actual
  desktop screenshot verification is not claimed.

## Reported live failure and boundaries

The user independently started a download during integration. A staging-path
containment check aborted that run. Read-only inspection afterward found the
reported path inside its library with no link/junction escape. A separate
100-iteration six-thread directory race probe did not reproduce the error.
The exact original cause therefore remains unconfirmed; the failure boundary
was hardened and covered by concurrent-worker and injected-failure tests.

No automatic live-library retry or cleanup was performed for verification.
Original backups were not modified. Successful files and failed partials are
retained; restarting the desktop process is required to load code changed after
its worker first imported the service module.

## Git state

The user approved the conventional-commit grouping in `commit-plan.md` and
explicitly requested pushing to origin/master on 2026-11-07. Original data,
libraries, local settings and external QA artifacts are excluded. Commit
identifiers and remote publication are verified through Git history.
