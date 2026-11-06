"""Additive, portable multi-app library orchestration.

The legacy adapters keep their original contracts. Each new source is exported
in an isolated run directory, then handed to the reader's transactional merger.
Sources are read-only, successful media are content-addressed, and work/history
files are retained. Nothing here performs automatic cleanup or removes records.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from typing import Callable, Iterable
from uuid import uuid4

from .errors import ExportError
from .runtime import file_sha256

PACKAGES = {"com.tencent.mm": "wechat", "my.maya.android": "duoshan", "com.xingin.xhs": "xhs"}
LABELS = {"wechat": "微信", "duoshan": "多闪", "xhs": "小红书"}
PREFIXES = {"wechat": "wx", "duoshan": "dy", "xhs": "xhs"}
ACCOUNT_NAME = re.compile(r"^(?:(?:wx|xhs|dy)_)?(?:[0-9]+|[0-9a-f]{16,64})$")
ARCHIVE_SUFFIXES = (".tar.zst", ".tar.gz", ".tar", ".tgz", ".tzst", ".zst")
HISTORY = "import_history.jsonl"
IMPORT_VERSION = 1


def _is_archive(path: Path) -> bool:
    return path.is_file() and path.name.lower().endswith(ARCHIVE_SUFFIXES)


def _archive_package(path: Path) -> str | None:
    """Read only a bounded archive prefix to identify the app, never extract."""
    from .source import _open_tar
    try:
        with _open_tar(path) as archive:
            for index, member in enumerate(archive):
                for part in member.name.replace("\\", "/").split("/"):
                    if part in PACKAGES:
                        return part
                # Don't stream unrelated large files just to discover a label.
                if index >= 128 or member.size > 16 * 1024 * 1024:
                    break
    except (OSError, ValueError) as error:
        raise ExportError(f"Cannot inspect backup archive {path.name}: {error}") from error
    return None


def _package_hint(path: Path) -> str | None:
    for part in reversed(path.parts):
        if part in PACKAGES:
            return part
    return None


def _directory_package(path: Path) -> str | None:
    if (path / "MicroMsg").is_dir():
        return "com.tencent.mm"
    databases = path if path.name == "databases" else path / "databases"
    if databases.is_dir():
        names = {p.name for p in databases.iterdir() if p.is_file()}
        if names & {"msgDB", "localRelationDB", "hedwig.db"}:
            return "com.xingin.xhs"
        if any(re.match(r"(?:encrypted_)?(?:\d{6,}_im|im_\d{6,})(?:\.db)?$", n) for n in names):
            return "my.maya.android"
    return None


def discover_sources(paths: Iterable[str | os.PathLike]) -> list[dict]:
    """Discover individual app/user backups from files, packages or parent folders.

    A package root with user_0/user_999 is split before export, preventing archive
    basename collisions. Re-selecting both a parent and one child deduplicates it.
    Unknown selected paths fail explicitly instead of quietly importing nothing.
    """
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    found: dict[tuple[str, str], dict] = {}

    def add(path: Path, package: str, *, exported=False):
        key = (PACKAGES[package], os.path.normcase(str(path)))
        found[key] = {"app": PACKAGES[package], "package": package,
                      "source": str(path), "label": LABELS[PACKAGES[package]] + " · " + path.name,
                      "kind": "export" if exported else ("archive" if path.is_file() else "backup")}

    def walk(path: Path, depth: int):
        if path.is_symlink():
            raise ExportError(f"Select the actual backup directory, not a symbolic link: {path}")
        if path.is_file():
            hint = _package_hint(path)
            package = _archive_package(path) if _is_archive(path) else None
            if not package:
                package = ("com.tencent.mm" if path.name.startswith("EnMicroMsg") else
                           "com.xingin.xhs" if path.name == "msgDB" else
                           "my.maya.android" if re.fullmatch(r"(?:encrypted_)?(?:\d{6,}_im|im_\d{6,})(?:\.db)?", path.name) else hint)
            if package:
                add(path, package)
            return
        children = sorted(path.iterdir(), key=lambda p: p.name)
        users = [p for p in children if p.is_dir() and re.fullmatch(r"user_\d+", p.name)]
        if users:
            for user in users:
                walk(user, depth + 1)
            return
        package = _directory_package(path)
        if package:
            add(path, package)
            return
        # Already exported app trees are also accepted as an interchange format.
        summary = path / "export_summary.json"
        if summary.is_file() and any(p.is_dir() and (p / "messages.jsonl").is_file() for p in children):
            label = str(_read_json(summary, {}).get("app", "")).lower()
            app = next((a for a, aliases in {"wechat": ("wechat", "微信"),
                        "xhs": ("xiaohongshu", "xhs", "小红书"),
                        "duoshan": ("duoshan", "多闪")}.items() if any(s == label for s in aliases)), None)
            if app:
                add(path, next(k for k, v in PACKAGES.items() if v == app), exported=True)
                return
        archives = [p for p in children if _is_archive(p) and not p.name.lower().startswith(("apk.", "obb."))]
        if archives:
            package = _package_hint(path) or _archive_package(archives[0])
            if package:
                add(path, package)
                return
        if depth < 4:
            for child in children:
                if child.is_dir() and not child.name.startswith(".") and child.name not in {
                    "attachments", "node_modules", "media", "files", "cache", "databases", "MicroMsg",
                }:
                    walk(child, depth + 1)

    for value in paths:
        path = Path(value).expanduser().resolve()
        if not path.exists():
            raise ExportError(f"Backup path does not exist: {path}")
        previous = set(found)
        walk(path, 0)
        if set(found) == previous and not any(Path(r["source"]) == path or Path(r["source"]).is_relative_to(path) for r in found.values()):
            raise ExportError(f"No supported WeChat, Duoshan or Xiaohongshu backup found: {path}")
    if not found:
        raise ExportError("Select at least one supported backup.")
    return sorted(found.values(), key=lambda item: (item["app"], item["source"]))


def _read_json(path: Path, fallback):
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ExportError(f"Invalid JSON file {path}: {error}") from error


def _check_cancel(cancel):
    if cancel and cancel():
        raise ExportError("Import cancelled between stages. Completed imports and retained work are preserved.")


def _check_locations(source: Path, output: Path | None, work: Path):
    for dest in (output, work):
        if dest is None:
            continue
        if dest == source or dest.is_relative_to(source) or source.is_relative_to(dest):
            raise ExportError(f"Source backup and destination must be separate: {source} / {dest}")
    if output and (work == output or output.is_relative_to(work) or work.is_relative_to(output)):
        raise ExportError("Work and library directories must be separate; choose a sibling work directory.")


def _validated_source_paths(paths, output: Path | None, work: Path) -> list[Path]:
    """Protect selected backup roots before discovery splits Android users.

    Selecting a nested database/archive or user directory still protects its
    enclosing Android package. Otherwise file selections protect their parent,
    which can contain key material and database sidecars.
    """
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    normalized = [Path(value).expanduser().resolve() for value in paths]
    for path in normalized:
        root = path.parent if path.is_file() else path
        package = next((ancestor for ancestor in (root, *root.parents)
                        if ancestor.name in PACKAGES), None)
        _check_locations(package or root, output, work)
    return normalized


def _new_run(work: Path, app: str) -> Path:
    run = work / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + app + "-" + uuid4().hex[:8])
    run.mkdir(parents=True, exist_ok=False)
    return run


def _source_signature(source: Path, progress: Callable[[str], None], cancel=None) -> str:
    digest = hashlib.sha256(f"library-source-v{IMPORT_VERSION}\n".encode())
    files = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.is_file())
    for index, path in enumerate(files):
        if path.name.lower().startswith(("apk.tar", "obb.tar")):
            continue
        _check_cancel(cancel)
        if path.is_symlink():
            raise ExportError(f"Source contains a symbolic link: {path}")
        relative = path.name if source.is_file() else path.relative_to(source).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(str(path.stat().st_size).encode() + b"\0")
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                h.update(block)
                _check_cancel(cancel)
        digest.update(h.hexdigest().encode() + b"\n")
        if index % 500 == 0 or path.stat().st_size > 100 * 1024 * 1024:
            progress(f"已校验来源文件 {index + 1}/{len(files)}：{path.name}")
    return digest.hexdigest()


@contextmanager
def _library_lock(output: Path):
    """OS lock with a retained marker file, so no cleanup/unlink is needed."""
    output.mkdir(parents=True, exist_ok=True)
    lock = (output / ".import.lock").open("a+b")
    acquired = False
    try:
        try:
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            raise ExportError("Another import is already using this library. Wait for it to finish.") from error
        yield
    finally:
        if acquired:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _append_history(output: Path, event: dict):
    with (output / HISTORY).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def import_history(output: str | os.PathLike) -> list[dict]:
    path = Path(output) / HISTORY
    if not path.is_file():
        return []
    result = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(value)
            except ValueError:
                # A process interrupted during an append may leave one partial line.
                continue
    return result


def _completed_source_ids(output: Path) -> set[str]:
    database = output / "reader.sqlite"
    if not database.is_file():
        return set()
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='import_batches'").fetchone():
            return set()
        return {row[0] for row in db.execute("SELECT source_id FROM import_batches WHERE projection_status='completed'")}
    finally:
        db.close()


class _ProgressStream(io.TextIOBase):
    def __init__(self, progress):
        self.progress = progress
        self.pending = ""

    def write(self, value):
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line:
                self.progress(line)
        return len(value)

    def flush(self):
        if self.pending:
            self.progress(self.pending)
            self.pending = ""


def inspect_sources(paths, work, *, progress=print, cancel=None, sqlcipher_module_path=None,
                    password=None, imei=None, uin=None) -> dict:
    work = Path(work).expanduser().resolve()
    paths = _validated_source_paths(paths, None, work)
    results = []
    for source in discover_sources(paths):
        _check_cancel(cancel)
        path = Path(source["source"])
        _check_locations(path, None, work)
        run = _new_run(work, source["app"] + "-inspect")
        progress("检查 " + source["label"])
        if source["kind"] == "export":
            accounts = []
            for directory in path.iterdir():
                messages = directory / "messages.jsonl"
                if directory.is_dir() and ACCOUNT_NAME.fullmatch(directory.name) and messages.is_file():
                    with messages.open("rb") as stream:
                        count = sum(1 for line in stream if line.strip())
                    accounts.append({"uid": directory.name.split("_", 1)[-1], "message_count": count})
            result = {"accounts": accounts}
        elif source["app"] == "wechat":
            from .apps.wechat import inspect_backup
            result = inspect_backup(path, run, progress=progress, cancel=cancel,
                                    sqlcipher_module_path=sqlcipher_module_path,
                                    password=password, imei=imei, uin=uin)
        else:
            from .cli import _inspect
            result = _inspect(SimpleNamespace(source=path, work_dir=run, app=source["app"],
                                              sqlcipher_module_path=sqlcipher_module_path))
        results.append({**source, **result, "work": str(run)})
    return {"status": "inspected", "sources": results}


def _export_source(source, output, work, *, timezone, download_media, progress, cancel,
                   sqlcipher_module_path, password, imei, uin, media_output):
    path = Path(source["source"])
    if source["kind"] == "export":
        return path
    if source["app"] == "wechat":
        from .apps.wechat import export_backup
        export_backup(path, output, work, timezone=timezone, progress=progress, cancel=cancel,
                      sqlcipher_module_path=sqlcipher_module_path, password=password,
                      imei=imei, uin=uin, media_output=media_output)
    else:
        from contextlib import redirect_stdout
        from .cli import _export
        logger = _ProgressStream(progress)
        # Fresh stages keep legacy export ownership independent of the library.
        # Snapshot/decryption failures retain their incomplete artifacts.
        with redirect_stdout(logger):
            _export(SimpleNamespace(source=path, output=output, work_dir=work, app=source["app"],
                account=[], timezone=timezone, page_size=600, resume=False, download_media=download_media,
                reuse_media=None, decode_media=False, workers=6, sqlcipher_module_path=sqlcipher_module_path,
                ffmpeg="ffmpeg", ffprobe="ffprobe"))
        logger.flush()
    return output


def _contained_file(root: Path, relative: str) -> Path:
    parts = relative.replace("\\", "/").split("/")
    if not parts or parts[0] != "attachments" or any(p in {"", ".", ".."} or ":" in p for p in parts):
        raise ExportError(f"Unsafe media path: {relative}")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ExportError(f"Media path escapes its export: {relative}")
    return path


def _rewrite_paths(value, mapping):
    if isinstance(value, str):
        return mapping.get(value.replace("\\", "/"), value)
    if isinstance(value, list):
        return [_rewrite_paths(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_paths(item, mapping) for key, item in value.items()}
    return value


def _media_references(value):
    if isinstance(value, str) and value.replace("\\", "/").startswith("attachments/"):
        yield value.replace("\\", "/")
    elif isinstance(value, dict):
        for item in value.values():
            yield from _media_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _media_references(item)


def _copy_asset(source: Path, library: Path, app: str, mapping: dict, relative: str):
    path = _contained_file(source, relative)
    if not path.is_file():
        # The WeChat adapter can stream directly to the destination to avoid
        # a second 60+ GB media tree. Its paths already identify final assets.
        target = _contained_file(library, relative)
        if not target.is_file():
            raise ExportError(f"Export references missing media: {relative}")
        mapping[relative] = relative
        return
    digest = file_sha256(path)
    extension = path.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", extension):
        extension = ".bin"
    relative_out = f"attachments/{app}/{digest[:2]}/{digest}{extension}"
    target = _contained_file(library, relative_out)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file() or file_sha256(target) != digest:
            raise ExportError(f"Existing content-addressed media failed hash verification: {target}")
    else:
        # An interrupted copy is kept with a unique partial filename. The final
        # immutable path appears only after size/hash verification succeeds.
        partial = target.with_name(target.name + ".partial-" + uuid4().hex[:8])
        with path.open("rb") as inp, partial.open("xb") as out:
            shutil.copyfileobj(inp, out, 4 * 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        if file_sha256(partial) != digest:
            raise ExportError(f"Media copy verification failed: {relative}")
        partial.rename(target)
    mapping[relative] = relative_out


def _prepare_reader_stage(source: Path, destination: Path, library: Path, app: str, progress, cancel=None):
    destination.mkdir(parents=True, exist_ok=False)
    mapping = {}
    attached = source / "attachments"
    if attached.is_dir():
        for index, path in enumerate(attached.rglob("*")):
            if path.is_file():
                _check_cancel(cancel)
                _copy_asset(source, library, app, mapping, path.relative_to(source).as_posix())
                if index % 500 == 0:
                    progress(f"已校验媒体文件：{len(mapping)}")
    manifests = {}
    for name in ("media_manifest.json", "decoration_manifest.json"):
        data = _read_json(source / name, {})
        for relative in _media_references(data):
            if relative not in mapping:
                # Failed resource records can legitimately name an absent path.
                if _contained_file(source, relative).is_file() or _contained_file(library, relative).is_file():
                    _copy_asset(source, library, app, mapping, relative)
        manifests[name] = _rewrite_paths(data, mapping)
        (destination / name).write_text(json.dumps(manifests[name], ensure_ascii=False), encoding="utf-8")
    for directory in sorted(source.iterdir()):
        messages = directory / "messages.jsonl"
        if not directory.is_dir() or not ACCOUNT_NAME.fullmatch(directory.name) or not messages.is_file():
            continue
        target = destination / directory.name
        target.mkdir()
        for name in ("statistics.json", "contacts.json"):
            if (directory / name).is_file():
                shutil.copyfile(directory / name, target / name)
        with messages.open(encoding="utf-8-sig") as inp, (target / "messages.jsonl").open("w", encoding="utf-8", newline="\n") as out:
            for index, line in enumerate(inp):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ExportError(f"Invalid message object in {messages.name}, line {index + 1}")
                meta = row.get("_export", {})
                if isinstance(meta, dict):
                    for relative in _media_references(meta):
                        if relative not in mapping:
                            _copy_asset(source, library, app, mapping, relative)
                    row["_export"] = _rewrite_paths(meta, mapping)
                out.write(json.dumps(row, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029") + "\n")
                if index % 25000 == 0:
                    _check_cancel(cancel)
        progress(f"已准备账号：{directory.name}")
    summary = _read_json(source / "export_summary.json", {})
    (destination / "export_summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    return manifests, {"files": len(mapping)}


def _merge_manifest(old, incoming):
    """Preserve usable old assets when a newer offline backup has no copy."""
    if isinstance(old, dict) and isinstance(incoming, dict):
        if old.get("status") == "ok" and incoming.get("status") not in (None, "ok"):
            return old
        result = dict(old)
        for key, value in incoming.items():
            result[key] = _merge_manifest(old[key], value) if key in old else value
        return result
    if isinstance(old, list) and isinstance(incoming, list):
        seen = {json.dumps(value, sort_keys=True, ensure_ascii=True) for value in old}
        return old + [value for value in incoming if json.dumps(value, sort_keys=True, ensure_ascii=True) not in seen]
    return incoming if incoming not in (None, "") else old


def _retain_and_write(path: Path, value, history: Path):
    history.mkdir(parents=True, exist_ok=True)
    prepared = history / (path.name + ".prepared")
    with prepared.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    # Retain the old file with a rename; never unlink it or truncate it in place.
    old = history / path.name
    if path.is_file():
        path.rename(old)
    try:
        prepared.rename(path)
    except OSError:
        if old.exists() and not path.exists():
            old.rename(path)
        raise


def _node_command(node: str) -> tuple[str, Path]:
    executable = shutil.which(node)
    if not executable:
        raise ExportError("Node.js is required to update the reader library. Install Node.js 24.15+ or select its executable.")
    script = Path(__file__).resolve().parents[2] / "web/server/library-import.mjs"
    if not script.is_file():
        raise ExportError(f"Reader import service not found: {script}. Run from the complete desktop tool folder.")
    return executable, script


def _run_reader(node, arguments, progress):
    executable, script = _node_command(node)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    completed = subprocess.run([executable, str(script), *arguments, "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **options)
    if completed.stderr.strip():
        progress(completed.stderr.strip())
    if completed.returncode:
        raise ExportError("Reader import failed: " + (completed.stderr.strip() or completed.stdout.strip()))
    try:
        return json.loads(completed.stdout)
    except ValueError as error:
        raise ExportError("Reader import returned an invalid result: " + completed.stdout[:500]) from error


def organize_library(output, *, node="node", progress=print):
    output = Path(output).expanduser().resolve()
    if not (output / "reader.sqlite").is_file():
        raise ExportError("Choose an existing reader library containing reader.sqlite.")
    with _library_lock(output):
        return _run_reader(node, ["--output", str(output), "--organize"], progress)


def import_sources(paths, output, work, *, timezone="Asia/Shanghai", progress=print, cancel=None,
                   download_media=False, sqlcipher_module_path=None, node="node",
                   password=None, imei=None, uin=None) -> dict:
    output, work = Path(output).expanduser().resolve(), Path(work).expanduser().resolve()
    paths = _validated_source_paths(paths, output, work)
    sources = discover_sources(paths)
    _node_command(node)
    summary = _read_json(output / "export_summary.json", {})
    old_timezone = summary.get("timezone")
    if old_timezone and old_timezone != timezone:
        raise ExportError(f"Library timezone is {old_timezone}; use it for this import to keep calendar days consistent.")
    for source in sources:
        _check_locations(Path(source["source"]), output, work)
    results = []
    with _library_lock(output):
        completed_sources = _completed_source_ids(output)
        for source in sources:
            _check_cancel(cancel)
            path = Path(source["source"])
            progress("校验备份内容：" + source["label"])
            signature = _source_signature(path, progress, cancel)
            policy = f"{timezone}|download={int(download_media)}"
            source_id = source["app"] + ":" + hashlib.sha256((signature + policy).encode()).hexdigest()
            if source_id in completed_sources and (output / "reader.sqlite").is_file():
                results.append({**source, "status": "unchanged", "source_id": source_id})
                progress("此备份已导入，跳过重复处理：" + source["label"])
                continue
            run = _new_run(work, source["app"])
            event = {**source, "source_id": source_id, "work": str(run)}
            _append_history(output, {**event, "status": "started"})
            try:
                progress("导出并解密：" + source["label"])
                exported = _export_source(source, run / "export", run / "work", timezone=timezone,
                    download_media=download_media, progress=progress, cancel=cancel,
                    sqlcipher_module_path=sqlcipher_module_path, password=password,
                    imei=imei, uin=uin, media_output=output)
                _check_cancel(cancel)
                manifests, media = _prepare_reader_stage(exported, run / "reader", output, source["app"], progress, cancel)
                _check_cancel(cancel)
                retained = output / ".import-history" / run.name
                previous_metadata = {name: (output / name).exists() for name in
                                     (*manifests, "export_summary.json")}
                # Validate the whole metadata set before changing any live file.
                prepared_metadata = {name: _merge_manifest(_read_json(output / name, {}), data)
                                     for name, data in manifests.items()}
                summary = _read_json(output / "export_summary.json", {})
                apps = set(summary.get("apps", []))
                old_label = str(summary.get("app", "")).lower()
                for app, label in {"wechat": "wechat", "xhs": "xiaohongshu", "duoshan": "duoshan"}.items():
                    if label in old_label:
                        apps.add(app)
                apps.add(source["app"])
                summary.update(app=", ".join({"wechat": "WeChat", "xhs": "Xiaohongshu", "duoshan": "Duoshan"}[a] for a in sorted(apps)),
                               apps=sorted(apps), timezone=timezone, library_format=IMPORT_VERSION)
                prepared_metadata["export_summary.json"] = summary
                changed_metadata = []
                reader_started = False
                try:
                    for name, data in prepared_metadata.items():
                        _retain_and_write(output / name, data, retained)
                        changed_metadata.append(name)
                    progress("正在合并到阅读器；此阶段完成后才能取消。")
                    reader_started = True
                    result = _run_reader(node, ["--source", str(run / "reader"), "--output", str(output),
                                               "--app", source["app"], "--source-id", source_id], progress)
                except Exception:
                    # If the DB transaction failed, restore root metadata. If a
                    # projection failed after commit, keep media/summary for the
                    # committed messages and let the next attempt repair it.
                    db = sqlite3.connect((output / "reader.sqlite").as_uri() + "?mode=ro", uri=True) if reader_started and (output / "reader.sqlite").exists() else None
                    try:
                        committed = bool(db and db.execute("SELECT 1 FROM sqlite_master WHERE name='import_batches'").fetchone()
                                         and db.execute("SELECT 1 FROM import_batches WHERE source_id=?", (source_id,)).fetchone())
                    finally:
                        if db:
                            db.close()
                    if not committed:
                        for name in reversed(changed_metadata):
                            existed = previous_metadata[name]
                            current = output / name
                            if current.exists():
                                current.rename(retained / (name + ".failed"))
                            old = retained / name
                            if existed and old.exists():
                                old.rename(current)
                    raise
                item = {**event, "status": "completed", "media": media, "result": result}
                _append_history(output, item)
                completed_sources.add(source_id)
                results.append(item)
                progress("导入完成：" + source["label"])
            except Exception as error:
                _append_history(output, {**event, "status": "failed", "error": str(error)})
                raise
    return {"status": "completed", "imports": results, "output": str(output),
            "reader_database": str(output / "reader.sqlite")}
