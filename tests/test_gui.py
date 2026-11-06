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
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QLabel
    import qfluentwidgets  # Verify the optional GUI dependency before running controls.
    HAS_QT = True
except ImportError:
    HAS_QT = False


class SettingsTests(unittest.TestCase):
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
        module = patch.dict(sys.modules, {"duoshan_export.library_media": backend})
        module.start()
        self.addCleanup(module.stop)
        self.window = gui.create_window(output=self.output, work=self.root / "work")
        self.window._error = Mock()

    def tearDown(self):
        if self.window.worker is not None:
            self.window._cancel()
            self.wait_for_worker()
        self.window.close()
        self.app.processEvents()

    def wait_for_worker(self):
        deadline = time.monotonic() + 8
        while self.window.worker is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertIsNone(self.window.worker, "Desktop worker failed to finish")

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
        self.download.side_effect = RuntimeError("fixture unavailable")
        window.library_download_check.setChecked(True)
        window.library_download_button.click()
        self.wait_for_worker()
        self.assertEqual(window.last_result["status"], "error")
        self.assertIn("fixture unavailable", window.result_view.toPlainText())
        self.assertTrue(window.library_download_button.isEnabled())
        self.assertFalse(window.history_cancel_button.isEnabled())
        self.assertEqual(window.history_progress.value(), 0)

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


if __name__ == "__main__":
    unittest.main()
