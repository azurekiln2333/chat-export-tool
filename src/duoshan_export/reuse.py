"""Import verified media from another export without runtime dependencies on it."""

from __future__ import annotations

import collections
import json
from pathlib import Path
import shutil

from .errors import ExportError
from .paths import ACCOUNT_FILES, LEGACY_NAMES, ROOT_FILES, english_relative_path
from .runtime import file_sha256, write_json


def _metadata_path(root: Path, name: str) -> Path:
    direct = root / name
    if direct.exists():
        return direct
    for legacy, current in LEGACY_NAMES.items():
        if current == name and (root / legacy).exists():
            return root / legacy
    return direct


def _resolve_asset(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative or ":" in relative:
        raise ExportError("Media manifest contains an unsafe local path.")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ExportError(f"Missing or unsafe media asset: {relative}")
    return path


def _copy_asset(old: Path, output: Path, record: dict, copied: set[str]) -> None:
    relative = record.get("path")
    if not relative:
        return
    source = _resolve_asset(old, relative)
    normalized = english_relative_path(relative)
    destination = (output / normalized).resolve()
    if not destination.is_relative_to(output.resolve()) or not normalized.isascii():
        raise ExportError("Imported media path must stay inside the output folder and use ASCII filenames.")
    record["path"] = normalized
    if normalized in copied:
        return
    expected_hash = record.get("sha256") or file_sha256(source)
    if record.get("bytes") is not None and source.stat().st_size != record["bytes"]:
        raise ExportError(f"Imported media size does not match its manifest: {relative}")
    if file_sha256(source) != expected_hash:
        raise ExportError(f"Imported media hash does not match its manifest: {relative}")
    if not destination.exists() or file_sha256(destination) != expected_hash:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Independent copies keep later recovery/conversion from changing the old export.
        shutil.copy2(source, destination)
    if file_sha256(destination) != expected_hash:
        raise ExportError(f"Copied media failed integrity verification: {relative}")
    copied.add(normalized)


def _raw_records(root: Path) -> dict:
    rows = {}
    for folder in root.iterdir():
        if not folder.is_dir() or not folder.name.isascii() or not folder.name.isdecimal():
            continue
        path = _metadata_path(folder, ACCOUNT_FILES["messages_jsonl"])
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            rows[folder.name + ":" + str(row["msg_uuid"])] = {
                name: row.get(name) for name in ("type", "content", "sender", "conversation_id")
            }
    return rows


def reuse_media(old: Path, output: Path) -> dict:
    old, output = old.expanduser().resolve(), output.resolve()
    if not old.is_dir() or old == output or old.is_relative_to(output) or output.is_relative_to(old):
        raise ExportError("--reuse-media must point to a separate existing export directory.")
    old_rows, new_rows = _raw_records(old), _raw_records(output)
    allowed = {key for key, row in new_rows.items() if old_rows.get(key) == row}
    copied: set[str] = set()
    manifest_path = _metadata_path(old, ROOT_FILES["media_manifest"])
    associations = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        associations = {key: dict(value) for key, value in manifest.get("messages", {}).items() if key in allowed}
        counts = collections.Counter(row.get("resource_id") for row in associations.values())
        resources = []
        for item in manifest.get("resources", []):
            if item.get("resource_id") not in counts:
                continue
            item = dict(item, message_count=counts[item["resource_id"]])
            if item.get("status") == "ok":
                _copy_asset(old, output, item, copied)
            resources.append(item)
        by_id = {item["resource_id"]: item for item in resources}
        for item in associations.values():
            if item.get("status") == "ok":
                _copy_asset(old, output, item, copied)
            item["message_count"] = counts[item.get("resource_id")]
            if item.get("resource_id") not in by_id:
                raise ExportError("Imported media association has no resource record.")
        statistics = {}
        for kind in ("voice", "emoji"):
            selected = [item for item in resources if item.get("kind") == kind]
            successful = [item for item in selected if item.get("status") == "ok"]
            statistics[kind] = {
                "unique_total": len(selected), "unique_downloaded": len(successful),
                "message_total": sum(item["message_count"] for item in selected),
                "messages_downloaded": sum(item["message_count"] for item in successful),
                "bytes": sum(item.get("bytes", 0) for item in successful),
                "animated_unique": sum(item.get("frames", 1) > 1 for item in successful),
            }
        write_json(output / ROOT_FILES["media_manifest"], {"resources": resources, "messages": associations, "statistics": statistics})
    decoration_path = _metadata_path(old, ROOT_FILES["decorations"])
    if decoration_path.is_file():
        decorations = json.loads(decoration_path.read_text(encoding="utf-8"))
        def copy_records(value):
            if isinstance(value, dict):
                if value.get("path"):
                    _copy_asset(old, output, value, copied)
                for child in value.values():
                    copy_records(child)
            elif isinstance(value, list):
                for child in value:
                    copy_records(child)
        copy_records(decorations)
        write_json(output / ROOT_FILES["decorations"], decorations)
    return {"copied_files": len(copied), "matching_messages": len(allowed), "media_associations": len(associations)}
