# Desktop importer

Chat Archive uses a Chinese PyQt6 and Fluent interface, with pages for
backup import, the current library, task logs, and settings. The existing
command-line exporter remains available independently of the desktop packages.

## Launch

From a source checkout on Windows:

```powershell
python -m pip install ".[full,gui]"
python -m duoshan_export.gui
```

Or run `launch-gui.ps1` / `start-gui.pyw`. The PowerShell launcher first tries
the project's Python environment, then the Python on PATH. The source launcher
also makes backend dependencies in the local `.venv` available. It does not
install packages or alter the reference photo-tool project.

The launcher supports Windows PowerShell 5.1 and PowerShell 7. Dependency
probes run in hidden processes with a 10-second timeout; a missing Qt package
or failed probe advances to the next Python candidate.
The `.pyw` entry point is a normal Python script that lets Windows launch the
visible GUI without an extra console window. Only dependency probes are hidden;
the final application starts with normal window visibility. For console diagnostics, run
`python start-gui.pyw` from the project directory.

After package installation, `chat-archive` launches the same interface. The
reader also requires the repository's `web/server` and built `web/dist`
directories and the Node.js version documented in `web/package.json`.

## Workflow

1. Open **导入聊天记录**. Local PC WeChat/QQ accounts appear automatically from
   the Windows Documents known folder, including redirected locations. Each row
   has a checkbox and **账号** column. Automatically found accounts start unchecked;
   manually added backup directories, archives or databases start checked.
   A parent directory can contain several supported app backups. Use **刷新本机账号**
   to refresh discovery; checks/imports use only the selected concrete rows.
2. Choose a shared library directory and a separate working directory. Keep
   both outside original backup paths. The work volume needs room for private
   database snapshots and export staging.
3. Use **识别来源** for source discovery or **检查数据库** to inspect accounts
   and decryption. Detailed results and errors appear under **任务记录**.
4. Use **导入 / 更新资料库** to run the real import pipeline. Continue choosing
   that same library for future backups or another app. New data is merged by
   the library service; the GUI does not rewrite messages itself.
5. Open **聊天资料库** to view account totals and source labels (**多闪/抖音**,
   **微信**, **小红书**, **PC 微信**, **PC QQ**) and start the local reader. **切换资料库** selects another
   existing library and immediately refreshes this overview.

Online downloads are off by default. The opt-in checkbox enables media
downloads through the existing adapter's supported network rules.

PC imports copy and verify database/WAL snapshots into the chosen work directory
before parsing. Originals are read-only. See [PC versions and key requirements](desktop-import.md)
for the current limits: WeChat 4 and QQ NT support verified read-only key recovery
from logged-in clients. Decrypted classic QQ `Msg3.0` records are supported;
classic proprietary encryption still needs a readable decrypted copy. QQ local
media is matched independently of the client's index; PC WeChat media recovery
is not implemented. Mixed QQ accounts report partial imports and remain retryable.
The separate PC key controls accept a key or
existing JSON file for this task only; they are never saved in preferences.

Import and task pages show the **current stage**, actual completed/total counts
and a percentage whenever the total is known. This percentage belongs to that
stage, not the whole import. Source verification and reader-file preparation
use byte counts; message export, referenced-media extraction and conversion use
record/file counts. Uncounted archive traversal remains indeterminate and shows
the latest action, scanned members and elapsed time. Long WeChat disk/codec
stages report at least every five seconds while waiting; stage boundaries and
completion are logged immediately. Reader merge progress is streamed live.
Failure/cancellation keeps the last actual count instead of claiming success.

After an offline import, open **聊天资料库**, check **允许下载记录中的在线媒体**,
then click **补下载在线媒体**. This independent operation covers all accounts in
the current library and needs no original backup selection. Checking the box
alone does not access the network. The task page shows progress, a stop button,
and successful, already-present, failed and unsupported resource counts.
Completed downloads remain available when cancelled and valid local assets
are reused on the next run. The service only retrieves supported URLs retained
in current library messages; expired or unsupported resources are reported.
Refresh an already-open reader page to request the updated media references.
No reader restart or reindex is required.

The task log directly shows full URLs after each media outcome: `[✓] URL` for
validated downloads, `[x] URL` with the failure reason, `[!] URL` for unsupported
resources, and `[=] URL` for a reused local file. Missing addresses are shown as
`[!] 无 URL` with the reason. Signed query strings are preserved. Recorded
candidates, redirects and actual attempts remain distinguished; a cached file
does not imply a new request. Aggregate counters continue alongside these lines.
The GUI retains the latest 5,000 log lines; complete resource diagnostics remain
available in the library journal and details reports. Code changes load when
the GUI is reopened after the current task finishes.

**查看媒体下载详情** is available on both the library and task pages. It reads
the selected library and download journal offline, with no backup selection,
working-directory setting or network opt-in required. It defaults to failed and
unsupported resources; filters also cover all statuses, app, media type, reason,
account, message ID and URL. Large inventories are shown in pages of 200.
The details show the recorded URL, actual attempted URLs when known, Chinese
reasons and the related accounts/messages. Full signed URLs remain selectable;
**复制此资源全部 URL** copies the complete recorded and attempted addresses.
Missing addresses are identified explicitly. Old journals without attempt
history are marked unknown, rather than implying that no request was made.

This view uses an independent background reader and remains available during
an active download. Its timestamped snapshot can be refreshed; closing the
detail window cancels only that read and leaves the download running. Opening,
filtering, refreshing or copying details never visits a remote URL. The service
also saves uniquely named local HTML, text and JSON reports in
`LIBRARY/media_reports`, including the full URLs. Their paths appear in the
dialog. Detail errors do not replace the active task's progress or results.

WeChat key material is normally discovered from the backup. If automatic
decryption fails, the settings page accepts an optional database password or
UIN/IMEI pair. These masked fields apply to checks and imports in the current
window only. They are not saved in preferences; matching values are redacted
from worker logs, errors, and displayed results.

Cancellation is cooperative: **完成当前步骤后停止** asks the pipeline to stop at
its next safe boundary. Closing while a worker is running requests the same
stop and keeps the window alive until the current step finishes. It never
force-kills a database transaction.

The GUI stores only output/work paths, timezone and the Node executable in its
portable settings. It does not store database keys. Logs can be explicitly saved
through **保存本次日志**. This interface does not delete or clean up backups.
Settings use `PROJECT/.work/gui/settings.json`, or `CHAT_ARCHIVE_RUNTIME/settings.json`
when that environment override is set. The source launcher also places temporary
files in that runtime directory. Old `%LOCALAPPDATA%/ChatArchive/settings.json`
and `AzureKilnChatArchive/settings.json` remain read-only fallbacks, preserving
saved library/work paths without writing beside the old preferences. Network
opt-in is never persisted.
On first launch, an existing `ChatExportAll` in the current directory or its
parent is selected when available; otherwise the default is
`CURRENT_DIRECTORY/ChatExportAll`. Work defaults to
`CURRENT_DIRECTORY/ChatImportWork` and is independently configurable.

## Reader behavior

The reader starts on an available loopback port outside **64967–65066**
(inclusive), with a unique startup log in
`LIBRARY/import_logs`. The GUI opens the browser after the reader responds. It
does not use `--reindex`. When this GUI already manages a reader, selecting a
different library stops that exact child process, restarts the reader on the
same port with the selected directory and opens the page when ready.
**重载阅读器** performs the same operation for the current library, useful after
updating the reader build. Only the process handle created by this GUI can be
stopped; readers started elsewhere or by earlier GUI instances are left alone.
Without a managed reader, selecting a library changes the overview;
**打开聊天阅读器** or **重载阅读器** starts it. A failed or timed-out switch is
reported without forcibly killing other processes. Closing the GUI stops its
owned reader, requests cancellation of running GUI tasks, and waits for their
safe exit before closing. Only the saved reader process handle is stopped;
unrelated services are never enumerated or terminated. A reader that does not
exit is stopped forcibly after a bounded wait; a shutdown failure keeps the
window open and reports the error. Browser tabs may remain open after the
reader service has exited.

Account source labels use `library_accounts.app` when recorded. Older exports
can fall back to explicit folder prefixes, account statistics, saved message
export metadata or an unambiguous single-app export summary. Unknown sources
are labelled explicitly rather than guessed from account ID length.

The physical account folders identify the source: `wx_` for WeChat, `xhs_` for
Xiaohongshu and `dy_` for Duoshan. Account IDs used by messages and annotations
are kept independent from these folder labels. Media lives under
`attachments/`; preserve `reader.sqlite` when moving the whole library.
