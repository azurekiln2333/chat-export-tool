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


## Media diagnostics, library switching and source labels (2026-11-07)

- Offline report generation joins a finite latest-per-resource journal snapshot
  to current live messages, preserving signed URLs and distinguishing unknown
  historical requests from actual attempts. HTML is escaped and never requests
  remote content. Missing/deleted message references remain historical.
- Python and Node media patch boundaries accept explicit media fields only;
  URL diagnostics and message references cannot enter reader display metadata.
- The desktop has independent media-details workers, status/source/type/search
  filters, pagination and full URL copying. A real service integration proved
  zero HTTP requests, unchanged database hash and successful reporting while
  the library mutation lock was held.
- Library selection and explicit reload use only the GUI-owned process handle,
  preserve its port and wait asynchronously for exit. A real Qt-to-Node test
  switched between independent synthetic libraries, observed new account data
  at the same port and then reloaded the same library. On Windows, Popen
  terminate terminates the owned process; graceful SIGTERM handling is not
  claimed. No live user reader or download was stopped for QA.
- The directory-explanation card is removed. Desktop and web account lists show
  source labels from registered identity, with explicit legacy evidence as a
  fallback and unknown sources retained as unknown. Raw metadata is read by an
  indexed single-account lookup without scanning the complete message table.
- Full Node regression before the final legacy-fallback addition: 162 passed.
  Final source-fallback store/client/import checks: 29 passed. Production Vite
  build passed with --emptyOutDir false. Python/Qt final results are recorded
  below. Cross-layer review found no unresolved blocker.
- Qt screenshots were visually inspected at 1180 x 780 for the revised library
  page and 1120 x 780 for media details. These are offscreen Qt renders, not a
  claimed capture of the user's active desktop. Browser visual QA is not claimed.
- Read-only diagnosis confirmed the reported emoji was extracted correctly and
  had a latest HTTP 403 failure with no local attachment. A successful emoji
  returned matching local file bytes from both running library readers, and
  the actual frontend message model marked it available. This distinguishes
  resource-specific download failure from an all-emoji rendering failure.
- A local full-URL report was generated outside Git for the user's selected
  library. Private URLs, message identifiers and media were not copied into
  repository fixtures. Original backups remained untouched.

External synthetic QA evidence:

- D:/ChatImportWork/gui-media-details-qa-o298jrgy/library-page.png
- D:/ChatImportWork/gui-media-details-qa-o298jrgy/media-details.png
- D:/ChatImportWork/gui-media-details-qa-1fz76mxa/results.json
- D:/ChatImportWork/gui-reader-switch-qa-e5n26t_8/results.json
- D:/AppData/Local/Temp/chat-followup-qt-complete-20261107-054418.log

Final Python run: 199 tests, 187 passed and 12 optional Qt skips in the
backend-only environment. Separate installed-Qt run: all 16 GUI tests passed,
including every skipped control plus settings/source tests. Final logs:
D:/AppData/Local/Temp/chat-followup-python-complete-20261107-054418.log and
D:/AppData/Local/Temp/chat-followup-qt-complete-20261107-054418.log.

Workspace retention: task evidence is retained in place. The standard archive
command removes session files with unlink; it was not used because the parent
AGENTS.md prohibits permanent deletion. Task completion is recorded without
removing runtime or task files.


## Direct full-URL log correction (2026-11-07)

The user clarified that URLs must appear inline in the running task log. The
previous report-only implementation did not satisfy that requirement. Each
completed attempt/resource now emits a status-prefixed full URL, reason and
accurate request/reuse context through the existing progress callback. Aggregate
counters remain throttled. Long URL lines append as plain text without replacing
the compact GUI status. No user download or GUI process was restarted.

Backend focused tests: 34 passed, including signed query preservation, fallback
failure then success, excluded unused alternatives, rejected redirect target,
missing URLs, cached reuse without replay, cancellation, and coordinator-thread
callbacks under six download workers. The test log is outside the repository:
D:/AppData/Local/Temp/chat-media-live-url-tests-20261107-060638.log.

Qt worker-to-log tests verify URL visibility while the mock download is still
running, full long signed URLs, literal HTML text, no automatic URL visits and
stable aggregate status. Controlled media callback URL portions preserve signed
values even if a query contains a credential-like substring; surrounding text,
stdout and arbitrary errors continue credential redaction. Original data and
live libraries are unchanged. No Node or media acquisition behavior was changed.

Final Qt run: 17/17 passed, including signed queries containing synthetic credential values and safe handling of surrounding text/stdout/errors. Legacy Windows redirected console output falls back to ASCII status markers if its encoding cannot print a checkmark; ASCII signed URLs remain unchanged.
