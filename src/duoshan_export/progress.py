"""Structured stage progress that remains compatible with text callbacks."""
from __future__ import annotations

import threading
import time

PROGRESS_INTERVAL = 5.0


class ProgressEvent(str):
    """A normal log string carrying truthful, optional stage-level counters."""

    def __new__(cls, text, *, stage: str, completed: int | None = None,
                total: int | None = None, unit: str = "个", state: str = "running"):
        if state not in {"running", "completed", "failed", "cancelled"}:
            raise ValueError(f"Unknown progress state: {state}")
        for label, value in (("completed", completed), ("total", total)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{label} must be a non-negative integer or None")
        value = super().__new__(cls, str(text))
        value.stage = str(stage)
        value.completed = completed
        value.total = total
        value.unit = str(unit)
        value.state = state
        return value


class StageReporter:
    """Bounded heartbeat for long disk operations; counters never imply an ETA."""

    def __init__(self, callback, stage, *, completed=None, total=None, unit="个", cancel=None):
        self.callback, self.stage = callback, stage
        self.completed, self.total, self.unit = completed, total, unit
        self.cancel = cancel
        self.detail = ""
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None
        self.error = None

    def __enter__(self):
        self.emit("开始")
        self.thread = threading.Thread(target=self._heartbeat, name="library-stage-progress", daemon=True)
        self.thread.start()
        return self

    def update(self, *, completed=None, total=None, detail=None):
        with self.lock:
            if completed is not None:
                self.completed = completed
            if total is not None:
                self.total = total
            if detail is not None:
                self.detail = detail
            error = self.error
        if error is not None:
            raise error

    def emit(self, status="进行中", state="running"):
        with self.lock:
            completed, total, detail = self.completed, self.total, self.detail
        parts = [f"{self.stage}：{status}"]
        if completed is not None:
            parts.append(f"已处理 {completed:,}" + (f"/{total:,}" if total is not None else "") + f" {self.unit}")
        if detail:
            parts.append(detail)
        parts.append(f"耗时 {time.monotonic() - self.started:.1f} 秒")
        self.callback(ProgressEvent("；".join(parts), stage=self.stage, completed=completed,
                                    total=total, unit=self.unit, state=state))

    def _heartbeat(self):
        while not self.stopped.wait(PROGRESS_INTERVAL):
            try:
                self.emit()
            except Exception as error:
                with self.lock:
                    self.error = error
                self.stopped.set()

    def __exit__(self, kind, value, traceback):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join()
        if kind is None:
            if self.error is not None:
                raise self.error
            self.emit("完成", "completed")
        else:
            try:
                cancelled = self.cancel and self.cancel()
                self.emit("已取消" if cancelled else "中断", "cancelled" if cancelled else "failed")
            except Exception:
                pass  # Never replace the original operation error.
        return False
