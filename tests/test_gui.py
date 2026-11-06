"""Desktop contracts; optional Qt tests never contact a real media service."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from duoshan_export import gui

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
    def test_source_labels_match_explicit_legacy_aliases(self):
        for value in ("WeChat", "com.tencent.mm", " 微信 "):
            self.assertEqual(gui._source_app(value), "wechat")
        for value in ("Xiaohongshu", "com.xingin.xhs", "小红书", "xhs"):
            self.assertEqual(gui._source_app(value), "xhs")
        for value in ("Duoshan", "douyin", "my.maya.android", "多闪/抖音"):
            self.assertEqual(gui._source_app(value), "duoshan")
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
            with patch.dict(os.environ, {"LOCALAPPDATA": directory}):
                legacy = Path(directory) / "AzureKilnChatArchive/settings.json"
                legacy.parent.mkdir()
                old = {"output": "existing-library", "work": "existing-work"}
                legacy.write_text(json.dumps(old), encoding="utf-8")
                before = legacy.read_bytes()
                self.assertEqual(gui.read_settings(), old)
                current = gui.settings_path()
                self.assertEqual(current, Path(directory) / "ChatArchive/settings.json")
                self.assertFalse(current.exists())
                current.parent.mkdir()
                current.write_text(json.dumps({"output": "new-library"}), encoding="utf-8")
                self.assertEqual(gui.read_settings(), {"output": "new-library"})
                self.assertEqual(legacy.read_bytes(), before)

    def test_invalid_settings_can_fall_back_to_legacy(self):
        with tempfile.TemporaryDirectory(prefix="chat-gui-settings-") as directory:
            with patch.dict(os.environ, {"LOCALAPPDATA": directory}):
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
        environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "settings")})
        environment.start()
        self.addCleanup(environment.stop)
        backend = types.ModuleType("duoshan_export.library_media")
        self.download = Mock(return_value={"status": "completed", "totals": {
            "downloaded": 2, "existing": 3, "failed": 1, "unsupported": 4}})
        backend.download_library_media = self.download
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
        backend.inspect_library_media = self.inspect
        module = patch.dict(sys.modules, {"duoshan_export.library_media": backend})
        module.start()
        self.addCleanup(module.stop)
        self.window = gui.create_window(output=self.output, work=self.root / "work")
        self.window._error = Mock()

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
