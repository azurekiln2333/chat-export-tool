"""Output ownership, source identity and atomic run state."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .config import ExportConfig
from .errors import ExportError

STATE_FILE = "export_state.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def source_signature(source: Path) -> str:
    """Fingerprint database/archive content and the selected source location."""
    source = source.resolve()
    if not source.exists():
        raise ExportError(f"Source path does not exist: {source}")
    digest = hashlib.sha256(str(source).encode("utf-8"))
    files = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.is_file())
    for path in files:
        relative = path.name if source.is_file() else path.relative_to(source).as_posix()
        info = path.stat()
        digest.update(relative.encode("utf-8"))
        digest.update(f"\0{info.st_size}\0{info.st_mtime_ns}\0".encode("ascii"))
        if (
            source.is_file()
            or "databases" in path.parts
            or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".zst", ".tar", ".tgz", ".gz"}
            or path.name.endswith(("-wal", "-journal"))
        ):
            digest.update(file_sha256(path).encode("ascii"))
    return digest.hexdigest()


def check_locations(config: ExportConfig) -> None:
    source_root = config.source
    for destination in (config.output, config.work):
        if destination == source_root or destination.is_relative_to(source_root):
            raise ExportError("Output and work directories must be outside the source backup directory.")
        if source_root.is_relative_to(destination):
            raise ExportError("Output and work directories cannot contain the source backup directory.")
    if config.work == config.output:
        raise ExportError("The work directory must differ from the output directory.")
    if config.output.is_relative_to(config.work):
        raise ExportError("The work directory cannot contain the output directory; place it inside the output or beside it.")


def begin_export(config: ExportConfig) -> dict:
    check_locations(config)
    signature = source_signature(config.source)
    state_path = config.output / STATE_FILE
    existing = list(config.output.iterdir()) if config.output.is_dir() else []
    if existing:
        if not config.resume:
            raise ExportError("Output directory is not empty. Choose a new directory or use --resume for the same backup.")
        if not state_path.is_file():
            raise ExportError("This output was not created by this tool. Choose a new output directory.")
        state = load_state(config.output)
        if state.get("source_signature") != signature:
            raise ExportError("The source backup has changed or differs from this export. Choose a new output directory.")
        if tuple(state.get("accounts", [])) != config.accounts:
            raise ExportError("The account selection differs from this export. Resume with the same --account values or choose a new output directory.")
    elif config.output.exists() and not config.output.is_dir():
        raise ExportError("Output path is a file. Choose a directory.")
    config.output.mkdir(parents=True, exist_ok=True)
    config.work.mkdir(parents=True, exist_ok=True)
    state = {
        "format_version": 1,
        "status": "in_progress",
        "source": str(config.source),
        "source_signature": signature,
        "accounts": list(config.accounts),
        "timezone": config.timezone,
        "page_size": config.page_size,
        "work": str(config.work),
    }
    write_json(state_path, state)
    return state


def load_state(output: Path) -> dict:
    path = output / STATE_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("format_version") != 1:
            raise ValueError("Unsupported export state")
        return value
    except (OSError, ValueError) as error:
        raise ExportError(f"Could not read tool export state at {path}. Run an export first.") from error
