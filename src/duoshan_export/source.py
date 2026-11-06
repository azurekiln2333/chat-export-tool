"""Discover Duoshan app data and safely extract DataBackup archives.

The tool accepts whatever a user has: an extracted Android app-data root, a
DataBackup folder containing ``*.tar.zst`` archives, a ``databases`` folder, or
a path to a single encrypted/plain database file. This module locates the app
directory, database directory, account databases and the local media/decoration
folders, and extracts archives into a separate work directory.

Archives are treated as untrusted. Members with absolute or Windows-style
paths, ``..`` traversal, reserved device names, drive letters, symlinks/devices
or oversized payloads are skipped; nothing is ever written outside the work
directory and the original source tree is never modified.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile
from typing import Iterator
from uuid import uuid4

from .errors import ExportError

APP_PACKAGE = "my.maya.android"
SQLITE_HEADER = b"SQLite format 3\x00"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
ARCHIVE_SUFFIXES = (".tar.zst", ".tar", ".tgz", ".tar.gz", ".tzst", ".zst")

MAX_MEMBER_BYTES = 8 * 1024 ** 3
MAX_TOTAL_BYTES = 128 * 1024 ** 3
_MEMBER_CHUNK = 1 << 20
_FINGERPRINT_CHUNK = 1 << 20
_REPARSE_POINT = 0x400

# Archive stems that never contain chat data and can be skipped without reading.
_SKIP_ARCHIVE_STEMS = ("apk", "obb", "media")

_WINDOWS_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)
_DRIVE = re.compile(r"^[A-Za-z]:")

_MAIN_NAME = re.compile(r"^(?:encrypted_)?(?P<uid>\d{6,})_im(?:\.db)?$", re.IGNORECASE)
_MAIN_NAME_ALT = re.compile(r"^(?:encrypted_)?im_(?P<uid>\d{6,})(?:\.db)?$", re.IGNORECASE)
_CONTACTS_NAME = re.compile(r"^(?:relation_uc_v3_|relation_user_center_v2_)(?P<uid>\d{6,})(?:\.db)?$", re.IGNORECASE)
_PROFILE_NAME = re.compile(r"^(?P<uid>\d{6,})_aweme_user_info(?:\.sgv)?$", re.IGNORECASE)

_LAYOUT_HINT = (
    "Supported inputs are an extracted Android app-data folder, a DataBackup folder "
    "containing *.tar.zst archives, a 'databases' folder, or a single encrypted/plain "
    "Duoshan database file."
)

_VERSION_KEYS = ("last_app_version_name", "app_version_name", "version_name", "versionName")
_VERSION_RE = re.compile(r'name="(' + "|".join(_VERSION_KEYS) + r')"\s*>([^<]+)<')


@dataclass(frozen=True)
class SourceLayout:
    """Where the app data, databases and reusable media live on disk."""

    source: Path
    app_dir: Path | None
    databases_dir: Path | None
    work_dir: Path
    data_dir: Path | None = None
    extracted_dir: Path | None = None
    files_dir: Path | None = None
    keva_dir: Path | None = None
    profiles_dir: Path | None = None
    bubbles_path: Path | None = None
    im_dir: Path | None = None
    im_resources_dir: Path | None = None
    small_emoji_dir: Path | None = None
    archives: tuple[Path, ...] = field(default_factory=tuple)
    # Best-effort app version from the backup (never a compiled-in value).
    app_version: str | None = None

    @property
    def media_dirs(self) -> dict[str, Path]:
        """Return the local media folders that exist, keyed by a stable name."""
        candidates = {
            "im": self.im_dir,
            "im_resources": self.im_resources_dir,
            "small_emoji_res": self.small_emoji_dir,
        }
        return {name: path for name, path in candidates.items() if path is not None and path.is_dir()}

    @property
    def has_app_data(self) -> bool:
        return self.app_dir is not None and self.app_dir.is_dir()


@dataclass(frozen=True)
class AccountSource:
    """One Duoshan account and the databases required to read it."""

    uid: str
    database: Path
    contacts_database: Path | None = None
    # The keva user profile: normally the ``*.sgv`` payload inside
    # ``files/keva/repo/<uid>_aweme_user_info/``; may be a file or folder.
    profile_path: Path | None = None
    bubbles_path: Path | None = None


# --------------------------------------------------------------------------- #
# Archive handling
# --------------------------------------------------------------------------- #

def _load_zstandard():
    try:
        import zstandard  # type: ignore import-not-found
    except ImportError as error:
        raise ExportError(
            "Zstandard-compressed backups need the 'zstandard' package. "
            "Install it with 'pip install zstandard' (a declared dependency, usually installed "
            "with this tool) and try again."
        ) from error
    return zstandard


def _looks_like_archive_file(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(ARCHIVE_SUFFIXES):
        return True
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
            if head == ZSTD_MAGIC:
                return True
            handle.seek(257)
            return handle.read(5) == b"ustar"
    except OSError:
        return False


def _is_zstd(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == ZSTD_MAGIC
    except OSError:
        return False


@contextmanager
def _open_tar(path: Path):
    with path.open("rb") as raw:
        if _is_zstd(path):
            zstandard = _load_zstandard()
            with zstandard.ZstdDecompressor().stream_reader(raw) as stream:
                with tarfile.open(fileobj=stream, mode="r|") as archive:
                    yield archive
        else:
            with tarfile.open(fileobj=raw, mode="r:*") as archive:
                yield archive


def _safe_parts(name: str) -> list[str] | None:
    """Return safe relative path components, or ``None`` for an unsafe member."""
    if not name:
        return None
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        return None
    if _DRIVE.match(normalized):
        return None
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        if ":" in part:
            return None
        # Windows silently strips trailing dots/spaces, which could alias another file.
        if part.rstrip(" .") != part:
            return None
        if _WINDOWS_RESERVED.match(part):
            return None
        parts.append(part)
    return parts or None


def _is_unsafe_kind(member: tarfile.TarInfo) -> bool:
    return not (member.isfile() or member.isdir())


def _relevant_member(name: str, head: bytes) -> bool:
    normalized = name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return False
    if any(part == "__MACOSX" or part.startswith("._") for part in parts):
        return False
    if "databases" in parts:
        return True
    if "small_emoji_res" in parts:
        return True
    if "im_resources" in parts:
        return True
    if "files" in parts and "im" in parts:
        return True
    if "shared_prefs" in parts or "mmkv" in parts:
        return True
    if "keva" in parts and "repo" in parts:
        joined = "/".join(parts)
        if "_aweme_user_info" in joined or "bubble_detail_info" in parts:
            return True
    lowered = normalized.lower()
    suffix = Path(normalized).suffix.lower()
    if suffix in {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".db-journal"}:
        return True
    if lowered.endswith(("-wal", "-shm", "-journal")):
        return True
    return head.startswith(SQLITE_HEADER)


def _ensure_within(target: Path, root: Path) -> None:
    resolved_root = root.resolve()
    try:
        resolved_target = target.resolve()
    except OSError as error:  # pragma: no cover - defensive
        raise ExportError(f"Could not resolve an archive target: {target}") from error
    if not resolved_target.is_relative_to(resolved_root):
        raise ExportError(f"Refusing to extract outside the work directory: {target}")


def _is_link_or_reparse(path: Path) -> bool:
    """True for symbolic links and Windows reparse points (e.g. junctions)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _owned_marker(directory: Path) -> dict | None:
    """Return the parsed tool marker for ``directory``, or ``None`` when absent/invalid."""
    marker = directory / ".extracted.json"
    if not marker.is_file():
        return None
    try:
        meta = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or "fingerprint" not in meta or "archive" not in meta:
        return None
    return meta


def _ensure_owned_dir(path: Path, root: Path) -> None:
    """Raise unless ``path`` is a directory strictly inside the tool-owned ``root``."""
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise ExportError(
            f"Refusing to modify {path}: it is outside the tool-owned directory {root}"
        )


def _retain_owned_tree(path: Path, root: Path) -> None:
    """Retain an old extraction beside its replacement without deleting it."""
    if _is_link_or_reparse(path):
        raise ExportError(f"Refusing to replace a symlinked/reparse directory: {path}")
    if not path.exists():
        return
    _ensure_owned_dir(path, root)
    path.rename(path.with_name(path.name + ".retained-" + uuid4().hex[:12]))


def _archive_fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(path).encode("utf-8"))
    digest.update(f"|{stat.st_size}|{stat.st_mtime_ns}".encode("ascii"))
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(_FINGERPRINT_CHUNK), b""):
                digest.update(block)
    except OSError as error:
        raise ExportError(f"Cannot read archive for fingerprinting: {path}") from error
    return digest.hexdigest()


def extract_archive(archive: Path, destination: Path, *, relevant_only: bool = True,
                    root: Path | None = None) -> dict:
    """Safely extract a tar/tar.zst ``archive`` into ``destination``.

    When ``root`` is given, ``destination`` must live strictly inside it and all
    replacements are scoped to that tool-owned tree. An existing non-empty
    ``destination`` is only replaced when it carries a valid tool marker; an
    unrelated user directory or a symlinked/reparse target is refused rather than
    changed. Prior tool-owned extractions are retained by rename. Repeated calls with the same archive reuse the earlier extraction
    when its fingerprint matches, so a changed archive is always extracted afresh.
    """
    archive = Path(archive)
    destination = Path(destination)
    if not archive.is_file():
        raise ExportError(f"Archive not found: {archive}")
    if _is_link_or_reparse(destination):
        raise ExportError(f"Refusing to use a symlinked/reparse extraction directory: {destination}")
    if root is not None:
        root = Path(root)
        if _is_link_or_reparse(root):
            raise ExportError(f"Refusing to extract into a symlinked/reparse directory: {root}")
        _ensure_owned_dir(destination, root)
    fingerprint = _archive_fingerprint(archive)
    meta = _owned_marker(destination)
    if meta is not None and meta.get("fingerprint") == fingerprint and meta.get("archive") == archive.name:
        return {"archive": archive, "extracted": int(meta.get("extracted", 0)),
                "bytes": int(meta.get("bytes", 0)), "skipped": [], "reused": True}
    if destination.exists():
        if not destination.is_dir():
            raise ExportError(f"Refusing to replace a non-directory extraction target: {destination}")
        if any(destination.iterdir()):
            if meta is None:
                raise ExportError(
                    f"Refusing to replace a non-empty directory that was not created by this tool: {destination}. "
                    "Choose a different work directory."
                )
            if root is not None:
                _retain_owned_tree(destination, root)
            else:
                destination.rename(destination.with_name(destination.name + ".retained-" + uuid4().hex[:12]))
    destination.mkdir(parents=True, exist_ok=True)

    stats = {"archive": archive, "extracted": 0, "bytes": 0, "skipped": [], "reused": False}
    total = 0
    with _open_tar(archive) as tar:
        for member in tar:
            if member.isdir():
                continue
            if _is_unsafe_kind(member):
                stats["skipped"].append(member.name)
                continue
            parts = _safe_parts(member.name)
            if parts is None:
                stats["skipped"].append(member.name)
                continue
            if member.size > MAX_MEMBER_BYTES:
                stats["skipped"].append(f"{member.name} (too large)")
                continue
            source = tar.extractfile(member)
            if source is None:
                continue
            with source:
                head = source.read(16)
                if relevant_only and not _relevant_member(member.name, head):
                    continue
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    raise ExportError(
                        "Archive expands beyond the extraction safety limit "
                        f"({MAX_TOTAL_BYTES} bytes). Choose a smaller backup or extract it manually."
                    )
                target = destination.joinpath(*parts)
                _ensure_within(target, destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                partial = target.with_name(target.name + ".partial-" + uuid4().hex[:12])
                with partial.open("xb") as output:
                    output.write(head)
                    written += len(head)
                    while True:
                        chunk = source.read(_MEMBER_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > member.size:
                            break
                        output.write(chunk)
                if written != member.size:
                    stats["skipped"].append(f"{member.name} (truncated)")
                    continue
                if target.exists():
                    target.rename(target.with_name(target.name + ".retained-" + uuid4().hex[:12]))
                partial.rename(target)
                stats["extracted"] += 1
                stats["bytes"] += written
    marker = destination / ".extracted.json"
    marker.write_text(
        json.dumps({"archive": archive.name, "fingerprint": fingerprint,
                    "extracted": stats["extracted"], "bytes": stats["bytes"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return stats


# --------------------------------------------------------------------------- #
# Layout discovery
# --------------------------------------------------------------------------- #

def _is_within(child: Path, parent: Path) -> bool:
    try:
        return child.resolve().is_relative_to(parent.resolve())
    except OSError:  # pragma: no cover - defensive
        return False


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _iter_named(root: Path, name: str, max_depth: int = 4) -> Iterator[Path]:
    root = Path(root)
    base_depth = len(root.parts)
    for dirpath, dirnames, _ in os.walk(root):
        current = Path(dirpath)
        depth = len(current.parts) - base_depth
        dirnames[:] = [item for item in dirnames if not item.startswith(".")]
        if depth >= max_depth:
            dirnames[:] = []
        for item in list(dirnames):
            if item == name:
                yield current / item


def _detect_app_version(app_dir: Path | None, source: Path) -> str | None:
    """Best-effort app version reported by the backup, or ``None`` when absent."""
    files: list[Path] = []
    if app_dir is not None:
        shared_prefs = app_dir / "shared_prefs"
        if shared_prefs.is_dir():
            try:
                files.extend(sorted(item for item in shared_prefs.glob("*.xml") if item.is_file()))
            except OSError:
                pass
        files.extend([app_dir / "app_version.txt", app_dir / "version.txt"])
    files.extend([source / "app_version.txt", source / "version.txt"])
    for candidate in files:
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _VERSION_RE.search(text)
        value = match.group(2).strip() if match else (text.strip().splitlines()[0] if candidate.suffix == ".txt" else "")
        if value and len(value) <= 64 and all(ch.isprintable() for ch in value):
            return value
    return None


def _is_databases_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    return any(entry.is_file() and _is_main_db_name(entry.name) for entry in entries)


def _looks_like_app_dir(path: Path) -> bool:
    return path.is_dir() and ((path / "databases").is_dir() or
                              ((path / "files").is_dir() and (path / "small_emoji_res").is_dir()))


def _classify(root: Path, app_dirs: list[Path], db_dirs: list[Path], data_dirs: list[Path]) -> None:
    if not root.is_dir():
        return
    if (root / "databases").is_dir():
        app_dirs.append(root)
    if _is_databases_dir(root):
        db_dirs.append(root)
    if (root / "files").is_dir():
        if (root / "databases").is_dir():
            app_dirs.append(root)
        else:
            data_dirs.append(root)


def _select_app_dir(roots: list[Path]) -> tuple[Path | None, Path | None, Path | None]:
    app_dirs: list[Path] = []
    db_dirs: list[Path] = []
    data_dirs: list[Path] = []
    for root in roots:
        _classify(root, app_dirs, db_dirs, data_dirs)
        for candidate in _iter_named(root, APP_PACKAGE):
            _classify(candidate, app_dirs, db_dirs, data_dirs)
    app_dirs = _dedupe(app_dirs)
    db_dirs = _dedupe(db_dirs)
    data_dirs = _dedupe(data_dirs)

    if app_dirs:
        app_dir = app_dirs[0]
        databases_dir = app_dir / "databases" if (app_dir / "databases").is_dir() else None
    elif db_dirs:
        databases_dir = db_dirs[0]
        parent = databases_dir.parent
        app_dir = parent if _looks_like_app_dir(parent) else None
    else:
        app_dir = None
        databases_dir = None
    if databases_dir is None and db_dirs:
        databases_dir = db_dirs[0]
    data_dir = data_dirs[0] if data_dirs else None
    return app_dir, databases_dir, data_dir


def _build_layout(source: Path, work: Path, app_dir: Path | None, databases_dir: Path | None,
                  data_dir: Path | None, extracted_dir: Path | None,
                  archives: tuple[Path, ...]) -> SourceLayout:
    files_dir = keva_dir = profiles_dir = bubbles_path = None
    im_dir = im_resources_dir = small_emoji_dir = None
    if app_dir is not None:
        files_dir = app_dir / "files"
        if not files_dir.is_dir():
            files_dir = None
        if files_dir is not None:
            keva_dir = files_dir / "keva"
            if not keva_dir.is_dir():
                keva_dir = None
            im_dir = files_dir / "im"
            if not im_dir.is_dir():
                im_dir = None
            im_resources_dir = files_dir / "im_resources"
            if not im_resources_dir.is_dir():
                im_resources_dir = None
        small_emoji_dir = app_dir / "small_emoji_res"
        if not small_emoji_dir.is_dir():
            small_emoji_dir = None
        if keva_dir is not None:
            profiles_dir = keva_dir / "repo"
            if not profiles_dir.is_dir():
                profiles_dir = None
            candidate = keva_dir / "repo" / "bubble_detail_info"
            bubbles_path = candidate if candidate.is_dir() else None
    return SourceLayout(
        source=source,
        app_dir=app_dir,
        databases_dir=databases_dir,
        work_dir=work,
        data_dir=data_dir,
        extracted_dir=extracted_dir,
        files_dir=files_dir,
        keva_dir=keva_dir,
        profiles_dir=profiles_dir,
        bubbles_path=bubbles_path,
        im_dir=im_dir,
        im_resources_dir=im_resources_dir,
        small_emoji_dir=small_emoji_dir,
        archives=archives,
        app_version=_detect_app_version(app_dir, source),
    )


def _find_archives(root: Path, max_depth: int = 3) -> list[Path]:
    root = Path(root)
    base_depth = len(root.parts)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.parts) - base_depth
        dirnames[:] = [item for item in dirnames if not item.startswith(".")]
        if depth >= max_depth:
            dirnames[:] = []
        for filename in filenames:
            candidate = current / filename
            if _looks_like_archive_file(candidate):
                found.append(candidate)
    return sorted(found)


def _skip_archive(path: Path) -> bool:
    stem = path.name.lower()
    for suffix in ARCHIVE_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem.strip(".") in _SKIP_ARCHIVE_STEMS


def prepare_source(source: os.PathLike[str] | str, work: os.PathLike[str] | str) -> SourceLayout:
    """Locate Duoshan app data for ``source`` and extract any archives into ``work``."""
    source_path = Path(source).expanduser().resolve()
    work_path = Path(work).expanduser().resolve()
    if not source_path.exists():
        raise ExportError(f"Source path does not exist: {source_path}")
    if work_path == source_path or _is_within(work_path, source_path):
        raise ExportError(
            "The work directory must be outside the source path so the original backup is never modified."
        )
    work_path.mkdir(parents=True, exist_ok=True)
    extracted_root = work_path / "extracted"

    archives: list[Path] = []
    search_roots: list[Path] = []
    extracted_dir: Path | None = None

    if source_path.is_file():
        if _looks_like_archive_file(source_path):
            archives = [source_path]
        elif _looks_like_database_file(source_path) or _is_main_db_name(source_path.name) or _is_contacts_db_name(source_path.name):
            # A single database file: report its directory and a best-effort app dir.
            app_dir, _, data_dir = _select_app_dir([source_path.parent])
            databases_dir = source_path.parent
            return _build_layout(source_path, work_path, app_dir, databases_dir, data_dir, None, ())
        else:
            raise ExportError(f"Unrecognised source file: {source_path}. {_LAYOUT_HINT}")
    else:
        search_roots.append(source_path)
        archives = _find_archives(source_path)

    if not archives and source_path.is_dir() and not search_roots:
        search_roots = [source_path]

    for archive in archives:
        if _skip_archive(archive):
            continue
        stem = archive.name
        for suffix in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
            if stem.lower().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        destination = extracted_root / (stem or "archive")
        extract_archive(archive, destination, root=extracted_root)
        search_roots.append(destination)
        if extracted_dir is None:
            extracted_dir = destination

    if not archives and source_path.is_dir():
        # No archives: the source is already extracted app data.
        pass

    app_dir, databases_dir, data_dir = _select_app_dir(search_roots)
    if databases_dir is None and app_dir is None:
        # Fall back to a bounded search for main databases anywhere under the roots.
        for root in search_roots:
            for candidate in _iter_named(root, "databases"):
                if _is_databases_dir(candidate):
                    databases_dir = candidate
                    break
            if databases_dir is not None:
                break
    return _build_layout(source_path, work_path, app_dir, databases_dir, data_dir,
                         extracted_dir, tuple(archives))


def _looks_like_database_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    return head == SQLITE_HEADER or head[:4] == b"SQLi"


# --------------------------------------------------------------------------- #
# Account discovery
# --------------------------------------------------------------------------- #

def _is_main_db_name(name: str) -> bool:
    lowered = name.lower()
    return bool(_MAIN_NAME.match(name) or _MAIN_NAME_ALT.match(name) or lowered in {"im.db", "im"})


def _is_contacts_db_name(name: str) -> bool:
    return bool(_CONTACTS_NAME.match(name))


def _main_db_uid(name: str) -> str | None:
    match = _MAIN_NAME.match(name) or _MAIN_NAME_ALT.match(name)
    return match.group("uid") if match else None


def _contacts_db_uid(name: str) -> str | None:
    match = _CONTACTS_NAME.match(name)
    return match.group("uid") if match else None


def _scan_files(root: Path, max_depth: int = 2) -> list[Path]:
    root = Path(root)
    base_depth = len(root.parts)
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.parts) - base_depth
        dirnames[:] = [item for item in dirnames if not item.startswith(".")]
        if depth >= max_depth:
            dirnames[:] = []
        for filename in filenames:
            files.append(current / filename)
    return files


def _profile_uids(layout: SourceLayout) -> dict[str, Path]:
    profiles: dict[str, Path] = {}
    if layout.profiles_dir is None or not layout.profiles_dir.is_dir():
        return profiles
    bases: list[Path] = [layout.profiles_dir]
    nested = layout.profiles_dir / "aweme_user"
    if nested.is_dir():
        bases.append(nested)
    for base in bases:
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            match = _PROFILE_NAME.match(entry.name)
            if not match:
                continue
            uid = match.group("uid")
            if entry.is_file():
                profiles.setdefault(uid, entry)
            elif entry.is_dir():
                # Keva stores the profile as <uid>_aweme_user_info/*.sgv.
                payloads = sorted(entry.glob("*.sgv"))
                profiles.setdefault(uid, payloads[0] if payloads else entry)
    return profiles


def _guess_uid_for_bare_db(database: Path, profiles: dict[str, Path]) -> str | None:
    if len(profiles) == 1:
        return next(iter(profiles))
    if database.parent.name.isdecimal():
        return database.parent.name
    match = re.search(r"(\d{6,})", database.parent.name)
    return match.group(1) if match else None


def discover_accounts(layout: SourceLayout) -> list[AccountSource]:
    """Discover every Duoshan account reachable from ``layout``."""
    search_dirs: list[Path] = []
    if layout.databases_dir is not None and layout.databases_dir.is_dir():
        search_dirs.append(layout.databases_dir)
    if layout.app_dir is not None:
        candidate = layout.app_dir / "databases"
        if candidate.is_dir() and candidate not in search_dirs:
            search_dirs.append(candidate)
    if not search_dirs:
        # Last resort: bounded search under the source and extracted roots.
        for root in {layout.source, layout.app_dir}:
            if root is None or not root.is_dir():
                continue
            for candidate in _iter_named(root, "databases"):
                if _is_databases_dir(candidate):
                    search_dirs.append(candidate)
                    break
            if search_dirs:
                break
    if not search_dirs:
        raise ExportError(
            "No 'databases' folder was found. " + _LAYOUT_HINT
        )

    files: list[Path] = []
    for directory in search_dirs:
        files.extend(_scan_files(directory))
    if not files:
        raise ExportError(f"No database files were found in: {', '.join(str(item) for item in search_dirs)}")

    contacts_by_uid: dict[str, Path] = {}
    for path in files:
        uid = _contacts_db_uid(path.name)
        if uid is not None:
            contacts_by_uid.setdefault(uid, path)

    profiles = _profile_uids(layout)

    candidates: list[tuple[str, Path]] = []
    for path in files:
        if not _is_main_db_name(path.name):
            continue
        uid = _main_db_uid(path.name) or _guess_uid_for_bare_db(path, profiles)
        if uid is None:
            continue
        candidates.append((uid, path))

    if not candidates:
        raise ExportError(
            "No Duoshan IM database (names like 'encrypted_<uid>_im.db' or '<uid>_im.db') was found in: "
            + ", ".join(str(item) for item in search_dirs)
        )

    # Prefer the canonical 'encrypted_<uid>_im.db' spelling when both exist.
    candidates.sort(key=lambda item: (not item[1].name.lower().startswith("encrypted_"), item[1].name.lower()))
    accounts: list[AccountSource] = []
    seen: set[str] = set()
    for uid, database in candidates:
        if uid in seen:
            continue
        seen.add(uid)
        accounts.append(
            AccountSource(
                uid=uid,
                database=database,
                contacts_database=contacts_by_uid.get(uid),
                profile_path=profiles.get(uid),
                bubbles_path=layout.bubbles_path,
            )
        )
    return sorted(accounts, key=lambda account: account.uid)
