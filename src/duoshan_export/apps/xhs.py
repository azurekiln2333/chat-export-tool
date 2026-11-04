"""Xiaohongshu (``com.xingin.xhs``) app rules: layout, secrets and accounts.

This module encodes the verified layout of Xiaohongshu's Android private data
as found in DataBackup archives:

    <app>/databases/msgDB              IM chat database (WCDB encrypted)
    <app>/databases/localRelationDB    contact/relation cache (WCDB encrypted)
    <app>/databases/hedwig.db          IM conversation metadata (WCDB encrypted)
    <app>/files/mmkv/<instance>        MMKV key-value files holding passphrases

The databases are opened through :mod:`.apps.wcdb` (the stock ``sqlcipher3``
wheel cannot read this fork's page HMACs). Passphrases live in MMKV instances;
when a database has no stored passphrase the app's built-in default is used.
A single ``msgDB`` can accumulate rows from more than one logged-in account:
each message row carries its owning account in the ``@`` suffix of
``local_chat_user_id`` / ``local_group_chat_id``, which is how per-account
exports stay separated.

Nothing in this module writes to the original backup, contacts the network,
or hardcodes any account identifier.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from ..errors import ExportError
from . import mmkv
from . import wcdb

APP_PACKAGE = "com.xingin.xhs"

# MMKV instance that k0b.h.g() opens for app-wide secrets.
MAIN_MMKV_INSTANCE = "com.xingin.xhs_preferences"

DEFAULT_PASSWORD_MMKV = "com.xingin.xhs_preferences"
LOGIN_MMKV_INSTANCE = "login_user_info_kv"
LOGIN_USER_ID_KEY = "login_user_id_key"
# The user-switch profile JSON (nickname, red_id) of the current account.
PROFILE_MMKV_INSTANCE = "com.xingin.xhs"
PROFILE_USERINFO_KEY = "key_desc_userinfo"

# Databases the export needs, with the MMKV key holding their passphrase and
# the app's built-in default (used when no rotated password was stored).
@dataclass(frozen=True)
class DatabaseSpec:
    name: str
    role: str                    # 'messages' | 'contacts'
    password_key: str
    default_passphrase: str
    mmkv_instances: tuple[str, ...] = (MAIN_MMKV_INSTANCE,)

    @property
    def mmkv_file_names(self) -> tuple[str, ...]:
        return self.mmkv_instances


MESSAGE_DB = DatabaseSpec(
    name="msgDB", role="messages",
    password_key="msg_db_password_updated", default_passphrase="xhsdev",
)
CONTACTS_DB = DatabaseSpec(
    name="localRelationDB", role="contacts",
    password_key="relation_db_password_updated", default_passphrase="",
)
SUPPORTED_DATABASES = (MESSAGE_DB, CONTACTS_DB)

# Database file names that identify a Xiaohongshu app directory.
MARKER_DATABASES = frozenset({"msgDB", "localRelationDB", "hedwig.db"})


@dataclass(frozen=True)
class XhsSource:
    """Discovered Xiaohongshu app data inside a prepared backup layout."""

    app_dir: Path
    databases_dir: Path
    mmkv_dir: Path | None
    databases: dict[str, Path]

    @property
    def has_message_db(self) -> bool:
        return MESSAGE_DB.name in self.databases


def discover(layout) -> XhsSource | None:
    """Locate Xiaohongshu app data inside a prepared :class:`SourceLayout`.

    Returns ``None`` when the backup does not contain the app. The layout's
    archive extraction is reused as-is; because the shared source module only
    pins the Duoshan package name, this routine also scans every extracted
    archive root for the Xiaohongshu app directory.
    """
    candidates: list[Path] = []
    app_dir = getattr(layout, "app_dir", None)
    if app_dir is not None and Path(app_dir).is_dir():
        candidates.append(Path(app_dir))
    work_dir = getattr(layout, "work_dir", None)
    if work_dir is not None:
        extracted_root = Path(work_dir) / "extracted"
        if extracted_root.is_dir():
            # Extracted archive destinations: <stem>/com.xingin.xhs or <stem>/
            for stem_dir in sorted(extracted_root.iterdir()):
                if not stem_dir.is_dir():
                    continue
                if stem_dir.name == APP_PACKAGE:
                    candidates.append(stem_dir)
                child = stem_dir / APP_PACKAGE
                if child.is_dir():
                    candidates.append(child)
    extracted = getattr(layout, "extracted_dir", None)
    if extracted is not None and Path(extracted).is_dir():
        for child in sorted(Path(extracted).iterdir()):
            if child.is_dir() and (child / "databases").is_dir():
                candidates.append(child)
    source = getattr(layout, "source", None)
    if source is not None and Path(source).is_dir():
        for child in sorted(Path(source).iterdir()):
            if child.is_dir() and (child / "databases").is_dir():
                candidates.append(child)
        if (Path(source) / "databases").is_dir():
            candidates.append(Path(source))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        source_info = _probe(candidate)
        if source_info is not None:
            return source_info
    return None


def _probe(app_dir: Path) -> XhsSource | None:
    databases_dir = app_dir / "databases"
    if not databases_dir.is_dir():
        return None
    databases: dict[str, Path] = {}
    try:
        entries = sorted(item for item in databases_dir.iterdir() if item.is_file())
    except OSError:
        return None
    for entry in entries:
        if entry.name in MARKER_DATABASES:
            databases.setdefault(entry.name, entry)
    if not databases:
        return None
    mmkv_dir = app_dir / "files" / "mmkv"
    if not mmkv_dir.is_dir():
        mmkv_dir = None
    return XhsSource(app_dir=app_dir, databases_dir=databases_dir,
                     mmkv_dir=mmkv_dir, databases=databases)


def mmkv_instance_path(source: XhsSource, instance: str) -> Path | None:
    """Path of one MMKV instance file, or ``None`` when absent."""
    if source.mmkv_dir is None:
        return None
    candidate = source.mmkv_dir / instance
    return candidate if candidate.is_file() else None


def resolve_passphrase(source: XhsSource, spec: DatabaseSpec) -> str:
    """The stored passphrase for a database, else the app's built-in default.

    MMKV stores byte-array values wrapped in an inner length prefix, which
    :func:`mmkv.decode_bytes` removes. The passphrase is used as raw UTF-8
    bytes by WCDB, so text is returned unchanged.
    """
    for instance in spec.mmkv_file_names:
        path = mmkv_instance_path(source, instance)
        if path is None:
            continue
        try:
            table = mmkv.load(path)
        except (OSError, ValueError):
            continue
        blob = table.get(spec.password_key)
        if blob is None:
            continue
        return mmkv.decode_bytes(blob).decode("utf-8", errors="replace")
    return spec.default_passphrase


def login_user_id(source: XhsSource) -> str | None:
    """The last logged-in account id from the login MMKV, when available."""
    path = mmkv_instance_path(source, LOGIN_MMKV_INSTANCE)
    if path is None:
        return None
    try:
        table = mmkv.load(path)
    except (OSError, ValueError):
        return None
    blob = table.get(LOGIN_USER_ID_KEY)
    if blob is None:
        return None
    value = mmkv.decode_string(blob).strip()
    return value or None


def login_user_profile(source: XhsSource) -> dict:
    """Profile of the current account from the user-switch MMKV, when available.

    Reads ``key_desc_userinfo`` (a JSON document with the current account's
    nickname, red_id and more) and returns the parsed dict, or ``{}``.
    """
    path = mmkv_instance_path(source, PROFILE_MMKV_INSTANCE)
    if path is None:
        return {}
    try:
        table = mmkv.load(path)
    except (OSError, ValueError):
        return {}
    blob = table.get(PROFILE_USERINFO_KEY)
    if blob is None:
        return {}
    import json

    text = mmkv.decode_string(blob).strip()
    if not text.startswith("{"):
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def decrypt_database(source: XhsSource, spec: DatabaseSpec,
                     destination: os.PathLike[str] | str) -> dict:
    """Decrypt one database into a plain SQLite file via the WCDB pipeline."""
    database_path = source.databases.get(spec.name)
    if database_path is None:
        raise ExportError(f"{spec.name} is not present in this backup's databases directory.")
    passphrase = resolve_passphrase(source, spec).encode("utf-8")
    return wcdb.decrypt_database(database_path, passphrase, destination)


def account_ids(db) -> list[str]:
    """Account ids that own rows in an opened msgDB connection.

    Ownership is carried by the ``@<uid>`` suffix of ``local_chat_user_id``
    (direct chats) and ``local_group_chat_id`` (group chats). Sorted by
    message count, most first; ids without a suffix are ignored.
    """
    counts: dict[str, int] = {}
    for (owner, count) in db.execute(
        """SELECT owner, count(*) FROM (
             SELECT CASE WHEN local_chat_user_id LIKE '%@%'
                         THEN substr(local_chat_user_id, instr(local_chat_user_id, '@') + 1)
                         WHEN local_group_chat_id LIKE '%@%'
                         THEN substr(local_group_chat_id, instr(local_group_chat_id, '@') + 1)
                         ELSE '' END AS owner
             FROM message) WHERE owner != '' GROUP BY owner"""
    ):
        counts[str(owner)] = int(count)
    return [uid for uid, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _owner_suffix(column_owner: object) -> str:
    text = str(column_owner or "")
    if "@" not in text:
        return ""
    return text.rsplit("@", 1)[1]
