"""Observable stages retain real counts and stop reporting before returning."""
import sys
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from duoshan_export.progress import ProgressEvent, StageReporter


class StageProgressTests(unittest.TestCase):
    def test_blocked_work_reports_real_count_and_joins_on_success(self):
        messages = []
        heartbeat = threading.Event()

        def collect(message):
            messages.append(message)
            if "进行中" in message:
                heartbeat.set()

        with patch("duoshan_export.progress.PROGRESS_INTERVAL", 0.01):
            with StageReporter(collect, "读取", completed=0, total=4, unit="字节") as stage:
                stage.update(completed=2, detail="等待磁盘")
                self.assertTrue(heartbeat.wait(2))
                live = [message for message in messages if "进行中" in message][-1]
                self.assertEqual((live.completed, live.total, live.state), (2, 4, "running"))
                stage.update(completed=4)
            self.assertFalse(stage.thread.is_alive())
        self.assertEqual((messages[-1].completed, messages[-1].state), (4, "completed"))
        self.assertIsInstance(messages[-1], str)

    def test_cancellation_keeps_partial_count_and_original_error(self):
        messages = []
        with self.assertRaisesRegex(RuntimeError, "original cancellation"):
            with StageReporter(messages.append, "写入", completed=1, total=8, cancel=lambda: True) as stage:
                raise RuntimeError("original cancellation")
        self.assertFalse(stage.thread.is_alive())
        self.assertEqual((messages[-1].completed, messages[-1].state), (1, "cancelled"))

    def test_background_callback_failure_reaches_caller_and_stops(self):
        fired = threading.Event()

        def callback(event):
            if "进行中" in event:
                fired.set()
                raise RuntimeError("callback failure")

        with patch("duoshan_export.progress.PROGRESS_INTERVAL", 0.01):
            with self.assertRaisesRegex(RuntimeError, "callback failure"):
                with StageReporter(callback, "等待") as stage:
                    self.assertTrue(fired.wait(2))
        self.assertFalse(stage.thread.is_alive())

    def test_unknown_total_is_not_invented(self):
        messages = []
        with StageReporter(messages.append, "扫描", completed=0) as stage:
            stage.update(completed=7)
        self.assertEqual(messages[-1].completed, 7)
        self.assertIsNone(messages[-1].total)
        with self.assertRaises(ValueError):
            ProgressEvent("invalid", stage="stage", completed=-1)


if __name__ == "__main__":
    unittest.main()
