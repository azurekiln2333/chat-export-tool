"""Chinese desktop entry point for the portable, incremental chat library.

Qt is optional and imported only when creating the desktop application. All
backup work belongs to ``library``; widgets never read or write message rows.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request

APP_TITLE = "Chat Archive"
APP_NAMES = {"wechat": "微信", "xhs": "小红书", "duoshan": "多闪"}
GUI_HINT = "桌面界面需要 PyQt6 与 PyQt6-Fluent-Widgets。请在工具环境运行：pip install \".[gui]\""


def settings_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config")
    return base / "ChatArchive" / "settings.json"


def read_settings() -> dict:
    current = settings_path()
    legacy = current.parent.parent / "AzureKilnChatArchive" / "settings.json"
    for path in (current, legacy):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, ValueError):
            continue
    return {}


def default_library() -> Path:
    """Prefer a nearby existing library when launched from a source checkout."""
    candidates = (Path.cwd() / "ChatExportAll", Path.cwd().parent / "ChatExportAll")
    return next((path for path in candidates if (path / "reader.sqlite").is_file()), candidates[0])


def library_summary(output: Path) -> dict:
    """Small read-only overview; deliberately does not scan millions of rows."""
    database = output / "reader.sqlite"
    if not database.is_file():
        return {"accounts": [], "messages": 0, "history": [], "database": str(database)}
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        rows = connection.execute("""SELECT a.uid,a.name,COALESCE(SUM(c.message_count),0)
            FROM accounts a LEFT JOIN conversations c ON c.uid=a.uid
            GROUP BY a.uid,a.name ORDER BY a.uid""").fetchall()
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        history = []
        if "import_batches" in tables:
            for app, source, imported, result_json, projection in connection.execute(
                    "SELECT app,source_id,imported_at,result_json,projection_status FROM import_batches ORDER BY imported_at DESC LIMIT 100"):
                try:
                    details = json.loads(result_json)
                    if not isinstance(details, dict):
                        details = {}
                except (TypeError, ValueError):
                    details = {}
                history.append({"app": app, "source": source, "time": imported,
                    "inserted": details.get("inserted", 0), "updated": details.get("updated", 0),
                    "status": "已完成" if projection == "completed" else "待完成导出"})
        return {"accounts": [{"uid": r[0], "name": r[1], "messages": r[2]} for r in rows],
                "messages": sum(r[2] for r in rows), "history": history, "database": str(database)}
    finally:
        connection.close()


class SignalWriter(io.TextIOBase):
    def __init__(self, emit):
        self.emit = emit
        self.pending = ""

    @property
    def encoding(self):
        return "utf-8"

    def write(self, text):
        text = str(text)
        self.pending += text.replace("\r", "\n")
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.strip():
                self.emit(line)
        return len(text)

    def flush(self):
        if self.pending.strip():
            self.emit(self.pending)
        self.pending = ""


def create_window(*, sources=(), output=None, work=None):
    try:
        from PyQt6.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal
        from PyQt6.QtGui import QDesktopServices, QFont, QColor
        from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog,
            QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QScrollArea,
            QTableWidgetItem, QVBoxLayout, QWidget)
        from qfluentwidgets import (BodyLabel, CardWidget, CheckBox, FluentIcon,
            FluentWindow, LineEdit, NavigationItemPosition, PlainTextEdit,
            PrimaryPushButton, ProgressBar, PushButton, SubtitleLabel,
            TableWidget, TitleLabel, setTheme, setThemeColor, Theme)
    except ImportError as error:
        raise RuntimeError(GUI_HINT) from error

    def icon(*names):
        for name in names:
            if hasattr(FluentIcon, name):
                return getattr(FluentIcon, name)
        return FluentIcon.FOLDER

    class Worker(QThread):
        log = pyqtSignal(str)
        succeeded = pyqtSignal(object)
        failed = pyqtSignal(str)

        def __init__(self, operation, paths, options, parent=None):
            super().__init__(parent)
            self.operation = operation
            self.paths = tuple(Path(p) for p in paths)
            self.options = dict(options)
            self.cancel_event = threading.Event()

        def _redact(self, value):
            if isinstance(value, str):
                for key in ("password", "imei", "uin"):
                    secret = self.options.get(key)
                    if secret:
                        value = value.replace(str(secret), "[已隐藏]")
                return value
            if isinstance(value, list):
                return [self._redact(item) for item in value]
            if isinstance(value, dict):
                return {key: self._redact(item) for key, item in value.items()}
            return value

        def _emit_log(self, value):
            self.log.emit(self._redact(str(value)))

        def run(self):
            writer = SignalWriter(self._emit_log)
            try:
                from . import library
                credentials = {key: self.options.get(key) for key in ("password", "imei", "uin")}
                with redirect_stdout(writer), redirect_stderr(writer):
                    if self.operation == "discover":
                        result = {"status": "completed", "sources": library.discover_sources(self.paths)}
                    elif self.operation == "inspect":
                        result = library.inspect_sources(self.paths, self.options["work"],
                            progress=self._emit_log, cancel=self.cancel_event.is_set, **credentials)
                    elif self.operation == "download":
                        from .library_media import download_library_media
                        result = download_library_media(self.options["output"], self.options["work"],
                            progress=self._emit_log, cancel=self.cancel_event.is_set,
                            node=self.options["node"], workers=6,
                            download_media=self.options["library_download_media"])
                    else:
                        result = library.import_sources(self.paths, self.options["output"], self.options["work"],
                            timezone=self.options["timezone"], download_media=self.options["download_media"],
                            progress=self._emit_log, cancel=self.cancel_event.is_set,
                            node=self.options["node"], **credentials)
                writer.flush()
                self.succeeded.emit(self._redact(result))
            except Exception as error:
                writer.flush()
                if self.cancel_event.is_set():
                    self.succeeded.emit({"status": "cancelled", "message": self._redact(str(error))})
                else:
                    self.failed.emit(self._redact(str(error)))

    class MainWindow(FluentWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(APP_TITLE)
            self.resize(1260, 880)
            self.setMinimumSize(1000, 720)
            self.setAcceptDrops(True)
            self.saved = read_settings()
            self.sources = []
            self.worker = None
            self.last_result = None
            self.media_result_output = None
            self.reader_process = None
            self.reader_url = ""
            self.reader_output = None
            self.reader_log = None
            self.reader_started = 0.0
            self.reader_ready = False
            self.reader_timer = QTimer(self)
            self.reader_timer.setInterval(500)
            self.reader_timer.timeout.connect(self._check_reader)
            self._busy_controls = []
            self.import_page = self._page("import", "导入聊天备份", "微信、多闪、小红书，汇总到一个可持续更新的聊天资料库。")
            self.library_page = self._page("library", "我的聊天资料库", "查看合并结果，打开阅读器，继续追加新的备份。")
            self.history_page = self._page("history", "任务记录", "查看本次操作的进度、结果和错误信息。")
            self.settings_page = self._page("settings", "设置", "配置显示时间和本机运行环境。")
            self._build_import()
            self._build_library()
            self._build_history()
            self._build_settings()
            self.addSubInterface(self.import_page, icon("DOWNLOAD", "FOLDER_ADD"), "导入备份")
            self.addSubInterface(self.library_page, icon("BOOK_SHELF", "FOLDER"), "聊天资料库")
            self.addSubInterface(self.history_page, icon("HISTORY", "DOCUMENT"), "任务记录")
            self.addSubInterface(self.settings_page, icon("SETTING"), "设置", NavigationItemPosition.BOTTOM)
            self.navigationInterface.setExpandWidth(206)
            self.navigationInterface.setMinimumExpandWidth(980)
            self.navigationInterface.panel.expand(useAni=False)
            self.output_edit.setText(str(output or self.saved.get("output") or default_library()))
            self.work_edit.setText(str(work or self.saved.get("work") or Path.cwd() / "ChatImportWork"))
            self._add_paths(sources, discover=False)
            self._refresh_library()
            if sources:
                QTimer.singleShot(0, lambda: self._start("discover"))

        def _page(self, name, title, description):
            page = QWidget(self)
            page.setObjectName(name)
            outer = QVBoxLayout(page)
            outer.setContentsMargins(0, 0, 0, 0)
            scroll = QScrollArea(page)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
            content = QWidget()
            content.setStyleSheet("background: transparent;")
            layout = QVBoxLayout(content)
            layout.setContentsMargins(28, 30, 28, 24)
            layout.setSpacing(16)
            layout.addWidget(TitleLabel(title, content))
            desc = BodyLabel(description, content)
            desc.setWordWrap(True)
            desc.setStyleSheet("color: #64748b;")
            layout.addWidget(desc)
            scroll.setWidget(content)
            outer.addWidget(scroll)
            page.body = layout
            return page

        def _card(self, page, title, description=""):
            card = CardWidget(page)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(20, 18, 20, 18)
            layout.setSpacing(12)
            layout.addWidget(SubtitleLabel(title, card))
            if description:
                label = BodyLabel(description, card)
                label.setWordWrap(True)
                label.setStyleSheet("color: #64748b;")
                layout.addWidget(label)
            page.body.addWidget(card)
            return card, layout

        def _button(self, text, callback, parent, *, primary=False, glyph=None, busy=True):
            button = (PrimaryPushButton if primary else PushButton)(text, parent)
            if glyph:
                button.setIcon(icon(*glyph))
            button.clicked.connect(callback)
            if busy:
                self._busy_controls.append(button)
            return button

        def _path_field(self, layout, label, field, callback):
            line = QHBoxLayout()
            text = BodyLabel(label)
            text.setFixedWidth(86)
            line.addWidget(text)
            line.addWidget(field, 1)
            browse = self._button("浏览", callback, field.parent(), glyph=("FOLDER",))
            line.addWidget(browse)
            layout.addLayout(line)
            self._busy_controls.append(field)

        def _build_import(self):
            card, layout = self._card(self.import_page, "1  添加备份", "可拖入应用备份目录、包含多个应用的目录或压缩包，也可以分次导入不同应用。")
            buttons = QHBoxLayout()
            buttons.addWidget(self._button("添加文件夹", self._choose_source_folder, card, glyph=("FOLDER_ADD", "FOLDER")))
            buttons.addWidget(self._button("添加压缩包 / 数据库", self._choose_source_files, card, glyph=("ADD",)))
            buttons.addStretch()
            buttons.addWidget(self._button("移出列表", self._remove_selected, card, glyph=("REMOVE",)))
            layout.addLayout(buttons)
            self.source_table = TableWidget(card)
            self.source_table.setColumnCount(3)
            self.source_table.setHorizontalHeaderLabels(["应用", "备份位置", "状态"])
            self.source_table.verticalHeader().hide()
            self.source_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.source_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            self.source_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.source_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            self.source_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.source_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            self.source_table.setMinimumHeight(160)
            self.source_table.setMaximumHeight(190)
            layout.addWidget(self.source_table)
            self.source_hint = BodyLabel("尚未添加备份。原始备份会保持只读。", card)
            self.source_hint.setStyleSheet("color: #64748b;")
            layout.addWidget(self.source_hint)

            card, layout = self._card(self.import_page, "2  选择资料库", "以后导入新的备份时，继续选择同一个资料库目录。工具会合并新增消息并保留已有记录与批注。")
            self.output_edit = LineEdit(card)
            self.work_edit = LineEdit(card)
            self._path_field(layout, "资料库", self.output_edit, lambda: self._choose_destination(self.output_edit))
            self._path_field(layout, "工作目录", self.work_edit, lambda: self._choose_destination(self.work_edit))
            self.output_edit.editingFinished.connect(self._refresh_library)
            self.download_check = CheckBox("允许下载备份中记录的在线媒体", card)
            self.download_check.setChecked(False)
            self._busy_controls.append(self.download_check)
            layout.addWidget(self.download_check)

            card, layout = self._card(self.import_page, "3  检查并导入")
            # Keep actions visible while long source lists scroll above them.
            self.import_page.body.removeWidget(card)
            footer = QWidget(self.import_page)
            footer_layout = QVBoxLayout(footer)
            footer_layout.setContentsMargins(28, 0, 28, 16)
            footer_layout.addWidget(card)
            self.import_page.layout().addWidget(footer)
            buttons = QHBoxLayout()
            self.scan_button = self._button("识别来源", lambda: self._start("discover"), card, glyph=("SEARCH",))
            self.inspect_button = self._button("检查数据库", lambda: self._start("inspect"), card, glyph=("DOCUMENT",))
            self.import_button = self._button("导入 / 更新资料库", lambda: self._start("import"), card, primary=True, glyph=("SYNC", "DOWNLOAD"))
            self.cancel_button = self._button("完成当前步骤后停止", self._cancel, card, busy=False)
            self.cancel_button.setEnabled(False)
            for button in (self.scan_button, self.inspect_button, self.import_button):
                buttons.addWidget(button)
            buttons.addStretch()
            buttons.addWidget(self.cancel_button)
            layout.addLayout(buttons)
            self.progress = ProgressBar(card)
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            layout.addWidget(self.progress)
            self.status_label = BodyLabel("准备就绪", card)
            self.status_label.setWordWrap(True)
            layout.addWidget(self.status_label)
            self.import_page.body.addStretch()

        def _build_library(self):
            card, layout = self._card(self.library_page, "当前资料库")
            self.library_path = BodyLabel(card)
            self.library_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.library_path.setWordWrap(True)
            layout.addWidget(self.library_path)
            self.library_count = TitleLabel("尚未导入", card)
            layout.addWidget(self.library_count)
            self.account_table = TableWidget(card)
            self.account_table.setColumnCount(3)
            self.account_table.setHorizontalHeaderLabels(["账号", "账号 ID", "消息"])
            self.account_table.verticalHeader().hide()
            self.account_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.account_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.account_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.account_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            self.account_table.setMinimumHeight(280)
            layout.addWidget(self.account_table)
            buttons = QHBoxLayout()
            self.reader_button = self._button("打开聊天阅读器", self._open_reader, card, primary=True, glyph=("BOOK_SHELF", "LINK"))
            buttons.addWidget(self.reader_button)
            buttons.addWidget(self._button("打开资料库目录", self._open_output, card, glyph=("FOLDER",)))
            buttons.addWidget(self._button("刷新", self._refresh_library, card, glyph=("SYNC",)))
            buttons.addStretch()
            layout.addLayout(buttons)
            self.reader_status = BodyLabel("阅读器在本机运行，可浏览、搜索和查看媒体。", card)
            self.reader_status.setWordWrap(True)
            layout.addWidget(self.reader_status)
            card, layout = self._card(self.library_page, "补全在线媒体",
                "为当前资料库的全部账号补下载记录中的图片、视频、语音等媒体，无需重新选择备份。已失效或不支持的资源会列在任务结果中。")
            self.library_download_check = CheckBox("允许下载记录中的在线媒体", card)
            self.library_download_check.setChecked(False)
            self._busy_controls.append(self.library_download_check)
            layout.addWidget(self.library_download_check)
            self.library_download_button = self._button("补下载在线媒体", lambda: self._start("download"),
                card, primary=True, glyph=("DOWNLOAD",))
            layout.addWidget(self.library_download_button, 0, Qt.AlignmentFlag.AlignLeft)
            self.media_result_label = BodyLabel("勾选后点击按钮开始。进度与停止操作显示在任务记录中。", card)
            self.media_result_label.setWordWrap(True)
            layout.addWidget(self.media_result_label)
            # Keep the explicit network action visible while accounts scroll.
            self.library_page.body.removeWidget(card)
            footer = QWidget(self.library_page)
            footer_layout = QVBoxLayout(footer)
            footer_layout.setContentsMargins(28, 0, 28, 16)
            footer_layout.addWidget(card)
            self.library_page.layout().addWidget(footer)
            card, layout = self._card(self.library_page, "目录说明")
            layout.addWidget(BodyLabel("wx_：微信账号    xhs_：小红书账号    dy_：多闪账号", card))
            label = BodyLabel("图片、视频、语音等媒体保存在 attachments 中。更新时继续使用整个资料库目录，保留 reader.sqlite 以保存批注与历史导入状态。", card)
            label.setWordWrap(True)
            layout.addWidget(label)
            self.library_page.body.addStretch()

        def _build_history(self):
            card, layout = self._card(self.history_page, "当前任务")
            self.history_progress = ProgressBar(card)
            self.history_progress.setRange(0, 100)
            self.history_progress.setValue(0)
            layout.addWidget(self.history_progress)
            self.history_status = BodyLabel("准备就绪", card)
            self.history_status.setWordWrap(True)
            layout.addWidget(self.history_status)
            self.history_cancel_button = self._button("完成当前步骤后停止", self._cancel, card, busy=False)
            self.history_cancel_button.setEnabled(False)
            layout.addWidget(self.history_cancel_button, 0, Qt.AlignmentFlag.AlignLeft)
            card, layout = self._card(self.history_page, "资料库导入历史", "保存在资料库中的最近 100 次成功合并。")
            self.history_table = TableWidget(card)
            self.history_table.setColumnCount(5)
            self.history_table.setHorizontalHeaderLabels(["时间", "应用", "新增", "更新", "状态"])
            self.history_table.verticalHeader().hide()
            self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            for col in range(1, 5):
                self.history_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
            self.history_table.setMinimumHeight(150)
            self.history_table.setMaximumHeight(220)
            layout.addWidget(self.history_table)
            card, layout = self._card(self.history_page, "运行日志")
            self.log_view = PlainTextEdit(card)
            self.log_view.setReadOnly(True)
            self.log_view.setFont(QFont("Consolas", 10))
            self.log_view.setMinimumHeight(240)
            self.log_view.document().setMaximumBlockCount(5000)
            layout.addWidget(self.log_view)
            card, layout = self._card(self.history_page, "最近一次结果")
            self.result_view = PlainTextEdit(card)
            self.result_view.setReadOnly(True)
            self.result_view.setMinimumHeight(170)
            self.result_view.setPlaceholderText("检查或导入完成后，结果会显示在这里。")
            layout.addWidget(self.result_view)
            buttons = QHBoxLayout()
            buttons.addWidget(self._button("保存本次日志", self._save_log, card, glyph=("SAVE",), busy=False))
            buttons.addStretch()
            layout.addLayout(buttons)
            self.history_page.body.addStretch()

        def _build_settings(self):
            card, layout = self._card(self.settings_page, "显示与运行环境")
            self.timezone_edit = LineEdit(card)
            self.timezone_edit.setText(str(self.saved.get("timezone", "Asia/Shanghai")))
            self.node_edit = LineEdit(card)
            self.node_edit.setText(str(self.saved.get("node", "node")))
            for name, field in (("显示时区", self.timezone_edit), ("Node.js 程序", self.node_edit)):
                line = QHBoxLayout()
                label = BodyLabel(name, card)
                label.setFixedWidth(110)
                line.addWidget(label)
                line.addWidget(field)
                layout.addLayout(line)
                self._busy_controls.append(field)
            label = BodyLabel("时区使用 Asia/Shanghai 等 IANA 名称。Node.js 用于资料库合并和阅读器；可以填写 node 或可执行文件的完整路径。", card)
            label.setWordWrap(True)
            label.setStyleSheet("color: #64748b;")
            layout.addWidget(label)
            layout.addWidget(self._button("保存设置", self._save_settings, card, glyph=("SAVE",)), 0, Qt.AlignmentFlag.AlignLeft)
            card, layout = self._card(self.settings_page, "微信解密参数（可选）",
                "默认从备份自动识别。自动解密失败时，可填写数据库密码，或填写 UIN 与 IMEI。参数仅用于当前窗口中的检查和导入，不保存到设置或日志。")
            self.password_edit = LineEdit(card)
            self.imei_edit = LineEdit(card)
            self.uin_edit = LineEdit(card)
            for name, field in (("数据库密码", self.password_edit), ("UIN", self.uin_edit), ("IMEI", self.imei_edit)):
                line = QHBoxLayout()
                label = BodyLabel(name, card)
                label.setFixedWidth(110)
                line.addWidget(label)
                field.setEchoMode(QLineEdit.EchoMode.Password)
                field.setPlaceholderText("留空以自动识别")
                line.addWidget(field)
                layout.addLayout(line)
                self._busy_controls.append(field)
            card, layout = self._card(self.settings_page, "关于")
            layout.addWidget(BodyLabel(APP_TITLE, card))
            label = BodyLabel("用于整理已有的 Android 聊天备份。支持的来源和数据库格式会在检查结果中列出；识别失败时保留原始文件，并显示具体原因。", card)
            label.setWordWrap(True)
            layout.addWidget(label)
            self.settings_page.body.addStretch()

        def _log(self, text):
            stamp = datetime.now().strftime("%H:%M:%S")
            self.log_view.appendPlainText(f"[{stamp}] {text}")
            self.status_label.setText(str(text)[-240:])
            self.history_status.setText(str(text)[-240:])

        def _error(self, text):
            self._log(text)
            QMessageBox.warning(self, "操作未完成", text)

        def _choose_source_folder(self):
            value = QFileDialog.getExistingDirectory(self, "选择应用备份或多个应用的上级目录")
            if value:
                self._add_paths([value])

        def _choose_source_files(self):
            values, _ = QFileDialog.getOpenFileNames(self, "选择备份压缩包或数据库", "",
                "备份与数据库 (*.zst *.tar *.gz *.tgz *.db *.sqlite *.sqlite3);;所有文件 (*)")
            self._add_paths(values)

        def _choose_destination(self, field):
            value = QFileDialog.getExistingDirectory(self, "选择目录", field.text())
            if value:
                field.setText(value)
                self._refresh_library()

        def _add_paths(self, values, *, discover=True):
            if self.worker is not None:
                return
            for value in values:
                path = Path(value).expanduser().resolve()
                if str(path) not in self.sources:
                    self.sources.append(str(path))
            self._fill_source_table()
            if values and discover:
                self._start("discover")

        def _fill_source_table(self, discovered=None):
            records = discovered if discovered is not None else [
                {"source": path, "app": "", "label": "待识别"} for path in self.sources]
            self.source_table.setRowCount(len(records))
            for i, item in enumerate(records):
                source = str(item.get("source", item.get("path", "")))
                app = str(item.get("app", ""))
                for col, value in enumerate((APP_NAMES.get(app, app or "—"), source,
                        str(item.get("label") or ("已识别" if app else "待识别")))):
                    cell = QTableWidgetItem(value)
                    cell.setData(Qt.ItemDataRole.UserRole, source)
                    cell.setToolTip(value)
                    self.source_table.setItem(i, col, cell)
            self.source_hint.setText(f"已选择 {len(self.sources)} 个位置，识别到 {len(records)} 项来源。可继续添加其他备份。" if discovered is not None
                                    else f"已选择 {len(self.sources)} 个备份位置。可拖入文件夹或压缩包。")

        def _remove_selected(self):
            selected = {self.source_table.item(index.row(), 1).data(Qt.ItemDataRole.UserRole)
                        for index in self.source_table.selectionModel().selectedRows()}
            # A parent source can expand to several detected Android users.
            self.sources = [value for value in self.sources if not any(
                Path(item) == Path(value) or Path(item).is_relative_to(Path(value)) for item in selected)]
            self._fill_source_table()

        def dragEnterEvent(self, event):
            if self.worker is None and event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
                event.acceptProposedAction()

        def dropEvent(self, event):
            if self.worker is None:
                self._add_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
                event.acceptProposedAction()

        def _options(self):
            if not self.output_edit.text().strip() or not self.work_edit.text().strip():
                raise ValueError("请填写资料库目录和工作目录。")
            return {"output": Path(self.output_edit.text()).expanduser().resolve(),
                "work": Path(self.work_edit.text()).expanduser().resolve(),
                "timezone": self.timezone_edit.text().strip() or "Asia/Shanghai",
                "node": self.node_edit.text().strip() or "node",
                "password": self.password_edit.text() or None,
                "imei": self.imei_edit.text().strip() or None,
                "uin": self.uin_edit.text().strip() or None,
                "library_download_media": self.library_download_check.isChecked(),
                "download_media": self.download_check.isChecked()}

        def _start(self, operation):
            if self.worker is not None:
                return
            if operation != "download" and not self.sources:
                self._error("请先添加至少一个备份目录或文件。")
                return
            try:
                options = self._options()
                if operation == "download":
                    if not options["library_download_media"]:
                        raise ValueError("请先勾选“允许下载记录中的在线媒体”，再点击补下载。")
                    if not (options["output"] / "reader.sqlite").is_file():
                        raise ValueError("资料库尚未生成。请先选择已经导入的资料库。")
                else:
                    for value in self.sources:
                        if not Path(value).exists():
                            raise ValueError(f"备份位置不存在：{value}")
                self._save_settings(silent=True)
            except (OSError, ValueError) as error:
                self._error(str(error))
                return
            labels = {"discover": "识别来源", "inspect": "检查数据库", "import": "导入 / 更新资料库", "download": "补下载在线媒体"}
            self._log(f"开始：{labels[operation]}")
            self.last_result = None
            self._set_busy(True)
            worker = Worker(operation, () if operation == "download" else self.sources, options, self)
            self.worker = worker
            worker.log.connect(self._log)
            worker.succeeded.connect(lambda result: self._completed(operation, result))
            worker.failed.connect(self._failed)
            worker.finished.connect(self._worker_finished)
            if operation == "download":
                self.media_result_output = options["output"]
                self.media_result_label.setText("正在补下载，请在任务记录中查看进度。")
                self.switchTo(self.history_page)
            worker.start()

        def _set_busy(self, busy):
            for control in self._busy_controls:
                control.setEnabled(not busy)
            self.cancel_button.setEnabled(busy)
            self.history_cancel_button.setEnabled(busy)
            self.progress.setRange(0, 0 if busy else 100)
            self.history_progress.setRange(0, 0 if busy else 100)
            if not busy:
                completed = isinstance(self.last_result, dict) and self.last_result.get("status") not in ("error", "cancelled")
                self.progress.setValue(100 if completed else 0)
                self.history_progress.setValue(100 if completed else 0)

        def _cancel(self):
            if self.worker is not None:
                self.worker.cancel_event.set()
                self.cancel_button.setEnabled(False)
                self.history_cancel_button.setEnabled(False)
                self._log("已请求停止。当前步骤完成后停止，保留已经完成的内容。")

        def _completed(self, operation, result):
            self.last_result = result
            self.result_view.setPlainText(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            if isinstance(result, dict) and isinstance(result.get("sources"), list):
                self._fill_source_table(result["sources"])
            status = result.get("status", "completed") if isinstance(result, dict) else "completed"
            if operation == "download":
                counts = result.get("totals") if isinstance(result, dict) else None
                summary = " · ".join(f"{label} {counts.get(key, 0)}" for key, label in (
                    ("downloaded", "成功"), ("existing", "已有"), ("failed", "失败"), ("unsupported", "不支持"))) if isinstance(counts, dict) else "详细结果见任务记录。"
                stopped = status in ("cancelled", "canceled", "stopped")
                message = ("补下载已停止，已完成的内容已保留。" if stopped else "补下载已结束。") + summary
                if isinstance(counts, dict) and counts.get("downloaded", 0):
                    message += "。刷新已打开的阅读器页面即可查看新媒体。"
                self.media_result_label.setText(message)
                self._log(message)
                self.switchTo(self.history_page)
                return
            if status in ("cancelled", "canceled", "stopped"):
                self._log("任务已停止，已完成的内容已保留。")
            elif status in ("error", "failed", "partial", "partial_failure"):
                self._log("任务包含未完成项目，请查看任务记录。")
                self.switchTo(self.history_page)
            elif operation == "import":
                self._log("导入完成。可打开聊天阅读器查看合并结果。")
                self._refresh_library()
                self.switchTo(self.library_page)
            elif operation == "inspect":
                self._log("数据库检查完成，详细结果见任务记录。")
                self.switchTo(self.history_page)
            else:
                self._log("来源识别完成。")

        def _failed(self, message):
            self.last_result = {"status": "error", "error": message}
            self.result_view.setPlainText(json.dumps(self.last_result, ensure_ascii=False, indent=2))
            self._log(f"操作失败：{message}")
            if self.worker is not None and self.worker.operation == "download":
                self.media_result_label.setText("补下载未完成，请查看任务记录。")
            self.switchTo(self.history_page)

        def _worker_finished(self):
            worker = self.worker
            self.worker = None
            self._set_busy(False)
            if worker is not None:
                worker.deleteLater()

        def _refresh_library(self):
            self.account_table.setRowCount(0)
            self.history_table.setRowCount(0)
            if not self.output_edit.text().strip():
                self.library_path.setText("尚未选择资料库")
                self.library_count.setText("请选择资料库目录")
                return
            try:
                path = Path(self.output_edit.text()).expanduser().resolve()
                self.library_path.setText(str(path))
                if self.media_result_output != path:
                    self.media_result_label.setText("勾选后点击按钮开始。进度与停止操作显示在任务记录中。")
                result = library_summary(path)
                accounts = result["accounts"]
                self.library_count.setText(f"{len(accounts)} 个账号  ·  {result['messages']:,} 条消息")
                self.account_table.setRowCount(len(accounts))
                for i, account in enumerate(accounts):
                    for col, text in enumerate((account["name"], account["uid"], f"{account['messages']:,}")):
                        self.account_table.setItem(i, col, QTableWidgetItem(str(text)))
                self.history_table.setRowCount(len(result["history"]))
                for i, record in enumerate(result["history"]):
                    values = (record["time"], APP_NAMES.get(record["app"], record["app"]),
                              record["inserted"], record["updated"], record["status"])
                    for col, text in enumerate(values):
                        cell = QTableWidgetItem(str(text))
                        cell.setToolTip(str(record["source"]))
                        self.history_table.setItem(i, col, cell)
                if not self.reader_ready or self.reader_output != path:
                    self.reader_status.setText("阅读器在本机运行，可浏览、搜索和查看媒体。")
            except (OSError, ValueError, sqlite3.Error) as error:
                self.library_count.setText("暂时无法读取资料库")
                self.reader_status.setText(str(error))

        def _save_settings(self, checked=False, *, silent=False):
            del checked
            try:
                options = self._options()
                value = {key: str(options[key]) for key in ("output", "work", "timezone", "node")}
                path = settings_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
                if not silent:
                    self._log("设置已保存。")
            except (OSError, ValueError) as error:
                if not silent:
                    self._error(f"无法保存设置：{error}")

        def _save_log(self):
            name, _ = QFileDialog.getSaveFileName(self, "保存日志", f"chat-import-{datetime.now():%Y%m%d-%H%M%S}.txt", "文本 (*.txt)")
            if name:
                try:
                    Path(name).write_text(self.log_view.toPlainText() + "\n\n" + self.result_view.toPlainText(), encoding="utf-8")
                except OSError as error:
                    self._error(f"无法保存日志：{error}")

        def _open_output(self):
            path = Path(self.output_edit.text()).expanduser().resolve()
            if path.is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            else:
                self._error("资料库目录尚不存在。完成第一次导入后再打开。")

        def _open_reader(self):
            try:
                options = self._options()
                output_path = options["output"]
                if not (output_path / "reader.sqlite").is_file():
                    raise ValueError("资料库尚未生成。请先完成导入。")
                if self.reader_process is not None and self.reader_process.poll() is None and self.reader_output == output_path:
                    if self.reader_timer.isActive():
                        return
                    if self.reader_ready:
                        QDesktopServices.openUrl(QUrl(self.reader_url))
                    else:
                        self.reader_started = time.monotonic()
                        self.reader_timer.start()
                    return
                repo = Path(__file__).resolve().parents[2]
                server = repo / "web" / "server" / "index.mjs"
                if not server.is_file() or not (repo / "web" / "dist" / "index.html").is_file():
                    raise ValueError("未找到阅读器程序或构建文件。请在源码项目的 web 目录安装依赖并运行 npm run build。")
                node = shutil.which(options["node"])
                if node is None:
                    raise ValueError("找不到 Node.js。请安装项目要求的 Node.js，或在设置中填写完整路径。")
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                log_dir = output_path / "import_logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                self.reader_log_path = log_dir / f"reader-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
                self.reader_log = self.reader_log_path.open("wb")
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                self.reader_process = subprocess.Popen([node, str(server), "--data", str(output_path), "--port", str(port)],
                    cwd=str(repo / "web"), stdout=self.reader_log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, creationflags=flags)
                self.reader_log.close()
                self.reader_log = None
                self.reader_output = output_path
                self.reader_url = f"http://127.0.0.1:{port}/"
                self.reader_started = time.monotonic()
                self.reader_ready = False
                self.reader_status.setText("正在启动阅读器，首次索引可能需要一些时间……")
                self.reader_button.setEnabled(False)
                self.reader_timer.start()
            except (OSError, ValueError) as error:
                if self.reader_log is not None:
                    self.reader_log.close()
                    self.reader_log = None
                self._error(str(error))

        def _check_reader(self):
            if self.reader_process is None or self.reader_process.poll() is not None:
                self.reader_timer.stop()
                self.reader_button.setEnabled(True)
                self.reader_status.setText(f"阅读器启动失败。请查看：{self.reader_log_path}")
                return
            try:
                with urllib.request.urlopen(self.reader_url + "api/session", timeout=0.15) as response:
                    ready = response.status == 200
            except (OSError, ValueError):
                ready = False
            if ready:
                self.reader_ready = True
                self.reader_timer.stop()
                self.reader_button.setEnabled(True)
                self.reader_status.setText(f"阅读器已运行：{self.reader_url}")
                QDesktopServices.openUrl(QUrl(self.reader_url))
            elif time.monotonic() - self.reader_started > 120:
                self.reader_timer.stop()
                self.reader_button.setEnabled(True)
                self.reader_status.setText(f"阅读器仍在启动。进度日志：{self.reader_log_path}。稍后再点击打开。")

        def closeEvent(self, event):
            if self.worker is not None and self.worker.isRunning():
                self.worker.cancel_event.set()
                self._log("已请求停止。请等待当前步骤完成，再关闭窗口。")
                event.ignore()
                return
            try:
                self._save_settings(silent=True)
            except ValueError:
                pass
            self.reader_timer.stop()
            # The reader is deliberately detached so an open browser stays usable.
            super().closeEvent(event)

    return MainWindow()


def main(argv=None):
    parser = argparse.ArgumentParser(description="微信、多闪、小红书备份导入与增量更新桌面界面")
    parser.add_argument("sources", nargs="*", help="启动时添加的备份位置")
    parser.add_argument("--output", type=Path, help="统一资料库目录")
    parser.add_argument("--work-dir", type=Path, help="解压和处理的工作目录")
    args = parser.parse_args(argv)
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QFont, QColor
        from qfluentwidgets import setTheme, setThemeColor, Theme
    except ImportError:
        print(GUI_HINT, file=sys.stderr)
        return 1
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_TITLE)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    setTheme(Theme.LIGHT)
    setThemeColor(QColor("#2563eb"))
    window = create_window(sources=args.sources, output=args.output, work=args.work_dir)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
