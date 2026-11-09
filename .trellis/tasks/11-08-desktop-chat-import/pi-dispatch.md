# Four Pi continuation: classic QQ local component integration

Active task: `.trellis/tasks/11-08-desktop-chat-import`.
Repository: `E:/analysis/duoshan-chat-export-tool`, base `54947fd`.

Continuation status: all four Pi panes stopped after provider rate limits and
then a client-rejection response, before producing code edits. Root took over
the implementation and separate read-only review. Ownership below records the
original dispatch; do not resume those assignments without rechecking Git.
The user explicitly requested the four existing Pi agents in the E:/analysis
Herdr workspace to implement the remaining work in parallel. Do not create new
agents/worktrees, commit/push, restart clients, or modify other projects.

## Read first

Read E:/analysis/AGENTS.md, repo AGENTS.md, this task's PRD/design/implement,
and `.trellis/spec/backend/desktop-import.md`. No permanent deletion anywhere
in this workspace, including fixtures; cleanup requires user approval and the
recycle script. Original databases/archives are read-only. All new files,
snapshots, builds, tests, logs and TEMP/TMP belong under E:/analysis. Do not
persist actual keys, memory dumps, account IDs or chats in repo/logs.
Only read/query process access; no injection, hooks, breakpoints, process writes,
login automation or stopping the user's clients. No GUI control this task.

## Verified input

- User supplied E:/analysis/QQ9.7.23.29348.exe, signed by Tencent with valid
  Authenticode status and file/product version 9.7.23.29348. This is NOT 29368.
- The installer was never executed. PE resources 101/102 were extracted into
  E:/analysis/desktop-import-validation/qq-9.7.23.29348-2325bbb1.
- KernelUtil.dll is at components/Files/Bin/KernelUtil.dll beneath that folder;
  x86 machine 0x14c, version 9.7.23.29348. Upstream key/open/exec/rekey byte
  signatures each have one static occurrence. That is preliminary evidence,
  not proof of correct calling conventions or actual decrypt success.
- Upstream research files (read only as evidence, do not vendor):
  E:/analysis/desktop-import-validation/research/qq-win-db-key-scripts_windows_pcqq_pcqq_rekey_to_none.cpp
  and adjacent pcqq_get_key.py, pcqq_dump.py, LICENSE. Verify upstream function
  identities independently; do not blindly execute signature-located functions.
- Current WeChat4 and QQNT AES/HMAC/WAL implementation is already in tool source.
  Auto-key recovery reads running logged-in Weixin.exe/QQ.exe; supplied matching
  keys allow offline copied-DB decryption. No client decrypt DLL required for NT.
- Real NT normalized SQLite is retained at
  E:/analysis/desktop-import-validation/real-qqnt-20261109-005225/plain/nt_msg.standard-011926.db.
  Do not duplicate the multi-GB snapshot. Actual classic key is still unavailable.
- Current baseline 322 Python tests and 169 Node tests pass, no skips.

## Ownership (exclusive writes)

1. w3F:p7 / native helper: new native/legacy_qq/ source/build instructions and
   tests/test_qq_legacy_helper.py; runtime artifacts only under E QA/.work.
   Inspect DLL dependencies and function signatures, implement isolated x86 helper
   probe/decrypt on copied DBs. Use authenticated known fixtures if possible;
   never confuse loading the DLL with decrypting the user's data.
2. w3F:p8 / keys: new apps/qq_legacy_keys.py and tests/test_qq_legacy_keys.py,
   research/legacy-qq-keys.md. Investigate verifiable read-only key acquisition
   from existing environment. If no supported path exists, report exact missing
   condition; don't manufacture a key or claim raw NT keys can unlock classic.
3. w3F:p9 / integration: new apps/desktop_legacy.py, and edits to
   apps/desktop_crypto.py, apps/desktop.py, cli.py, gui.py, desktop_sources.py,
   pyproject.toml only as necessary. Local bundle discovery/config and error
   handling; integrate helper only once its contract is stable. Preserve Android
   adapters/NT profiles, per-account key scoping and mixed partial retry semantics.
4. w3F:pA / QA/docs: new tests/test_desktop_legacy.py, existing desktop/GUI tests
   as needed, docs/desktop-import.md, docs/gui.md, and desktop-import spec.
   Audit supplied package, native/runtime boundaries and regression. Do not run
   full suite while other agents are still editing. Do not overclaim coverage.

Root owns shared task metadata, final integration review, final test run and Git.
Send interface changes/blockers to root pane w3F:p6 through Herdr, or directly
coordinate with assigned neighboring Pi panes. Do not change other's owned files.
Respond to original task with file changes, checks, remaining blockers.

## Proposed cross-agent contracts (coordinate before changing)

Helper: `probe` emits safe JSON capabilities; `decrypt` receives JSON over stdin
with source/output paths and hex classic key (never in process command line),
emits progress/result JSON without secret material. x86 isolated process, load
only explicit component directory, write only private output/work files.
Python bridge: decrypt_legacy_snapshot(source_snapshot, output, *, keys=None,
account=None, relative_path=None, progress=print, cancel=None), raises ExportError
on missing/incorrect key/unsupported DLL. Optional local component directory
through CHAT_ARCHIVE_LEGACY_QQ_DIR; default project .work/legacy-qq. No proprietary
DLL or private installer is committed/pushed; bundle stays local to this tool.
The key agent should propose a small pure verification/recovery API to the
integration agent before integration. Actual keys stay in memory only.

## Retained testing

E:/analysis/desktop-import-validation/run_tests_retained.py supports dotted module
args such as tests.test_desktop. It redirects cleanup to retained renames.
Use TEMP/TMP under E:/analysis/desktop-import-validation. Global Qt Python is
D:/ProgramFiles/Python/Python313/python.exe; append local .venv/Lib/site-packages
via site.addsitedir when needed. Repo .venv Python has backend/crypto but no Qt.
Never use unwrapped automatic cleanup tests in this workspace.
