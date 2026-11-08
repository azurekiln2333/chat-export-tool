"""Desktop contracts; optional Qt tests never contact a real media service."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from duoshan_export import desktop_sources, gui, library_media
from duoshan_export.progress import ProgressEvent

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PyQt6.QtCore import QEvent, QPoint, Qt
    from PyQt6.QtGui import QDesktopServices
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QFileDialog, QLabel
    import qfluentwidgets  # Verify the optional GUI dependency before running controls.
    HAS_QT = True
except ImportError:
    HAS_QT = False


class SettingsTests(unittest.TestCase):
    def test_progress_event_remains_a_string_with_optional_stage_counts(self):
        event = ProgressEvent("正在处理", stage="校验媒体", completed=3, total=7, unit="文件")
        self.assertIsInstance(event, str)
        self.assertEqual(event, "正在处理")
        self.assertEqual((event.stage, event.completed, event.total, event.unit, event.state),
            ("校验媒体", 3, 7, "文件", "running"))
        self.assertIsNone(ProgressEvent("扫描", stage="扫描").total)
        with self.assertRaises(ValueError):
            ProgressEvent("坏计数", stage="扫描", completed=-1)

    def test_reader_port_selection_excludes_both_boundaries_and_range(self):
        for port in (64967, 65000, 65066):
            self.assertFalse(gui.reader_port_allowed(port))
        for port in (64966, 65067):
            self.assertTrue(gui.reader_port_allowed(port))
        with patch.object(gui.socket, "socket") as factory, patch.object(gui.secrets, "randbelow", side_effect=(0, 64966 - 49152)):
            probe = factory.return_value.__enter__.return_value
            probe.bind.side_effect = [OSError("already bound"), None]
            self.assertEqual(gui.available_reader_port(), 64966)
            self.assertEqual(factory.call_count, 2)
            self.assertEqual([call.args[0][1] for call in probe.bind.call_args_list], [49152, 64966])
        with patch.object(gui.socket, "socket") as factory:
            probe = factory.return_value.__enter__.return_value
            probe.bind.side_effect = OSError("occupied")
            with self.assertRaises(OSError):
                gui.available_reader_port()
            self.assertEqual(probe.bind.call_count, 64)
            self.assertTrue(all(gui.reader_port_allowed(call.args[0][1]) for call in probe.bind.call_args_list))
    def test_source_labels_match_explicit_legacy_aliases(self):
        for value in ("WeChat", "com.tencent.mm", " 微信 "):
            self.assertEqual(gui._source_app(value), "wechat")
        for value in ("Xiaohongshu", "com.xingin.xhs", "小红书", "xhs"):
            self.assertEqual(gui._source_app(value), "xhs")
        for value in ("Duoshan", "douyin", "my.maya.android", "多闪/抖音"):
            self.assertEqual(gui._source_app(value), "duoshan")
        for value, app in (("PC 微信", "wechat_pc"), ("WeChat PC", "wechat_pc"), ("PC QQ", "qq_pc")):
            self.assertEqual(gui._source_app(value), app)
        self.assertEqual(gui._source_app("WeChat + Duoshan"), "")

    def test_summary_source_uses_manifest_then_prefix_and_recorded_metadata(self):
        with tempfile.TemporaryDirectory(prefix="chat-gui-summary-") as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "reader.sqlite")) as db:
                db.executescript("CREATE TABLE accounts(uid TEXT,name TEXT);"
                    "CREATE TABLE conversations(uid TEXT,message_count INTEGER);"
                    "CREATE TABLE library_accounts(uid TEXT,app TEXT);"
                    "CREATE TABLE messages(uid TEXT,raw_json TEXT);")
                db.executemany("INSERT INTO accounts VALUES(?,?)", [(uid, uid) for uid in ("authoritative", "prefix", "metadata", "unknown", "statistics", "registered-unknown")])
                db.execute("INSERT INTO library_accounts VALUES('authoritative','wechat')")
                db.execute("INSERT INTO library_accounts VALUES('registered-unknown','future-app')")
                db.execute("INSERT INTO messages VALUES('authoritative',?)", (json.dumps({"_export": {"app": "xhs"}}),))
                db.execute("INSERT INTO messages VALUES('metadata',?)", (json.dumps({"_export": {"app": "xhs"}}),))
                db.commit()
            (root / "dy_prefix").mkdir()
            (root / "dy_registered-unknown").mkdir()
            (root / "statistics").mkdir()
            (root / "statistics/statistics.json").write_text('{"app":"Xiaohongshu"}', encoding="utf-8")
            before = (root / "reader.sqlite").read_bytes()
            summary = gui.library_summary(root)
            self.assertEqual({item["uid"]: item["app"] for item in summary["accounts"]},
                {"authoritative": "wechat", "prefix": "duoshan", "metadata": "xhs", "unknown": "", "statistics": "xhs", "registered-unknown": ""})
            (root / "export_summary.json").write_text('{"app":"WeChat"}', encoding="utf-8")
            updated = {item["uid"]: item["app"] for item in gui.library_summary(root)["accounts"]}
            self.assertEqual(updated["unknown"], "wechat")
            self.assertEqual(updated["registered-unknown"], "")
            self.assertEqual(before, (root / "reader.sqlite").read_bytes())

    def test_legacy_settings_are_read_without_mutation_and_new_settings_win(self):
        with tempfile.TemporaryDirectory(prefix="chat-gui-settings-") as directory:
            with patch.dict(os.environ, {"LOCALAPPDATA": directory, "CHAT_ARCHIVE_RUNTIME": str(Path(directory) / "portable")}):
                legacy = Path(directory) / "AzureKilnChatArchive/settings.json"
                legacy.parent.mkdir()
                old = {"output": "existing-library", "work": "existing-work"}
                legacy.write_text(json.dumps(old), encoding="utf-8")
                before = legacy.read_bytes()
                self.assertEqual(gui.read_settings(), old)
                current = gui.settings_path()
                self.assertEqual(current, Path(directory) / "portable/settings.json")
                self.assertFalse(current.exists())
                current.parent.mkdir()
                current.write_text(json.dumps({"output": "new-library"}), encoding="utf-8")
                self.assertEqual(gui.read_settings(), {"output": "new-library"})
                self.assertEqual(legacy.read_bytes(), before)

    def test_partial_batch_history_does_not_claim_whole_source_completed(self):
        with tempfile.TemporaryDirectory(prefix="chat-gui-partial-") as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "reader.sqlite")) as db:
                db.executescript("CREATE TABLE accounts(uid TEXT,name TEXT);"
                    "CREATE TABLE conversations(uid TEXT,message_count INTEGER);"
                    "CREATE TABLE import_batches(app TEXT,source_id TEXT,imported_at TEXT,result_json TEXT,projection_status TEXT);")
                db.executemany("INSERT INTO import_batches VALUES('qq_pc',?,?,?,?)", [
                    ("partial:qq_pc:first", "1", '{"status":"completed","inserted":2}', "completed"),
                    ("qq_pc:second", "2", '{"status":"completed","inserted":4}', "completed"),
                    ("partial:qq_pc:third", "3", '{"status":"completed"}', "pending"),
                ])
                db.commit()
            before = (root / "reader.sqlite").read_bytes()
            rows = gui.library_summary(root)["history"]
            self.assertEqual([row["status"] for row in rows], ["待完成导出", "已完成", "部分完成，待重试"])
            self.assertEqual(rows[-1]["inserted"], 2)
            self.assertEqual((root / "reader.sqlite").read_bytes(), before)

    def test_invalid_settings_can_fall_back_to_legacy(self):
        with tempfile.TemporaryDirectory(prefix="chat-gui-settings-") as directory:
            with patch.dict(os.environ, {"LOCALAPPDATA": directory, "CHAT_ARCHIVE_RUNTIME": str(Path(directory) / "portable")}):
                current = gui.settings_path()
                current.parent.mkdir()
                current.write_text("{broken", encoding="utf-8")
                legacy = Path(directory) / "AzureKilnChatArchive/settings.json"
                legacy.parent.mkdir()
                legacy.write_text('{"output":"kept"}', encoding="utf-8")
                self.assertEqual(gui.read_settings(), {"output": "kept"})


@unittest.skipUnless(HAS_QT, "Install the optional GUI dependencies to test desktop controls")
class MediaControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chat-gui-media-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "library"
        self.output.mkdir()
        with closing(sqlite3.connect(self.output / "reader.sqlite")) as db:
            db.executescript("CREATE TABLE accounts(uid TEXT,name TEXT);"
                             "CREATE TABLE conversations(uid TEXT,message_count INTEGER);")
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "settings"), "CHAT_ARCHIVE_RUNTIME": str(self.root / "runtime")})
        environment.start()
        self.addCleanup(environment.stop)
        self.download = Mock(return_value={"status": "completed", "totals": {
            "downloaded": 2, "existing": 3, "failed": 1, "unsupported": 4}})
        self.recorded_url = "https://media.example.invalid/a.jpg?sign=keep%2Bthis%3D&expires=123&opaque=<image>"
        self.attempted_url = "https://cdn.example.invalid/b.jpg?signature=full%2Ftoken&width=1000"
        self.details_result = {"status": "completed", "as_of": "2026-11-07T00:00:00Z",
            "data_status": "active_snapshot", "snapshot_note": "下载进行中，这是读取时的快照。",
            "resources": [
                {"resource_id": "denied", "app": "duoshan", "kind": "image", "status": "unsupported",
                 "status_text": "不支持", "reason": "host_not_allowed", "reason_text": "域名不在允许下载范围内",
                 "recorded_urls": [self.recorded_url], "attempted_urls": [], "attempts_known": False,
                 "messages": [{"uid": "123", "msg_uuid": "message-denied", "conversation_id": "c-1",
                               "conversation_name": "示例会话", "account_directory": "dy_123"}]},
                {"resource_id": "http-failed", "app": "xhs", "kind": "video", "status": "failed",
                 "status_text": "失败", "reason": "http_403", "reason_text": "HTTP 403",
                 "recorded_urls": [self.recorded_url], "attempted_urls": [self.attempted_url],
                 "attempts_known": True, "messages": []},
                {"resource_id": "missing", "app": "wechat", "kind": "voice", "status": "unsupported",
                 "status_text": "不支持", "reason": "missing_url", "reason_text": "记录中没有可下载地址",
                 "recorded_urls": [], "attempted_urls": [], "attempts_known": True, "messages": []},
                {"resource_id": "saved", "app": "xhs", "kind": "image", "status": "ok",
                 "status_text": "成功", "recorded_urls": [], "attempted_urls": [], "messages": []}],
            "report_html": str(self.output / "media_reports/details.html"),
            "report_text": str(self.output / "media_reports/details.txt"),
            "report_json": str(self.output / "media_reports/details.json")}
        self.inspect = Mock(return_value=self.details_result)
        # Patch individual functions. Replacing sys.modules can evict newly
        # imported modules while leaving stale package attributes behind, so
        # later worker imports escape mocks and reach actual local accounts.
        for mocked in (patch.object(library_media, "download_library_media", self.download),
                       patch.object(library_media, "inspect_library_media", self.inspect),
                       patch.object(desktop_sources, "discover_desktop_sources", return_value=[])):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.window = gui.create_window(output=self.output, work=self.root / "work", auto_discover=False)
        self.window._error = Mock()

    def tearDown(self):
        self.wait_for_discovery()
        if self.window.details_worker is not None:
            self.window.details_worker.cancel_event.set()
            self.wait_for_details()
        if self.window.worker is not None:
            self.window._cancel()
            self.wait_for_worker()
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_for_worker(self):
        deadline = time.monotonic() + 8
        while self.window.worker is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertIsNone(self.window.worker, "Desktop worker failed to finish")

    def wait_for_details(self):
        deadline = time.monotonic() + 8
        while self.window.details_worker is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertIsNone(self.window.details_worker, "Media detail worker failed to finish")

    def wait_for_discovery(self):
        deadline = time.monotonic() + 8
        while self.window.discovery_worker is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertIsNone(self.window.discovery_worker, "Local discovery worker failed to finish")

    def wait_for_event(self, event):
        deadline = time.monotonic() + 5
        while not event.is_set() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(event.is_set(), "Worker failed to enter fixture")

    def test_neutral_title_and_explicit_opt_in_without_backup_selection(self):
        window = self.window
        self.assertEqual(window.windowTitle(), "Chat Archive")
        self.assertFalse(any("AzureKiln" in label.text() for label in window.findChildren(QLabel)))
        self.assertFalse(window.library_download_check.isChecked())
        QTest.qWait(30)
        self.download.assert_not_called()
        window.library_download_button.click()
        window._error.assert_called_once()
        self.download.assert_not_called()
        window.library_download_check.setChecked(True)
        QTest.qWait(30)
        self.download.assert_not_called()
        window.sources = [str(self.root / "missing-backup")]
        window.library_download_button.click()
        self.wait_for_worker()
        self.download.assert_called_once()
        args, kwargs = self.download.call_args
        self.assertEqual(args, (self.output, self.root / "work"))
        self.assertTrue(kwargs["download_media"])
        self.assertEqual(kwargs["workers"], 6)
        self.assertNotIn("sources", kwargs)
        for label in ("成功 2", "已有 3", "失败 1", "不支持 4"):
            self.assertIn(label, window.media_result_label.text())
        saved = json.loads(gui.settings_path().read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {"output", "work", "timezone", "node"})
        window.resize(1180, 780)
        window.show()
        window.switchTo(window.library_page)
        QTest.qWait(350)
        top = window.library_download_button.mapTo(window, QPoint(0, 0))
        self.assertGreaterEqual(top.y(), 0)
        self.assertLessEqual(top.y() + window.library_download_button.height(), window.height())
        self.assertTrue(window.library_download_button.isVisible())

    def test_media_worker_error_restores_controls(self):
        window = self.window
        window.password_edit.setText("fixture-error-secret")
        self.download.side_effect = RuntimeError("fixture unavailable fixture-error-secret")
        window.library_download_check.setChecked(True)
        window.library_download_button.click()
        self.wait_for_worker()
        self.assertEqual(window.last_result["status"], "error")
        self.assertIn("fixture unavailable", window.result_view.toPlainText())
        self.assertNotIn("fixture-error-secret", window.result_view.toPlainText())
        self.assertNotIn("fixture-error-secret", window.log_view.toPlainText())
        self.assertTrue(window.library_download_button.isEnabled())
        self.assertFalse(window.history_cancel_button.isEnabled())
        self.assertEqual(window.history_progress.value(), 0)

    def test_live_worker_log_keeps_full_signed_urls_and_compact_summary(self):
        window = self.window
        window.password_edit.setText("fixture-private-password")
        window.uin_edit.setText("fixture-private-uin")
        window.imei_edit.setText("fixture-private-imei")
        long_url = "https://media.example.invalid/image?signature=" + "abc%2Fxyz%2B" * 350 + "&markup=<b>literal</b>&expires=9999999999&token=fixture-private-password-fixture-private-uin-fixture-private-imei"
        redirect_url = "https://cdn.example.invalid/image?token=exact%3D%2F%2B-fixture-private-uin&x=1"
        lines = [
            f"[✓]{long_url} | 下载成功",
            f"[=]{redirect_url} | 已有本地文件（记录候选，未发起新请求）",
            f"[x]{redirect_url} | 失败：服务器返回 HTTP 403，拒绝访问",
            f"[!]{redirect_url} | 不支持：地址不在支持范围（未发起请求）",
            f"[→]{long_url} | HTTP 302 重定向至 {redirect_url}",
            "[!]无 URL | 不支持：备份未记录视频下载网址",
        ]
        summary = "媒体进度 6/9 · 成功 1 · 已有 1 · 失败 1 · 不支持 3"
        emitted = threading.Event()
        release = threading.Event()

        def download(*args, **kwargs):
            kwargs["progress"]("凭据测试 fixture-private-password fixture-private-uin fixture-private-imei")
            kwargs["progress"]("普通日志 https://media.example.invalid/normal?sign=fixture-private-password")
            print("[x]https://media.example.invalid/stdout?sign=fixture-private-uin | 原始输出 fixture-private-password")
            kwargs["progress"](summary)
            for line in lines:
                kwargs["progress"](line)
            kwargs["progress"](f"[x]{redirect_url} | 失败详情 fixture-private-password fixture-private-imei")
            kwargs["progress"]("[!]无 URL | 缺失详情 fixture-private-password")
            emitted.set()
            while not release.is_set() and not kwargs["cancel"]():
                time.sleep(0.01)
            return {"status": "completed", "totals": {
                "downloaded": 1, "existing": 1, "failed": 1, "unsupported": 3}}

        self.download.side_effect = download
        with patch.object(QDesktopServices, "openUrl") as opener, patch("urllib.request.urlopen") as network:
            window.library_download_check.setChecked(True)
            window.library_download_button.click()
            try:
                self.wait_for_event(emitted)
                QTest.qWait(30)
                self.assertTrue(window.worker.isRunning(), "URLs must be visible before download completion")
                log = window.log_view.toPlainText()
                for line in lines:
                    self.assertIn(line, log)
                self.assertIn("<b>literal</b>", log)
                self.assertIn("凭据测试 [已隐藏] [已隐藏] [已隐藏]", log)
                self.assertIn("普通日志 https://media.example.invalid/normal?sign=[已隐藏]", log)
                self.assertIn("[x]https://media.example.invalid/stdout?sign=[已隐藏] | 原始输出 [已隐藏]", log)
                self.assertIn(f"[x]{redirect_url} | 失败详情 [已隐藏] [已隐藏]", log)
                self.assertIn("[!]无 URL | 缺失详情 [已隐藏]", log)
                outside_urls = log.replace(long_url, "<URL>").replace(redirect_url, "<URL>")
                for secret in ("fixture-private-password", "fixture-private-uin", "fixture-private-imei"):
                    self.assertNotIn(secret, outside_urls)
                self.assertEqual(window.status_label.text(), summary)
                self.assertEqual(window.history_status.text(), summary)
                self.assertEqual(window.status_label.textFormat(), Qt.TextFormat.PlainText)
                self.assertEqual(window.history_status.textFormat(), Qt.TextFormat.PlainText)
                self.assertEqual(window.log_view.document().maximumBlockCount(), 5000)
                opener.assert_not_called()
                network.assert_not_called()
            finally:
                release.set()
                self.wait_for_worker()
        self.assertIn("成功 1", window.history_status.text())
        self.assertIn(long_url, window.log_view.toPlainText())

    def test_structured_stage_progress_is_numeric_redacted_and_resets_by_stage(self):
        window = self.window
        window.password_edit.setText("stage-secret")
        events = queue.Queue()
        emitted = threading.Event()

        def download(*args, **kwargs):
            while not kwargs["cancel"]():
                try:
                    value = events.get(timeout=0.01)
                except queue.Empty:
                    continue
                if value is None:
                    return {"status": "completed", "totals": {}}
                kwargs["progress"](value)
                emitted.set()
            return {"status": "cancelled", "totals": {}}

        self.download.side_effect = download
        window.library_download_check.setChecked(True)
        window.library_download_button.click()

        def send(event):
            emitted.clear()
            events.put(event)
            self.wait_for_event(emitted)
            QTest.qWait(20)

        send(ProgressEvent("校验 stage-secret", stage="校验 stage-secret", completed=3, total=10, unit="文件"))
        self.assertEqual(window.progress.maximum(), 100)
        self.assertEqual(window.history_progress.value(), 30)
        self.assertIn("3/10 文件 · 30%", window.history_stage_label.text())
        self.assertNotIn("stage-secret", window.history_stage_label.text())
        self.assertNotIn("stage-secret", window.log_view.toPlainText())
        send(ProgressEvent("校验完成", stage="校验", completed=10, total=10, state="completed"))
        self.assertEqual(window.history_progress.value(), 100)
        self.assertIn("本阶段完成", window.history_stage_label.text())
        self.assertNotIn("任务完成", window.history_stage_label.text())
        self.assertIsNone(window.last_result)
        self.assertTrue(window.worker.isRunning())
        send(ProgressEvent("正在合并", stage="数据库合并"))
        self.assertEqual(window.progress.maximum(), 0)
        self.assertEqual(window.history_progress.maximum(), 0)
        self.assertIn("总量未知", window.history_stage_label.text())
        send(ProgressEvent("写入", stage="写入消息", completed=2**38, total=2**40, unit="条"))
        self.assertEqual(window.history_progress.value(), 25)
        send(ProgressEvent("无转换项目", stage="转码", completed=0, total=0, state="completed"))
        self.assertEqual(window.progress.maximum(), 100)
        self.assertIn("0/0", window.history_stage_label.text())
        events.put(None)
        self.wait_for_worker()
        self.assertEqual(window.history_progress.value(), 100)
        self.assertEqual(window.history_stage_label.text(), "任务完成")

    def test_inspection_result_is_successful_completion(self):
        from duoshan_export import library
        window = self.window
        window.sources = [str(self.root)]
        window._fill_source_table([{"source": str(self.root), "app": "wechat", "kind": "backup"}])
        with patch.object(library, "inspect_sources", return_value={"status": "inspected", "sources": []}):
            window.inspect_button.click()
            self.wait_for_worker()
        self.assertEqual(window.last_result["status"], "inspected")
        self.assertEqual(window.progress.value(), 100)
        self.assertEqual(window.history_stage_label.text(), "任务完成")

    def test_detected_row_checkboxes_process_only_checked_android_sibling(self):
        from duoshan_export import library
        window = self.window
        parent = self.root / "android"
        first, second = parent / "user_0", parent / "user_999"
        first.mkdir(parents=True)
        second.mkdir()
        records = [{"source": str(path), "app": "wechat", "account": path.name, "kind": "backup"} for path in (first, second)]
        with patch.object(library, "discover_sources", return_value=records):
            window._add_paths([str(parent)])
            self.wait_for_worker()
        self.assertTrue(any(label.text() == "导入聊天记录" for label in window.findChildren(QLabel)))
        self.assertEqual(window.source_table.columnCount(), 5)
        self.assertEqual(window.source_table.item(0, 2).text(), "user_0")
        self.assertEqual(window.source_table.rowCount(), 2)
        window.source_table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        with patch.object(library, "inspect_sources", return_value={"status": "inspected", "sources": [records[0]]}) as inspect:
            window.inspect_button.click()
            self.wait_for_worker()
        self.assertEqual(inspect.call_args.args[0], (first,))
        self.assertEqual(window.source_table.rowCount(), 2)
        self.assertEqual(window.source_table.item(1, 0).checkState(), Qt.CheckState.Unchecked)
        with patch.object(library, "import_sources", return_value={"status": "completed"}) as importer:
            window.import_button.click()
            self.wait_for_worker()
        self.assertEqual(importer.call_args.args[0], (first,))
        self.assertNotIn(parent, importer.call_args.args[0])
        window.source_table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        with patch.object(library, "inspect_sources") as inspect:
            window.inspect_button.click()
            inspect.assert_not_called()
        self.assertIn("勾选", window._error.call_args.args[0])

    def test_manual_refresh_preserves_checks_errors_and_removable_rows(self):
        from duoshan_export import library
        window = self.window
        parent, unknown = self.root / "sources", self.root / "unknown"
        first, second = parent / "first", parent / "second"
        first.mkdir(parents=True)
        second.mkdir()
        unknown.mkdir()
        records = [{"source": str(first), "app": "wechat_pc", "account": "first", "kind": "desktop"},
                   {"source": str(second), "app": "qq_pc", "account": "second", "kind": "desktop"}]
        def discover(paths):
            if Path(paths[0]) == unknown:
                raise RuntimeError("Unsupported synthetic source")
            return records
        with patch.object(library, "discover_sources", side_effect=discover):
            window._add_paths([str(parent), str(unknown)])
            self.wait_for_worker()
            self.assertEqual(window.source_table.rowCount(), 3)
            self.assertEqual(window.source_table.item(2, 4).text(), "识别失败")
            self.assertIn("Unsupported synthetic", window.source_table.item(2, 4).toolTip())
            window.source_table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
            window.scan_button.click()
            self.wait_for_worker()
            self.assertEqual(window.source_table.item(1, 0).checkState(), Qt.CheckState.Unchecked)
            window.source_table.selectRow(0)
            window._remove_selected()
            window.scan_button.click()
            self.wait_for_worker()
        self.assertEqual({item["source"] for item in window.source_records}, {str(second), str(unknown)})
        self.assertIn(str(parent), window.sources)

    def test_local_discovery_is_async_unchecked_and_preserves_manual_rows(self):
        from duoshan_export import desktop_sources
        window = self.window
        manual = self.root / "manual"
        local = self.root / "pc-account"
        manual.mkdir()
        local.mkdir()
        window._add_paths([str(manual)], discover=False)
        records = [{"source": str(local), "app": "wechat_pc", "account": "wx-fixture", "uid": "a" * 32,
                    "kind": "desktop", "format": "wechat4", "status": "已识别"}]
        thread_ids = []
        def discover():
            thread_ids.append(threading.get_ident())
            return records
        with patch.object(desktop_sources, "discover_desktop_sources", side_effect=discover):
            window.local_refresh_button.click()
            self.wait_for_discovery()
            self.assertNotEqual(thread_ids[0], threading.get_ident())
            self.assertEqual(window.source_table.rowCount(), 2)
            self.assertEqual(window.source_table.item(1, 0).checkState(), Qt.CheckState.Unchecked)
            self.assertEqual(window.source_table.item(1, 1).text(), "PC 微信")
            self.assertEqual(window.source_table.item(1, 2).text(), "wx-fixture")
            window.source_table.item(1, 0).setCheckState(Qt.CheckState.Checked)
            window.local_refresh_button.click()
            self.wait_for_discovery()
            self.assertEqual(window.source_table.item(1, 0).checkState(), Qt.CheckState.Checked)
            window.source_table.selectRow(1)
            window._remove_selected()
            window.local_refresh_button.click()
            self.wait_for_discovery()
        self.assertEqual(window.source_table.rowCount(), 1)
        self.assertEqual(window.source_records[0]["source"], str(manual))
        self.download.assert_not_called()
        self.assertIsNone(window.worker)

    def test_cancelled_manual_discovery_keeps_unprocessed_source_rows(self):
        from duoshan_export import library
        window = self.window
        first, second = self.root / "first-scan", self.root / "second-scan"
        first.mkdir()
        second.mkdir()
        entered, release = threading.Event(), threading.Event()
        def discover(paths):
            entered.set()
            release.wait(5)
            return [{"source": str(first), "app": "wechat", "account": "first", "kind": "backup"}]
        with patch.object(library, "discover_sources", side_effect=discover) as scan:
            try:
                window._add_paths([first, second])
                self.wait_for_event(entered)
                window._cancel()
                release.set()
                self.wait_for_worker()
            finally:
                release.set()
                self.wait_for_worker()
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(window.last_result["status"], "cancelled")
        self.assertEqual({item["source"] for item in window.source_records}, {str(first), str(second)})
        pending = next(item for item in window.source_records if item["source"] == str(second))
        self.assertEqual(pending["status"], "pending")
        self.assertTrue(window.source_checks[window._source_key(pending)])

    def test_startup_discovers_local_accounts_without_running_import(self):
        from duoshan_export import desktop_sources, library
        with patch.object(desktop_sources, "discover_desktop_sources", return_value=[]) as discover, \
             patch.object(library, "import_sources") as importer:
            other = gui.create_window(output=self.output, work=self.root / "work")
            try:
                deadline = time.monotonic() + 5
                while (not discover.called or other.discovery_worker is not None) and time.monotonic() < deadline:
                    QTest.qWait(20)
                discover.assert_called_once_with()
                importer.assert_not_called()
                self.assertIsNone(other.discovery_worker)
            finally:
                other.close()
                other.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_startup_with_manual_sources_also_discovers_local_accounts(self):
        from duoshan_export import library
        source = self.root / "manual-startup"
        source.mkdir()
        manual_entered, release = threading.Event(), threading.Event()

        def manual(paths):
            manual_entered.set()
            release.wait(5)
            return [{"source": str(source), "app": "wechat", "account": "phone", "kind": "backup"}]

        local_records = [{"source": str(self.root / "local-startup"), "app": "qq_pc", "account": "10001", "kind": "desktop"}]
        with patch.object(library, "discover_sources", side_effect=manual), \
             patch.object(desktop_sources, "discover_desktop_sources", return_value=local_records) as discover:
            other = gui.create_window(sources=[source], output=self.output, work=self.root / "work")
            try:
                self.wait_for_event(manual_entered)
                self.assertTrue(other.pending_local_discovery)
                discover.assert_not_called()
                release.set()
                deadline = time.monotonic() + 5
                while (other.worker is not None or not discover.called or other.discovery_worker is not None) and time.monotonic() < deadline:
                    QTest.qWait(20)
                discover.assert_called_once_with()
                self.assertIsNone(other.worker)
                self.assertIsNone(other.discovery_worker)
                self.assertEqual([item["account"] for item in other.source_records], ["phone", "10001"])
                self.assertEqual([other.source_table.item(i, 0).checkState() for i in range(2)],
                    [Qt.CheckState.Checked, Qt.CheckState.Unchecked])
            finally:
                release.set()
                if other.worker is not None:
                    other.worker.wait(5000)
                if other.discovery_worker is not None:
                    other.discovery_worker.wait(5000)
                self.app.processEvents()
                other.close()
                other.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_manually_adding_an_unchecked_local_account_checks_and_retains_it(self):
        from duoshan_export import library
        window = self.window
        source = self.root / "previously-local"
        source.mkdir()
        record = {"source": str(source), "app": "wechat_pc", "account": "fixture", "kind": "desktop"}
        window._fill_source_table([{**record, "_origin": "local"}])
        self.assertEqual(window.source_table.item(0, 0).checkState(), Qt.CheckState.Unchecked)
        with patch.object(library, "discover_sources", return_value=[record]):
            window._add_paths([source])
            self.wait_for_worker()
        self.assertEqual(window.source_table.rowCount(), 1)
        self.assertEqual(window.source_table.item(0, 0).checkState(), Qt.CheckState.Checked)
        window.local_refresh_button.click()
        self.wait_for_discovery()
        self.assertEqual(window.source_table.rowCount(), 1)
        self.assertEqual(window.source_records[0]["_origin"], "manual")

    def test_close_waits_for_local_discovery_to_finish(self):
        window = self.window
        entered, release = threading.Event(), threading.Event()

        def discover():
            entered.set()
            release.wait(5)
            return []

        with patch.object(desktop_sources, "discover_desktop_sources", side_effect=discover):
            try:
                window.show()
                window.local_refresh_button.click()
                self.wait_for_event(entered)
                window.close()
                self.assertTrue(window.close_requested)
                self.assertTrue(window.isVisible())
                self.assertFalse(window.local_refresh_button.isEnabled())
                release.set()
                self.wait_for_discovery()
                QTest.qWait(20)
                self.assertFalse(window.isVisible())
            finally:
                release.set()
                self.wait_for_discovery()

    def test_pc_keys_only_apply_selected_pc_accounts_and_are_never_saved_or_logged(self):
        from duoshan_export import library
        window = self.window
        source = self.root / "pc-account"
        source.mkdir()
        window._fill_source_table([{"source": str(source), "app": "qq_pc", "account": "10001", "uid": "pcuid", "kind": "desktop"}])
        keyfile = self.root / "synthetic-keys.json"
        keyfile.write_text(json.dumps({"database.db": "private-file-key"}), encoding="utf-8")
        window.desktop_key_file_edit.setText(str(keyfile))
        window.desktop_key_edit.setText("private-entered-key")
        def inspect(*args, **kwargs):
            kwargs["progress"]("key detail private-file-key private-entered-key")
            return {"status": "inspected", "fixture": "private-file-key private-entered-key"}
        with patch.object(library, "inspect_sources", side_effect=inspect) as inspector:
            window.inspect_button.click()
            self.wait_for_worker()
        self.assertEqual(inspector.call_args.kwargs["desktop_keys"], {"database.db": "private-file-key", "pcuid": "private-entered-key"})
        self.assertIn("[已隐藏]", window.log_view.toPlainText())
        for value in ("private-file-key", "private-entered-key"):
            self.assertNotIn(value, window.log_view.toPlainText())
            self.assertNotIn(value, window.result_view.toPlainText())
        saved = gui.settings_path().read_text(encoding="utf-8")
        self.assertNotIn("private-", saved)
        self.assertNotIn("synthetic-keys", saved)
        self.assertEqual(set(json.loads(saved)), {"output", "work", "timezone", "node"})
        self.assertEqual(keyfile.read_text(encoding="utf-8"), json.dumps({"database.db": "private-file-key"}))

    def test_bad_key_file_reports_no_content_and_is_ignored_for_android(self):
        from duoshan_export import library
        window = self.window
        source = self.root / "account"
        source.mkdir()
        window._fill_source_table([{"source": str(source), "app": "wechat_pc", "account": "fixture", "kind": "desktop"}])
        keyfile = self.root / "bad-keys.json"
        keyfile.write_text("{private-broken-key", encoding="utf-8")
        window.desktop_key_file_edit.setText(str(keyfile))
        with patch.object(library, "inspect_sources") as inspect:
            window.inspect_button.click()
            inspect.assert_not_called()
        self.assertIn("JSON", window._error.call_args.args[0])
        self.assertNotIn("private-broken-key", window._error.call_args.args[0])
        window.source_records = []
        window.source_table.setRowCount(0)
        window._fill_source_table([{"source": str(source), "app": "wechat", "kind": "backup"}])
        with patch.object(library, "inspect_sources", return_value={"status": "inspected"}) as inspect:
            window.inspect_button.click()
            self.wait_for_worker()
        self.assertNotIn("desktop_keys", inspect.call_args.kwargs)

    def test_stage_counts_survive_cancel_and_failure_without_task_success(self):
        window = self.window
        window.library_download_check.setChecked(True)
        for state in ("cancelled", "failed", "partial_failure"):
            with self.subTest(state=state):
                def download(*args, **kwargs):
                    kwargs["progress"](ProgressEvent("处理到 3 条", stage="消息写入", completed=3, total=10,
                        state="cancelled" if state == "cancelled" else "failed"))
                    if state == "failed":
                        raise RuntimeError("fixture write failed")
                    return {"status": state, "totals": {}}
                self.download.side_effect = download
                window.library_download_button.click()
                self.wait_for_worker()
                self.assertEqual(window.history_progress.value(), 30)
                self.assertIn("3/10", window.history_stage_label.text())
                self.assertNotIn("任务完成", window.history_stage_label.text())
                self.assertIn("任务已停止" if state == "cancelled" else "任务未完成", window.history_stage_label.text())

    def test_partial_import_refreshes_visible_accounts_and_stays_on_task_history(self):
        from duoshan_export import library
        window = self.window
        source = self.root / "partial-account"
        source.mkdir()
        window._fill_source_table([{"source": str(source), "app": "qq_pc", "account": "fixture", "kind": "desktop"}])
        def partially_import(*args, **kwargs):
            with closing(sqlite3.connect(self.output / "reader.sqlite")) as db:
                db.execute("INSERT INTO accounts VALUES('fixture','QQ fixture')")
                db.execute("INSERT INTO conversations VALUES('fixture',2)")
                db.commit()
            return {"status": "partial", "imports": [{"status": "partial", "failed_databases": [{"database": "Msg3.0.db"}]}]}
        with patch.object(library, "import_sources", side_effect=partially_import), patch.object(window, "switchTo", wraps=window.switchTo) as switch:
            window.import_button.click()
            self.wait_for_worker()
        self.assertEqual(window.last_result["status"], "partial")
        self.assertEqual(window.account_table.rowCount(), 1)
        self.assertIn("2 条消息", window.library_count.text())
        self.assertIs(switch.call_args.args[0], window.history_page)
        self.assertIn("任务未完成", window.history_stage_label.text())
        self.assertNotIn("导入完成。", window.log_view.toPlainText())

    def test_history_cancel_reaches_worker_and_retains_completed_totals(self):
        entered = threading.Event()

        def download(*args, **kwargs):
            entered.set()
            deadline = time.monotonic() + 5
            while not kwargs["cancel"]() and time.monotonic() < deadline:
                time.sleep(0.01)
            return {"status": "cancelled", "totals": {
                "downloaded": 1, "existing": 2, "failed": 0, "unsupported": 0}}

        self.download.side_effect = download
        window = self.window
        window.library_download_check.setChecked(True)
        window.library_download_button.click()
        deadline = time.monotonic() + 5
        while not entered.is_set() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(entered.is_set())
        self.assertTrue(window.history_cancel_button.isEnabled())
        window.history_cancel_button.click()
        self.wait_for_worker()
        self.assertEqual(window.last_result["status"], "cancelled")
        self.assertIn("成功 1", window.media_result_label.text())
        self.assertEqual(window.history_progress.value(), 0)

    def test_missing_library_is_rejected_and_switching_clears_previous_summary(self):
        window = self.window
        window.library_download_check.setChecked(True)
        window.library_download_button.click()
        self.wait_for_worker()
        window.output_edit.setText(str(self.root / "missing-library"))
        window._refresh_library()
        self.assertNotIn("成功 2", window.media_result_label.text())
        window.library_download_button.click()
        self.download.assert_called_once()
        window._error.assert_called_once()
        self.assertIsNone(window.worker)

    def test_details_are_offline_without_backup_opt_in_or_work_directory(self):
        window = self.window
        window.sources = [str(self.root / "missing-backup")]
        window.work_edit.clear()
        original_result = {"status": "completed", "previous": True}
        window.last_result = original_result
        with patch.object(QDesktopServices, "openUrl") as opener, patch("urllib.request.urlopen") as network:
            window.library_details_button.click()
            self.wait_for_details()
            dialog = window.details_dialog
            self.assertEqual(self.inspect.call_args.args, (self.output,))
            self.assertFalse(self.inspect.call_args.kwargs["active_download"])
            self.download.assert_not_called()
            self.assertFalse(window.library_download_check.isChecked())
            self.assertIs(window.last_result, original_result)
            self.assertEqual(dialog.status_filter.currentData(), "problems")
            self.assertEqual(dialog.table.rowCount(), 3)
            self.assertIn(self.recorded_url, dialog.detail.toPlainText())
            self.assertIn("不能据此判断是否曾尝试", dialog.detail.toPlainText())
            self.assertIn("dy_123", dialog.detail.toPlainText())
            self.assertIn("message-denied", dialog.detail.toPlainText())
            previous_clipboard = self.app.clipboard().text()
            try:
                dialog.copy_urls_button.click()
                self.assertEqual(self.app.clipboard().text(), self.recorded_url)
                dialog.status_filter.setCurrentIndex(dialog.status_filter.findData("failed"))
                self.assertEqual(dialog.table.rowCount(), 1)
                self.assertIn(self.attempted_url, dialog.detail.toPlainText())
                dialog.copy_urls_button.click()
                self.assertEqual(self.app.clipboard().text(), self.recorded_url + "\n" + self.attempted_url)
            finally:
                self.app.clipboard().setText(previous_clipboard)
            dialog.status_filter.setCurrentIndex(dialog.status_filter.findData(""))
            self.assertEqual(dialog.table.rowCount(), 4)
            dialog.app_filter.setCurrentIndex(dialog.app_filter.findData("wechat"))
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertIn("未记录 URL，无法生成地址", dialog.detail.toPlainText())
            self.assertIn("没有实际请求地址", dialog.detail.toPlainText())
            self.assertFalse(dialog.copy_urls_button.isEnabled())
            dialog.app_filter.setCurrentIndex(0)
            dialog.kind_filter.setCurrentIndex(dialog.kind_filter.findData("video"))
            self.assertEqual(dialog.table.rowCount(), 1)
            dialog.kind_filter.setCurrentIndex(0)
            dialog.search.setText("message-denied")
            QTest.qWait(220)
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertIn(str(self.output), dialog.snapshot_label.text())
            self.assertIn("details.html", dialog.report_label.text())
            dialog.refresh_button.click()
            self.wait_for_details()
            self.assertEqual(self.inspect.call_count, 2)
            self.assertEqual(dialog.search.text(), "message-denied")
            opener.assert_not_called()
            network.assert_not_called()

    def test_detail_success_error_and_cancel_do_not_interrupt_active_download(self):
        download_entered = threading.Event()
        download_cancel = []

        def download(*args, **kwargs):
            download_cancel.append(kwargs["cancel"])
            download_entered.set()
            while not kwargs["cancel"]():
                time.sleep(0.01)
            return {"status": "cancelled", "totals": {"downloaded": 1}}

        self.download.side_effect = download
        window = self.window
        window.library_download_check.setChecked(True)
        window.library_download_button.click()
        self.wait_for_event(download_entered)
        main_worker = window.worker
        main_status = window.history_status.text()
        self.assertTrue(window.history_details_button.isEnabled())
        window.history_details_button.click()
        self.wait_for_details()
        self.assertTrue(self.inspect.call_args.kwargs["active_download"])
        self.assertIs(window.worker, main_worker)
        self.assertTrue(main_worker.isRunning())
        self.assertFalse(download_cancel[0]())
        self.assertEqual(window.history_status.text(), main_status)
        self.assertIsNone(window.last_result)
        self.assertFalse(window.library_download_button.isEnabled())

        self.assertTrue(window.history_cancel_button.isEnabled())
        self.inspect.side_effect = RuntimeError("snapshot fixture unavailable")
        window.details_dialog.refresh_button.click()
        self.wait_for_details()
        self.assertIn("snapshot fixture unavailable", window.details_dialog.snapshot_label.text())
        self.assertTrue(window.history_details_button.isEnabled())
        self.assertTrue(window.details_dialog.refresh_button.isEnabled())
        self.assertTrue(main_worker.isRunning())
        self.assertFalse(download_cancel[0]())
        self.assertIsNone(window.last_result)
        self.assertEqual(window.history_status.text(), main_status)

        details_entered = threading.Event()
        details_cancelled = threading.Event()

        def inspect(*args, **kwargs):
            details_entered.set()
            while not kwargs["cancel"]():
                time.sleep(0.01)
            details_cancelled.set()
            return {"status": "cancelled", "resources": []}

        self.inspect.side_effect = inspect
        window.history_details_button.click()
        self.wait_for_event(details_entered)
        window.details_dialog.close()
        self.wait_for_details()
        self.assertTrue(details_cancelled.is_set())
        self.assertTrue(main_worker.isRunning())
        self.assertFalse(download_cancel[0]())
        self.assertEqual(window.history_status.text(), main_status)
        self.assertFalse(window.library_download_button.isEnabled())

        details_entered.clear()
        details_cancelled.clear()
        window.history_details_button.click()
        self.wait_for_event(details_entered)
        QTest.keyClick(window.details_dialog, Qt.Key.Key_Escape)
        self.wait_for_details()
        self.assertTrue(details_cancelled.is_set())
        self.assertTrue(main_worker.isRunning())
        self.assertFalse(download_cancel[0]())

    def test_details_paginate_resources_and_reject_missing_library(self):
        self.details_result["resources"] = [dict(self.details_result["resources"][0], resource_id=f"resource-{i}") for i in range(205)]
        window = self.window
        window.history_details_button.click()
        self.wait_for_details()
        dialog = window.details_dialog
        self.assertEqual(dialog.table.rowCount(), 200)
        self.assertIn("第 1/2 页", dialog.page_label.text())
        dialog.next.click()
        self.assertEqual(dialog.table.rowCount(), 5)
        self.assertIn("resource-200", dialog.detail.toPlainText())
        dialog.previous.click()
        self.assertEqual(dialog.table.rowCount(), 200)
        window.output_edit.setText(str(self.root / "absent"))
        window.history_details_button.click()
        window._error.assert_called_once()
        self.assertEqual(self.inspect.call_count, 1)

    def other_library(self):
        output = self.root / "other-library"
        output.mkdir()
        with closing(sqlite3.connect(output / "reader.sqlite")) as db:
            db.executescript("CREATE TABLE accounts(uid TEXT,name TEXT);"
                             "CREATE TABLE conversations(uid TEXT,message_count INTEGER);"
                             "CREATE TABLE library_accounts(uid TEXT,app TEXT);"
                             "INSERT INTO accounts VALUES('fixture','新资料库账号');"
                             "INSERT INTO conversations VALUES('fixture',4);"
                             "INSERT INTO library_accounts VALUES('fixture','duoshan');")
        return output

    def managed_reader(self):
        reader = Mock()
        reader.poll.return_value = None
        self.window.reader_process = reader
        self.window.reader_output = self.output
        self.window.reader_port = 54321
        self.window.reader_url = "http://127.0.0.1:54321/"
        self.window.reader_ready = True
        return reader

    def test_library_picker_switches_summary_and_reader_on_owned_process_only(self):
        window = self.window
        other = self.other_library()
        old = self.managed_reader()
        new = Mock()
        new.poll.return_value = None
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(other)), \
             patch.object(gui.shutil, "which", return_value="fixture-node"), \
             patch.object(gui.subprocess, "Popen", return_value=new) as popen, \
             patch.object(QDesktopServices, "openUrl") as opener:
            window.library_choose_button.click()
            old.terminate.assert_called_once()
            old.kill.assert_not_called()
            popen.assert_not_called()
            self.assertEqual(window.library_path.text(), str(other))
            self.assertEqual(window.account_table.item(0, 0).text(), "多闪/抖音")
            self.assertEqual(window.account_table.item(0, 1).text(), "新资料库账号")
            self.assertFalse(any(label.text() == "目录说明" for label in window.findChildren(QLabel)))
            old.poll.return_value = 0
            window._check_reader_switch()
            self.assertEqual(window.reader_output, other)
            self.assertEqual(window.reader_port, 54321)
            args = popen.call_args.args[0]
            self.assertEqual(args[args.index("--data") + 1], str(other))
            self.assertEqual(args[args.index("--port") + 1], "54321")
            self.assertNotIn("--reindex", args)
            self.assertEqual(popen.call_args.kwargs["creationflags"], gui.subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            with patch("urllib.request.urlopen") as ready:
                ready.return_value.__enter__.return_value.status = 200
                window._check_reader()
            opener.assert_called_once()
            self.assertEqual(opener.call_args.args[0].toString(), "http://127.0.0.1:54321/")
            self.assertTrue(window.reader_ready)
            new.terminate.assert_not_called()

    def test_library_selection_without_managed_reader_and_invalid_selection(self):
        window = self.window
        other = self.other_library()
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(other)), \
             patch.object(gui.subprocess, "Popen") as popen:
            window.library_choose_button.click()
            self.assertEqual(Path(window.output_edit.text()), other)
            self.assertIsNone(window.reader_process)
            popen.assert_not_called()
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(self.root / "missing")):
            window.library_choose_button.click()
            self.assertEqual(Path(window.output_edit.text()), other)
            window._error.assert_called_once()

    def test_reader_reload_same_library_waits_and_reports_stop_failure(self):
        window = self.window
        reader = self.managed_reader()
        window.work_edit.clear()
        with patch.object(gui.shutil, "which", return_value="fixture-node"), \
             patch.object(gui.subprocess, "Popen") as popen:
            window.reader_reload_button.click()
            reader.terminate.assert_called_once()
            self.assertTrue(window.reader_switch_timer.isActive())
            window._set_busy(False)
            self.assertFalse(window.reader_button.isEnabled())
            self.assertFalse(window.reader_reload_button.isEnabled())
            self.assertEqual(window.reader_pending_options["output"], self.output)
            window.reader_switch_started = time.monotonic() - 11
            window._check_reader_switch()
            self.assertFalse(window.reader_switch_timer.isActive())
            self.assertIn("未完成切换", window.reader_status.text())
            self.assertEqual(window.reader_output, self.output)
            self.assertTrue(window.reader_button.isEnabled())
            reader.kill.assert_not_called()
            popen.assert_not_called()

    def test_manual_path_switch_can_retarget_pending_restart_without_extra_stop(self):
        window = self.window
        other = self.other_library()
        reader = self.managed_reader()
        with patch.object(gui.shutil, "which", return_value="fixture-node"), \
             patch.object(gui.subprocess, "Popen") as popen:
            window.output_edit.setText(str(other))
            window.output_edit.editingFinished.emit()
            self.assertEqual(window.reader_pending_options["output"], other)
            window.output_edit.setText(str(self.output))
            window.output_edit.editingFinished.emit()
            self.assertEqual(window.reader_pending_options["output"], self.output)
            reader.terminate.assert_called_once()
            popen.assert_not_called()
        window.reader_switch_timer.stop()
        window.reader_pending_options = None

    def test_reader_ready_during_download_keeps_reader_controls_disabled(self):
        window = self.window
        reader = self.managed_reader()
        window.reader_log_path = self.root / "synthetic-reader.log"
        window.reader_ready = False
        worker = Mock()
        worker.isRunning.return_value = False
        window.worker = worker
        window._set_busy(True)
        try:
            with patch("urllib.request.urlopen") as ready, patch.object(QDesktopServices, "openUrl"):
                ready.return_value.__enter__.return_value.status = 200
                window._check_reader()
            self.assertFalse(window.reader_button.isEnabled())
            self.assertFalse(window.reader_reload_button.isEnabled())
            window.worker = None
            window.reader_timer.start()
            window._set_busy(False)
            self.assertFalse(window.reader_button.isEnabled())
            self.assertFalse(window.reader_reload_button.isEnabled())
            window.worker = worker
            reader.poll.return_value = 1
            window._check_reader()
            self.assertFalse(window.reader_button.isEnabled())
        finally:
            window.worker = None

    def test_reload_replaces_a_previous_forbidden_port(self):
        window = self.window
        reader = self.managed_reader()
        window.reader_port = 64967
        with patch.object(gui.shutil, "which", return_value="fixture-node"), \
             patch.object(gui, "available_reader_port", return_value=65067), \
             patch.object(gui.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
            window.reader_reload_button.click()
            reader.poll.return_value = 0
            window._check_reader_switch()
            args = popen.call_args.args[0]
            self.assertEqual(args[args.index("--port") + 1], "65067")
            self.assertEqual(window.reader_port, 65067)

    def test_close_waits_for_owned_reader_and_never_stops_unrelated_process(self):
        window = self.window
        reader = self.managed_reader()
        unrelated = Mock()
        window.show()
        window.reader_pending_options = {"output": self.output}
        window.reader_switch_timer.start()
        window.close()
        reader.terminate.assert_called_once()
        self.assertTrue(window.close_requested)
        self.assertTrue(window.reader_close_timer.isActive())
        self.assertFalse(window.reader_switch_timer.isActive())
        self.assertIsNone(window.reader_pending_options)
        self.assertTrue(window.isVisible())
        self.assertFalse(window.reader_button.isEnabled())
        self.assertFalse(window.reader_reload_button.isEnabled())
        self.assertFalse(window.library_choose_button.isEnabled())
        self.assertFalse(window.library_download_button.isEnabled())
        window._sync_reader_controls()
        self.assertFalse(window.reader_button.isEnabled())
        with patch.object(gui.subprocess, "Popen") as popen, patch.object(QFileDialog, "getExistingDirectory") as choose:
            window._open_reader()
            window._choose_library()
            window._start("download")
            popen.assert_not_called()
            choose.assert_not_called()
            self.download.assert_not_called()
        reader.poll.return_value = 0
        window._check_reader_close()
        QTest.qWait(20)
        self.assertFalse(window.isVisible())
        self.assertFalse(window.reader_close_timer.isActive())
        unrelated.terminate.assert_not_called()
        unrelated.kill.assert_not_called()
        reader.kill.assert_not_called()

    def test_close_only_escalates_same_reader_handle_and_reports_stop_error(self):
        window = self.window
        reader = self.managed_reader()
        window.close()
        window.reader_close_started = time.monotonic() - 11
        window._check_reader_close()
        reader.kill.assert_called_once()
        self.assertTrue(window.reader_close_forced)
        window.reader_close_started = time.monotonic() - 11
        window._check_reader_close()
        window._error.assert_called_once()
        self.assertFalse(window.close_requested)
        self.assertFalse(window.reader_close_timer.isActive())
        reader.poll.return_value = 0

    def test_close_cancels_worker_then_finishes_without_second_close_click(self):
        window = self.window
        entered = threading.Event()

        def download(*args, **kwargs):
            entered.set()
            while not kwargs["cancel"]():
                time.sleep(0.01)
            return {"status": "cancelled", "totals": {}}

        self.download.side_effect = download
        window.library_download_check.setChecked(True)
        window.library_download_button.click()
        self.wait_for_event(entered)
        window.show()
        window.close()
        self.wait_for_worker()
        QTest.qWait(20)
        self.assertFalse(window.isVisible())
        self.assertEqual(window.last_result["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
