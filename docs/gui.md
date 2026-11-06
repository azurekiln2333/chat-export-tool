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

1. Add a backup directory, several archives/databases, or drag paths into the
   window. A parent directory can contain several supported app backups.
2. Choose a shared library directory and a separate working directory. Keep
   both outside original backup paths. The work volume needs room for private
   database snapshots and export staging.
3. Use **识别来源** for source discovery or **检查数据库** to inspect accounts
   and decryption. Detailed results and errors appear under **任务记录**.
4. Use **导入 / 更新资料库** to run the real import pipeline. Continue choosing
   that same library for future backups or another app. New data is merged by
   the library service; the GUI does not rewrite messages itself.
5. Open **聊天资料库** to view account totals and start the local reader.

Online downloads are off by default. The opt-in checkbox enables media
downloads through the existing adapter's supported network rules.

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
per-user settings. It does not store database keys. Logs can be explicitly saved
through **保存本次日志**. This interface does not delete or clean up backups.
Settings now use `%LOCALAPPDATA%/ChatArchive/settings.json`. When that file is
unavailable, the previous `AzureKilnChatArchive/settings.json` is read so saved
library/work paths remain available. The next settings save writes to the new
location and leaves the old file intact. Network opt-in is never persisted.
On first launch, an existing `ChatExportAll` in the current directory or its
parent is selected when available; otherwise the default is
`CURRENT_DIRECTORY/ChatExportAll`. Work defaults to
`CURRENT_DIRECTORY/ChatImportWork` and is independently configurable.

## Reader behavior

The reader starts on an available loopback port with a unique startup log in
`LIBRARY/import_logs`. The GUI opens the browser after the reader responds. It
does not stop existing reader instances or use `--reindex`. Closing the GUI
leaves the reader running so an open browser remains usable.

The physical account folders identify the source: `wx_` for WeChat, `xhs_` for
Xiaohongshu and `dy_` for Duoshan. Account IDs used by messages and annotations
are kept independent from these folder labels. Media lives under
`attachments/`; preserve `reader.sqlite` when moving the whole library.
