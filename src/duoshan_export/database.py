"""Open and decrypt Duoshan databases from private, fingerprinted snapshots.

Plain SQLite databases are opened with the standard library. Encrypted
Duoshan SQLCipher databases load ``sqlcipher3`` lazily, so importing this
package never requires the extension. The original files are only ever read:
every open or decryption works on a private snapshot copied into the work
directory, together with the matching ``-wal`` (and ``-journal``) files so the
committed state of the backup is preserved.

Snapshot caching is keyed by the resolved source path *and* a content/file
fingerprint. Two different backups that happen to share a database filename
therefore never reuse each other's snapshot, and re-extracting a backup with
the same name replaces the stale snapshot instead of silently reading it.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
from typing import Iterable
from uuid import uuid4

from .errors import ExportError

SQLITE_HEADER = b"SQLite format 3\x00"
CONTACTS_PASSPHRASE = "relation_user_center_passphrase"
MAIN_TABLES = ("msg",)
CONTACT_TABLES = ("user",)
SIDECARS = ("-wal", "-journal")
_FINGERPRINT_CHUNK = 1 << 20
_REPARSE_POINT = 0x400

SQLCIPHER_HINT = (
    "Encrypted Duoshan databases need the SQLCipher extension. Install a wheel with "
    "'pip install sqlcipher3-wheels' (or 'pip install sqlcipher3-binary'), or point "
    "--sqlcipher-module-path at the folder that contains the sqlcipher3 package. "
    "Plain SQLite databases need no extra dependency."
)

_CONTACTS_NAME = re.compile(r"^(?:relation_uc_v3_|relation_user_center_v2_)(?P<uid>\d{6,})(?:\.db)?$", re.IGNORECASE)
_MAIN_NAME = re.compile(r"^(?:encrypted_)?(?P<uid>\d{6,})_im(?:\.db)?$", re.IGNORECASE)
_MAIN_NAME_ALT = re.compile(r"^(?:encrypted_)?im_(?P<uid>\d{6,})(?:\.db)?$", re.IGNORECASE)

_SQLCIPHER_CACHE: dict[str, object] = {}


def main_password(uid: str) -> str:
    """Return the verified Duoshan IM key derivation for an account ID."""
    uid = str(uid)
    if not uid.isascii() or not uid:
        raise ExportError("Account ID must be a non-empty ASCII string.")
    return f"byte{uid}imwcdb{uid}dance"


def database_role(path: os.PathLike[str] | str) -> str:
    """Classify a database path as ``'main'``, ``'contacts'`` or ``'unknown'``."""
    name = Path(path).name
    if _CONTACTS_NAME.match(name):
        return "contacts"
    if _MAIN_NAME.match(name) or _MAIN_NAME_ALT.match(name):
        return "main"
    return "unknown"


def read_header(path: os.PathLike[str] | str, size: int = 16) -> bytes:
    try:
        with Path(path).open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def is_plain_sqlite(path: os.PathLike[str] | str) -> bool:
    """True when the file starts with the standard SQLite header."""
    return read_header(path) == SQLITE_HEADER


def sqlcipher_available(module_path: os.PathLike[str] | str | None = None) -> bool:
    """Return True when the SQLCipher extension can be imported."""
    try:
        _load_sqlcipher(module_path)
    except ExportError:
        return False
    return True


def _load_sqlcipher(module_path: os.PathLike[str] | str | None = None):
    cache_key = str(module_path) if module_path is not None else ""
    cached = _SQLCIPHER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    if module_path is not None:
        target = Path(module_path).expanduser()
        if not target.exists():
            raise ExportError(f"SQLCipher module path does not exist: {target}")
        base = target.parent if target.is_file() else target
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))
    try:
        from sqlcipher3 import dbapi2 as cipher  # type: ignore import-not-found
    except ImportError as error:
        raise ExportError(SQLCIPHER_HINT) from error
    _SQLCIPHER_CACHE[cache_key] = cipher
    return cipher


def _credentials(path: Path, uid: str, password: str | None, compatibility: int) -> tuple[str, int]:
    role = database_role(path)
    if password is not None:
        return password, compatibility
    if role == "contacts":
        return CONTACTS_PASSPHRASE, 4
    return main_password(uid), compatibility


def _update_with_file(digest, path: Path) -> None:
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(_FINGERPRINT_CHUNK), b""):
                digest.update(block)
    except OSError as error:
        raise ExportError(f"Cannot read file for fingerprinting: {path}") from error


def _content_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        stat = path.stat()
    except OSError as error:
        raise ExportError(f"Cannot read database file: {path}") from error
    digest.update(str(path).encode("utf-8"))
    digest.update(f"|size={stat.st_size}|mtime={stat.st_mtime_ns}".encode("ascii"))
    _update_with_file(digest, path)
    for suffix in SIDECARS:
        side = Path(str(path) + suffix)
        if side.is_file():
            side_stat = side.stat()
            digest.update(f"|{suffix}|{side_stat.st_size}|{side_stat.st_mtime_ns}".encode("ascii"))
            _update_with_file(digest, side)
    return digest.hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    """True for symbolic links and Windows reparse points (e.g. junctions)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _snapshot(path: Path, work_dir: Path) -> Path:
    """Copy ``path`` plus its WAL/journal into a private, fingerprint-checked snapshot."""
    if not path.is_file():
        raise ExportError(f"Database file not found: {path}")
    work_dir = Path(work_dir)
    fingerprint = _content_fingerprint(path)
    key = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    snapshots_root = work_dir / "db_snapshots"
    if _is_link_or_reparse(snapshots_root):
        raise ExportError(f"Refusing to use a symlinked/reparse snapshot directory: {snapshots_root}")
    root = snapshots_root / key
    target = root / path.name
    marker = root / "snapshot.json"
    if target.is_file() and marker.is_file():
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if meta.get("fingerprint") == fingerprint and meta.get("name") == path.name:
            return target
    staging = snapshots_root / f".{key}.{os.getpid()}.{uuid4().hex[:12]}.tmp"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copyfile(path, staging / path.name)
        for suffix in SIDECARS:
            side = Path(str(path) + suffix)
            if side.is_file():
                shutil.copyfile(side, staging / (path.name + suffix))
        (staging / "snapshot.json").write_text(
            json.dumps({"source": str(path), "name": path.name, "fingerprint": fingerprint}, ensure_ascii=False),
            encoding="utf-8",
        )
        retained = None
        if root.exists():
            if _is_link_or_reparse(root):
                raise ExportError(f"Refusing to replace a symlinked/reparse snapshot: {root}")
            retained = root.with_name(root.name + ".retained-" + uuid4().hex[:12])
            root.rename(retained)
        try:
            staging.rename(root)
        except OSError:
            if retained and retained.exists() and not root.exists():
                retained.rename(root)
            raise
    except BaseException:
        # Retain partial snapshots for diagnosis, including ENOSPC/cancellation.
        # A unique staging name lets the next attempt proceed without deletion.
        raise
    return root / path.name


def require_tables(db: sqlite3.Connection, tables: Iterable[str], *,
                   location: os.PathLike[str] | str | None = None,
                   hint: str | None = None) -> None:
    """Raise an actionable :class:`ExportError` when required tables are absent."""
    wanted = tuple(tables)
    if not wanted:
        return
    try:
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    except sqlite3.Error as error:
        raise ExportError(f"Could not read the database schema: {error}") from error
    missing = [name for name in wanted if name not in present]
    if missing:
        where = f" in {location}" if location is not None else ""
        message = (
            f"Missing required table(s){where}: {', '.join(missing)}. "
            + (hint or "This file does not look like a supported Duoshan chat database.")
        )
        raise ExportError(message)


def _required_for_role(role: str) -> tuple[str, ...]:
    if role == "main":
        return MAIN_TABLES
    if role == "contacts":
        return CONTACT_TABLES
    return ()


def _open_plain(snapshot: Path):
    connection = sqlite3.connect(str(snapshot))
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _open_cipher(snapshot: Path, uid: str, password: str | None, compatibility: int,
                 sqlcipher_module_path: os.PathLike[str] | str | None):
    cipher = _load_sqlcipher(sqlcipher_module_path)
    key, effective_compatibility = _credentials(snapshot, uid, password, compatibility)
    connection = cipher.connect(str(snapshot))
    try:
        connection.execute("PRAGMA key='" + key.replace("'", "''") + "'")
        connection.execute(f"PRAGMA cipher_compatibility={int(effective_compatibility)}")
        connection.execute("PRAGMA cipher_page_size=4096")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception as error:  # sqlcipher3 raises its own DatabaseError subclass
            connection.close()
            raise ExportError(
                f"Could not decrypt {snapshot.name} with the derived key for account {uid}. "
                "The backup may use a different key derivation or SQLCipher layout, or the account ID may be wrong."
            ) from error
    except BaseException:
        try:
            connection.close()
        except Exception:
            pass
        raise
    return connection


@contextmanager
def open_database(source: os.PathLike[str] | str, uid: str, work_dir: os.PathLike[str] | str, *,
                  password: str | None = None, compatibility: int = 3,
                  sqlcipher_module_path: os.PathLike[str] | str | None = None,
                  required_tables: Iterable[str] | None = None):
    """Open a read-only view of a Duoshan database via a private snapshot.

    Plain SQLite files are opened with :mod:`sqlite3`; encrypted files use the
    lazily imported SQLCipher extension. ``password``/``compatibility`` default
    to the verified layout: the main IM database uses the account-derived key
    with compatibility 3, while the ``relation_uc_v3`` contact cache uses the
    known passphrase with compatibility 4.
    """
    path = Path(source)
    if not path.is_file():
        raise ExportError(f"Database file not found: {path}")
    snapshot = _snapshot(path, Path(work_dir))
    role = database_role(path)
    required = tuple(required_tables) if required_tables is not None else _required_for_role(role)
    if is_plain_sqlite(path):
        connection = _open_plain(snapshot)
        try:
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
            require_tables(connection, required, location=path)
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()
        return
    connection = _open_cipher(snapshot, uid, password, compatibility, sqlcipher_module_path)
    try:
        require_tables(connection, required, location=path)
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def decrypt_database(source: os.PathLike[str] | str, uid: str,
                     destination: os.PathLike[str] | str, work_dir: os.PathLike[str] | str, *,
                     sqlcipher_module_path: os.PathLike[str] | str | None = None) -> None:
    """Write a standalone plaintext SQLite copy of ``source`` to ``destination``.

    The source is read only through a private snapshot. A plaintext source is
    copied after checkpointing its WAL; an encrypted source is exported by
    SQLCipher. The result is integrity-checked before it atomically replaces
    ``destination``.
    """
    path = Path(source)
    destination = Path(destination)
    if not path.is_file():
        raise ExportError(f"Database file not found: {path}")
    if destination.resolve() == path.resolve():
        raise ExportError("Refusing to overwrite the source database with its decrypted copy.")
    snapshot = _snapshot(path, Path(work_dir))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{os.getpid()}.{uuid4().hex[:12]}.tmp")

    plain_source = is_plain_sqlite(path)
    try:
        if plain_source:
            connection = _open_plain(snapshot)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
            shutil.copyfile(snapshot, temporary)
        else:
            cipher = _load_sqlcipher(sqlcipher_module_path)
            key, compatibility = _credentials(snapshot, uid, None, 3)
            connection = cipher.connect(str(snapshot))
            try:
                connection.execute("PRAGMA key='" + key.replace("'", "''") + "'")
                connection.execute(f"PRAGMA cipher_compatibility={int(compatibility)}")
                connection.execute("PRAGMA cipher_page_size=4096")
                try:
                    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
                except Exception as error:
                    raise ExportError(
                        f"Could not decrypt {path.name} with the derived key for account {uid}. "
                        "The backup may use a different key derivation or SQLCipher layout."
                    ) from error
                connection.execute("ATTACH DATABASE ? AS plaintext KEY ''", (str(temporary),))
                connection.execute("SELECT sqlcipher_export('plaintext')").fetchone()
                connection.execute("DETACH DATABASE plaintext")
            finally:
                connection.close()
        check = sqlite3.connect(str(temporary))
        try:
            result = check.execute("PRAGMA integrity_check").fetchall()
        finally:
            check.close()
            check = None  # release the handle before the atomic replace (Windows)
        if result != [("ok",)]:
            raise ExportError(f"Decrypted database failed its integrity check: {result!r}")
        retained = None
        if destination.exists():
            retained = destination.with_name(destination.name + ".retained-" + uuid4().hex[:12])
            destination.rename(retained)
        try:
            temporary.rename(destination)
        except OSError:
            if retained and retained.exists() and not destination.exists():
                retained.rename(destination)
            raise
    except BaseException:
        # Failed plaintext files remain available for inspection/recovery.
        raise
