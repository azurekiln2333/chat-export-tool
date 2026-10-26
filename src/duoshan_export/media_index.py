"""Index local media in exported databases without changing original chat tables."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
import os
from pathlib import Path
import sqlite3

from .config import ExportConfig
from .errors import ExportError
from .paths import ACCOUNT_FILES, ROOT_FILES
from .runtime import write_json

PREFIX = 'export_'

ATTACHMENT_COLUMNS = [
    'account_uid', 'msg_uuid', 'kind', 'status', 'resource_id', 'message_count',
    'path_rel_export', 'path_rel_db', 'duration_seconds', 'format', 'container',
    'source_format', 'bytes',
    'source_bytes', 'mime', 'codec', 'width', 'height', 'frames', 'sha256', 'source_url',
    'exists_on_disk', 'in_msg_table', 'msg_type', 'conversation_id', 'msg_created_time',
    'msg_sender', 'indexed_at',
]

ATTACHMENT_DDL = """
CREATE TABLE IF NOT EXISTS export_local_attachment (
    account_uid       TEXT    NOT NULL,
    msg_uuid          TEXT    NOT NULL,
    kind              TEXT,
    status            TEXT,
    resource_id       TEXT,
    message_count     INTEGER,
    path_rel_export   TEXT,
    path_rel_db       TEXT,
    duration_seconds  REAL,
    format            TEXT,
    container         TEXT,
    source_format     TEXT,
    bytes             INTEGER,
    source_bytes      INTEGER,
    mime              TEXT,
    codec             TEXT,
    width             INTEGER,
    height            INTEGER,
    frames            INTEGER,
    sha256            TEXT,
    source_url        TEXT,
    exists_on_disk    INTEGER,
    in_msg_table      INTEGER,
    msg_type          INTEGER,
    conversation_id   TEXT,
    msg_created_time  INTEGER,
    msg_sender        TEXT,
    indexed_at        TEXT    NOT NULL,
    PRIMARY KEY (account_uid, msg_uuid)
)
"""

BUBBLE_COLUMNS = [
    'account_uid', 'asset_scope', 'asset_key', 'role', 'path_rel_export', 'path_rel_db',
    'width', 'height', 'frames', 'sha256', 'source_url', 'nine_patch', 'png_chunks',
    'version', 'exists_on_disk', 'indexed_at',
]

BUBBLE_DDL = """
CREATE TABLE IF NOT EXISTS export_bubble_resource (
    account_uid     TEXT NOT NULL,
    asset_scope     TEXT NOT NULL,
    asset_key       TEXT NOT NULL,
    role            TEXT NOT NULL,
    path_rel_export TEXT,
    path_rel_db     TEXT,
    width           INTEGER,
    height          INTEGER,
    frames          INTEGER,
    sha256          TEXT,
    source_url      TEXT,
    nine_patch      TEXT,
    png_chunks      TEXT,
    version         TEXT,
    exists_on_disk  INTEGER,
    indexed_at      TEXT NOT NULL,
    PRIMARY KEY (account_uid, asset_scope, asset_key, role)
)
"""

VIEW_NAME = 'export_chat_with_media'
VIEW_DDL = """
CREATE VIEW export_chat_with_media AS
SELECT
    m.msg_uuid                       AS msg_uuid,
    m.conversation_id                AS conversation_id,
    m.conversation_short_id          AS conversation_short_id,
    m.conversation_type              AS conversation_type,
    m.type                           AS msg_type,
    m.sub_type                       AS msg_sub_type,
    m.created_time                   AS created_time,
    m.sender                         AS sender,
    m.deleted                        AS deleted,
    m.status                         AS msg_status,
    a.kind                           AS media_kind,
    a.status                         AS media_status,
    a.resource_id                    AS media_resource_id,
    a.path_rel_export                AS media_path_rel_export,
    a.path_rel_db                    AS media_path_rel_db,
    a.duration_seconds               AS media_duration_seconds,
    a.format                         AS media_format,
    a.container                      AS media_container,
    a.source_format                  AS media_source_format,
    a.mime                           AS media_mime,
    a.codec                          AS media_codec,
    a.bytes                          AS media_bytes,
    a.source_bytes                   AS media_source_bytes,
    a.width                          AS media_width,
    a.height                         AS media_height,
    a.frames                         AS media_frames,
    a.sha256                         AS media_sha256,
    a.exists_on_disk                 AS media_exists_on_disk,
    a.message_count                  AS media_message_count,
    a.indexed_at                     AS media_indexed_at
FROM msg AS m
LEFT JOIN export_local_attachment AS a
       ON a.msg_uuid = m.msg_uuid
      AND a.account_uid = {uid!r}
"""




def _local_path(relative: str, output: Path) -> Path:
    if not relative or not relative.isascii() or ':' in relative or '\\' in relative:
        raise ExportError('Media index contains an unsafe path.')
    path = (output / relative).resolve()
    if not path.is_relative_to(output.resolve()):
        raise ExportError('Media index path leaves the export directory.')
    return path


def db_relative(export_rel: str, account_dir: Path, output: Path) -> str | None:
    if not export_rel:
        return None
    return Path(os.path.relpath(_local_path(export_rel, output), account_dir)).as_posix()


def exists(export_rel: str, output: Path) -> int:
    return int(_local_path(export_rel, output).is_file()) if export_rel else 0


def msg_fingerprint(db: sqlite3.Connection) -> dict:
    """Cheap but sensitive fingerprint of the untouched msg table."""
    digest = hashlib.sha256()
    rows = 0
    for row in db.execute('SELECT * FROM msg ORDER BY msg_uuid'):
        rows += 1
        digest.update(repr(row).encode('utf-8', 'surrogatepass'))
    return {'rows': rows, 'sha256': digest.hexdigest()}


def msg_lookup(db: sqlite3.Connection, uuids: list[str]) -> dict:
    found = {}
    for start in range(0, len(uuids), 800):
        chunk = uuids[start:start + 800]
        placeholders = ','.join('?' * len(chunk))
        for row in db.execute(
            f'SELECT msg_uuid, type, conversation_id, created_time, sender '
            f'FROM msg WHERE msg_uuid IN ({placeholders})', chunk):
            found[row[0]] = row[1:]
    return found


def build_attachment_rows(uid: str, manifest: dict, resources: dict,
                          account_dir: Path, msg_meta: dict, output: Path, indexed_at: str) -> list[tuple]:
    rows = []
    for key, entry in manifest.items():
        if not key.startswith(uid + ':'):
            continue
        msg_uuid = key.split(':', 1)[1]
        resource = resources.get(entry.get('resource_id'), {})
        export_rel = entry.get('path')
        fmt = entry.get('format')
        container = None
        if export_rel:
            container = Path(export_rel).suffix.lstrip('.').upper() or None
        if not container and entry.get('mime'):
            container = entry['mime'].split('/')[-1].upper()
        meta = msg_meta.get(msg_uuid)
        rows.append((
            uid,
            msg_uuid,
            entry.get('kind'),
            entry.get('status'),
            entry.get('resource_id'),
            entry.get('message_count'),
            export_rel,
            db_relative(export_rel, account_dir, output) if export_rel else None,
            entry.get('duration'),
            fmt,
            container,
            entry.get('source_format'),
            entry.get('bytes'),
            entry.get('source_bytes'),
            entry.get('mime'),
            entry.get('codec'),
            entry.get('width'),
            entry.get('height'),
            entry.get('frames'),
            resource.get('sha256'),
            resource.get('source_url'),
            exists(export_rel, output),
            1 if meta is not None else 0,
            meta[0] if meta else None,
            meta[1] if meta else None,
            meta[2] if meta else None,
            str(meta[3]) if meta else None,
            indexed_at,
        ))
    return rows


def decoration_rows(uid: str, decorations: dict, account_dir: Path, output: Path, indexed_at: str) -> list[tuple]:
    rows = []
    seen = set()

    def emit(scope, asset_key, role, asset):
        path = asset.get('path')
        if not path:
            return
        marker = (scope, asset_key, role)
        if marker in seen:
            return
        seen.add(marker)
        nine = asset.get('nine_patch')
        chunks = asset.get('png_chunks')
        rows.append((
            uid, scope, str(asset_key), role,
            path,
            db_relative(path, account_dir, output),
            asset.get('width'), asset.get('height'), asset.get('frames'),
            asset.get('sha256'), asset.get('source_url'),
            json.dumps(nine, ensure_ascii=False, sort_keys=True) if nine is not None else None,
            json.dumps(chunks, ensure_ascii=False, sort_keys=True) if chunks is not None else None,
            str(asset['version']) if asset.get('version') is not None else None,
            exists(path, output),
            indexed_at,
        ))

    for bubble_id, info in (decorations.get('bubbles') or {}).items():
        for side, asset in (info.get('sides') or {}).items():
            if isinstance(asset, dict):
                emit('bubble', bubble_id, f'side_{side}', asset)
        for name, asset in (info.get('corners') or {}).items():
            if isinstance(asset, dict):
                emit('bubble', bubble_id, str(name), asset)
        for name, asset in (info.get('backgrounds') or {}).items():
            if isinstance(asset, dict):
                emit('bubble', bubble_id, f'background_{name}', asset)

    for label, asset in (decorations.get('inline_emoji') or {}).items():
        if isinstance(asset, dict):
            emit('inline_emoji', label, 'inline', asset)
    return rows


def ensure_columns(db: sqlite3.Connection, table: str, columns: dict) -> list[str]:
    """Additive migration: add any columns missing from an existing export_ table."""
    existing = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
    added = []
    for name, decl in columns.items():
        if name not in existing:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')
            added.append(name)
    return added


def ensure_schema(db: sqlite3.Connection, uid: str) -> None:
    db.execute(ATTACHMENT_DDL)
    db.execute(BUBBLE_DDL)
    ensure_columns(db, 'export_local_attachment', {
        'kind': 'TEXT', 'status': 'TEXT', 'resource_id': 'TEXT', 'message_count': 'INTEGER',
        'path_rel_export': 'TEXT', 'path_rel_db': 'TEXT', 'duration_seconds': 'REAL',
        'format': 'TEXT', 'container': 'TEXT', 'source_format': 'TEXT',
        'bytes': 'INTEGER', 'source_bytes': 'INTEGER',
        'mime': 'TEXT', 'codec': 'TEXT', 'width': 'INTEGER', 'height': 'INTEGER',
        'frames': 'INTEGER', 'sha256': 'TEXT', 'source_url': 'TEXT', 'exists_on_disk': 'INTEGER',
        'in_msg_table': 'INTEGER', 'msg_type': 'INTEGER', 'conversation_id': 'TEXT',
        'msg_created_time': 'INTEGER', 'msg_sender': 'TEXT', 'indexed_at': 'TEXT',
    })
    for column, table in (
        ('msg_uuid', 'export_local_attachment'),
        ('kind', 'export_local_attachment'),
        ('status', 'export_local_attachment'),
        ('resource_id', 'export_local_attachment'),
        ('asset_scope', 'export_bubble_resource'),
    ):
        index = f'export_idx_{table}_{column}'
        db.execute(f'CREATE INDEX IF NOT EXISTS {index} ON {table} ({column})')
    db.execute(f'DROP VIEW IF EXISTS {VIEW_NAME}')
    db.execute(VIEW_DDL.format(uid=uid))


def upsert(db: sqlite3.Connection, table: str, columns: list[str], rows: list[tuple],
           key: list[str]) -> None:
    if not rows:
        return
    updates = ','.join(f'{c}=excluded.{c}' for c in columns if c not in key)
    sql = (
        f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" * len(columns))}) '
        f'ON CONFLICT({",".join(key)}) DO UPDATE SET {updates}'
    )
    db.executemany(sql, rows)


def index_account(uid: str, manifest: dict, resources: dict, decorations: dict,
                  summary: dict, output: Path, indexed_at: str) -> dict:
    account_dir = output / uid
    db_path = account_dir / ACCOUNT_FILES['database']
    result = {'uid': uid, 'database': str(db_path)}

    db = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        before = msg_fingerprint(db)
        tables_before = sorted(r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))

        manifest_keys = [k.split(':', 1)[1] for k in manifest if k.startswith(uid + ':')]
        msg_meta = msg_lookup(db, manifest_keys)
        attachment_rows = build_attachment_rows(uid, manifest, resources, account_dir, msg_meta, output, indexed_at)
        bubble_rows = decoration_rows(uid, decorations, account_dir, output, indexed_at)

        db.execute('BEGIN IMMEDIATE')
        try:
            ensure_schema(db, uid)
            db.execute('DELETE FROM export_local_attachment WHERE account_uid=?', (uid,))
            db.execute('DELETE FROM export_bubble_resource WHERE account_uid=?', (uid,))
            upsert(db, 'export_local_attachment', ATTACHMENT_COLUMNS, attachment_rows,
                   ['account_uid', 'msg_uuid'])
            upsert(db, 'export_bubble_resource', BUBBLE_COLUMNS, bubble_rows,
                   ['account_uid', 'asset_scope', 'asset_key', 'role'])
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise

        after = msg_fingerprint(db)
        tables_after = sorted(r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        added = [t for t in tables_after if t not in tables_before]
        if not all(t.startswith(PREFIX) for t in added):
            raise ExportError('Media indexing created an unexpected database table.')
        if before != after:
            raise ExportError('Original chat rows changed during media indexing.')

        integrity = db.execute('PRAGMA integrity_check').fetchall()
        stored = db.execute(
            'SELECT COUNT(*) FROM export_local_attachment WHERE account_uid=?', (uid,)).fetchone()[0]
        stored_uuids = {r[0] for r in db.execute(
            'SELECT msg_uuid FROM export_local_attachment WHERE account_uid=?', (uid,))}
        ok_rows = db.execute(
            "SELECT COUNT(*), SUM(exists_on_disk) FROM export_local_attachment "
            "WHERE account_uid=? AND status='ok'", (uid,)).fetchone()
        status_counts = dict(db.execute(
            'SELECT status, COUNT(*) FROM export_local_attachment WHERE account_uid=? '
            'GROUP BY status', (uid,)))
        bubble_stored = db.execute(
            'SELECT COUNT(*) FROM export_bubble_resource WHERE account_uid=?', (uid,)).fetchone()[0]
        view_media = db.execute(
            f'SELECT COUNT(*) FROM {VIEW_NAME} WHERE media_kind IS NOT NULL').fetchone()[0]
        view_all = db.execute(f'SELECT COUNT(*) FROM {VIEW_NAME}').fetchone()[0]

        expected_manifest = {k.split(':', 1)[1] for k in manifest if k.startswith(uid + ':')}
        result.update({
            'msg_rows_unchanged': before == after,
            'msg_rows': before['rows'],
            'msg_fingerprint_sha256': before['sha256'],
            'integrity_check': [r[0] for r in integrity],
            'added_tables': added,
            'manifest_message_entries': len(expected_manifest),
            'indexed_attachment_rows': stored,
            'index_matches_manifest': stored == len(expected_manifest)
                                      and stored_uuids == expected_manifest,
            'attachment_status_counts': status_counts,
            'ok_rows_with_file_on_disk': ok_rows[1] if ok_rows[0] else 0,
            'ok_rows_total': ok_rows[0],
            'indexed_bubble_assets': bubble_stored,
            'decoration_assets_expected': len(bubble_rows),
            'view_rows_total': view_all,
            'view_rows_with_media': view_media,
        })
        if not result['index_matches_manifest']:
            raise ExportError('The SQLite media index does not match the media manifest.')
        if integrity != [('ok',)]:
            raise ExportError('The exported database failed its integrity check after media indexing.')
    finally:
        db.close()
    return result



def index_media(config: ExportConfig) -> dict:
    """Refresh only export_ tables/views in each standalone chat.sqlite."""
    output = config.output
    def read(name, default):
        path = output / name
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    media = read(ROOT_FILES['media_manifest'], {})
    decorations = read(ROOT_FILES['decorations'], {})
    summary = read(ROOT_FILES['summary'], {})
    if not summary.get('accounts'):
        raise ExportError('No exported accounts found. Run export before indexing media.')
    manifest = media.get('messages', {})
    resources = {item['resource_id']: item for item in media.get('resources', [])}
    indexed_at = datetime.now(config.tzinfo).isoformat(timespec='seconds')
    report = {'status': 'passed', 'generated_at': indexed_at, 'accounts': []}
    for account in summary['accounts']:
        uid = str(account['uid'])
        if not uid.isascii() or not uid.isdecimal():
            raise ExportError('Export summary contains an invalid account ID.')
        report['accounts'].append(index_account(uid, manifest, resources, decorations, summary, output, indexed_at))
    write_json(output / 'media_index_results.json', report)
    return report
