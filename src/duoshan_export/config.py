"""Explicit per-run configuration shared by extraction components."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ExportError


@dataclass(frozen=True)
class ExportConfig:
    source: Path
    output: Path
    work: Path
    accounts: tuple[str, ...] = ()
    timezone: str = "Asia/Shanghai"
    page_size: int = 600
    download: bool = False
    workers: int = 6
    sqlcipher_module_path: Path | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    resume: bool = False
    app: str = "auto"

    def __post_init__(self) -> None:
        for name in ("source", "output", "work"):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser().resolve())
        if self.sqlcipher_module_path is not None:
            object.__setattr__(self, "sqlcipher_module_path", Path(self.sqlcipher_module_path).expanduser().resolve())
        object.__setattr__(self, "accounts", tuple(dict.fromkeys(str(uid) for uid in self.accounts)))
        if self.app == "xhs":
            # Xiaohongshu account ids are lowercase hex (e.g. 68cf0e5d...).
            if any(not uid.isascii() or uid.lower() != uid
                   or any(ch not in "0123456789abcdef" for ch in uid)
                   for uid in self.accounts):
                raise ExportError("Xiaohongshu account IDs must be lowercase hex strings.")
        elif any(not uid.isascii() or not uid.isdecimal() for uid in self.accounts):
            raise ExportError("Account IDs must contain ASCII digits only.")
        if not 1 <= self.page_size <= 5000:
            raise ExportError("Page size must be between 1 and 5000 messages.")
        if not 1 <= self.workers <= 32:
            raise ExportError("Worker count must be between 1 and 32.")
        self.tzinfo

    @property
    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ExportError(
                f"Unknown timezone: {self.timezone!r}. Use an IANA timezone such as Asia/Shanghai or UTC; "
                "install tzdata if timezone data is missing."
            ) from error
