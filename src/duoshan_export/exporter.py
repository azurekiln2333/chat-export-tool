"""Export Duoshan chat history from a prepared source into an offline reader tree.

This is the reusable port of the verified analysis-workspace script. It depends
only on this package: the shared ``paths``/``errors`` contracts, the source and
database modules, and ``ReaderMedia``. Nothing here reads from the original
analysis workspace, and no account id, version, path or timestamp is hardcoded.

The public entry points are :func:`export_chats` (first export, needs the
prepared source) and :func:`rerender_export` (rebuild the reader pages from an
existing export, used by the media command when the original backup is gone).
"""

from __future__ import annotations

import base64
import collections
import csv
import hashlib
import html
import json
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Sequence
from urllib.parse import quote, urlsplit

from . import paths
from .errors import ExportError
from .reader_media import ReaderMedia

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExportConfig
    from .source import AccountSource, SourceLayout

__all__ = ['export_chats', 'rerender_export']

# Contact cache decryption parameters of the verified Duoshan Android layout.
CONTACTS_PASSPHRASE = 'relation_user_center_passphrase'
CONTACTS_COMPATIBILITY = 4

# Message type names shown in the reader UI and the _export block.
# Unknown numeric types fall back to "类型 <code>".
TYPE_NAMES = {
    1: '系统提示', 5: '表情', 7: '文字', 8: '分享视频', 15: '打招呼',
    17: '语音', 19: '话题', 21: '直播分享', 22: '音乐分享', 25: '名片',
    26: '分享卡片', 27: '图片', 30: '视频', 60: '链接卡片', 73: '通话记录',
    74: '红包', 77: '分享图文', 83: '回复/引用', 87: '音乐', 90: '直播卡片',
    105: '评论分享', 110: '分享卡片', 114: '评论/引用', 118: '问答',
    122: '红包卡片', 136: '合并转发', 139: '礼物/订单', 142: '活动卡片',
    149: '转账', 151: '转账卡片', 152: '接龙', 168: '分享内容', 502: '位置',
    1001: '系统/内部消息', 1004: '通知', 40001: '无正文记录',
}

# Fields searched, in order, when building a readable summary of a content blob.
CONTENT_TEXT_KEYS = (
    'text', 'tips', 'hint', 'msgHint', 'content_title', 'description', 'desc',
    'title', 'notice_title', 'notice_content', 'voip_main_title', 'voip_sub_title',
    'comment', 'comment_user_name', 'display_name', 'hint_content', 'hello_text',
    'hint_text', 'poi_name', 'poi_address', 'music_name', 'music_author_name', 'name',
)

# Content keys holding avatars/icons whose URLs are not interesting as references.
REFERENCE_URL_SKIP_KEYS = frozenset({'avatar', 'avatar_thumb', 'avatar_small', 'icon'})

AUDIO_SUFFIXES = ('.m4a', '.mp3', '.wav', '.mpeg', '.ogg')

PAGE_SIZE_FALLBACK = 600


# --------------------------------------------------------------------------- util
def dump(value: object, pretty: bool = False) -> str:
    """JSON dump that keeps text intact and never raises on odd column types."""
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2 if pretty else None,
        default=lambda obj: {'_base64': base64.b64encode(obj).decode('ascii')}
        if isinstance(obj, bytes) else str(obj),
    )


def parse(value: object) -> object:
    """Decode a stored JSON column; keep the raw string when it is not JSON."""
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value if isinstance(value, str) else str(value)


def e(value: object) -> str:
    """HTML-escape for attribute or text-node use."""
    return html.escape(str(value if value is not None else ''), quote=True)


def timestamp(value: object, tzinfo: Any) -> str:
    """Format a millisecond epoch value in the configured time zone."""
    if not value:
        return ''
    try:
        return datetime.fromtimestamp(value / 1000, tzinfo).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError, OverflowError, TypeError):
        return str(value)


def csv_value(value: object, identifier: bool = False) -> str:
    """Neutralise spreadsheet formulas and long numeric identifiers."""
    text = str(value if value is not None else '')
    if (identifier and text.isdecimal() and len(text) > 15) \
            or text.lstrip().startswith(('=', '+', '-', '@')) \
            or text.startswith(('\t', '\r')):
        return "'" + text
    return text


def write_csv(path: Path, headings: Sequence[str], rows: Iterable[Sequence[object]],
              id_columns: Sequence[int] = ()) -> None:
    """Write a UTF-8-with-BOM CSV with spreadsheet-safe cells."""
    id_columns = set(id_columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(headings)
        for row in rows:
            writer.writerow([csv_value(v, i in id_columns) for i, v in enumerate(row)])


def read_rows(db: sqlite3.Connection, table: str) -> list[dict]:
    """Read a whole table as dicts with the original column names and values."""
    cursor = db.execute('SELECT * FROM "' + table.replace('"', '""') + '"')
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def read_rows_optional(db: sqlite3.Connection, table: str) -> list[dict]:
    """Read a table if it exists; a schema without it is not an error."""
    found = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return read_rows(db, table) if found else []


def _paths_helper(name: str):
    """Optional helper from the shared paths module."""
    return getattr(paths, name, None)


def _page_size(config: 'ExportConfig') -> int:
    try:
        size = int(getattr(config, 'page_size', 0) or 0)
    except (TypeError, ValueError):
        size = 0
    return size if size > 0 else PAGE_SIZE_FALLBACK


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ source/profile
def _source_module():
    from . import source  # imported lazily so this module stays import-safe
    return source


def _database_module():
    from . import database  # imported lazily so this module stays import-safe
    return database


def _profile_dir(layout: 'SourceLayout') -> Path | None:
    """Directory holding the ``*_aweme_user_info.sgv`` profile caches."""
    explicit = getattr(layout, 'profiles_dir', None)
    if explicit and Path(explicit).is_dir():
        return Path(explicit)
    app_dir = getattr(layout, 'app_dir', None)
    if not app_dir:
        return None
    app_dir = Path(app_dir)
    for parts in (('files', 'keva', 'repo', 'aweme_user'), ('keva', 'repo', 'aweme_user')):
        candidate = app_dir.joinpath(*parts)
        if candidate.is_dir():
            return candidate
    return app_dir.joinpath('files', 'keva', 'repo', 'aweme_user')


def _read_profile(path: Path) -> dict:
    """Read a Duoshan ``*_aweme_user_info.sgv`` profile cache entry."""
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    start = raw.find(b'{')
    if start < 0:
        return {}
    try:
        result, _ = json.JSONDecoder().raw_decode(raw[start:].decode('utf-8', errors='replace'))
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _load_profiles(layout: 'SourceLayout', uids: Iterable[str]) -> dict[str, str]:
    """Profile nicknames for the given ids; empty when the cache is unavailable."""
    directory = _profile_dir(layout)
    if directory is None:
        return {}
    names: dict[str, str] = {}
    for uid in uids:
        profile = _read_profile(directory / f'{uid}_aweme_user_info.sgv')
        nickname = profile.get('nickname')
        if isinstance(nickname, str) and nickname:
            names[str(uid)] = nickname
    return names


class _Names:
    """Resolve a display name: contact cache, then profile, then the UID."""

    def __init__(self, contacts: dict[str, dict], profiles: dict[str, str]) -> None:
        self.contacts = contacts
        self.profiles = profiles

    def name(self, user_id: object) -> str:
        uid = str(user_id or '')
        if not uid:
            return '未知发送者'
        row = self.contacts.get(uid) or {}
        return (
            row.get('remark_name')
            or row.get('nickname')
            or self.profiles.get(uid)
            or ('UID ' + uid)
        )


def _contacts_map(rows: Iterable[dict]) -> dict[str, dict]:
    return {str(row.get('uid')): row for row in rows}


# --------------------------------------------------------------- content/readable
def readable_content(msg_type: object, content: object) -> str:
    """A readable display summary of a message content blob (Simplified-Chinese labels)."""
    label = TYPE_NAMES.get(msg_type, f'类型 {msg_type}')
    if not isinstance(content, dict):
        return content if isinstance(content, str) else dump(content)
    if msg_type == 1 and content.get('tips'):
        text = content['tips']
        for item in content.get('template') or []:
            if isinstance(item, dict):
                text = text.replace('{{' + str(item.get('key', '')) + '}}',
                                    str(item.get('name', '')))
        return text
    values: list[str] = []
    for key in CONTENT_TEXT_KEYS:
        value = content.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in values:
            values.append(value.strip())
    if msg_type == 17:
        duration = content.get('duration')
        if isinstance(duration, (int, float)):
            values.insert(0, f'语音 {duration / 1000:g} 秒')
        transcription = content.get('ai_audio_text')
        if isinstance(transcription, str) and transcription:
            values.append('转写：' + transcription)
    if msg_type in {8, 77, 168} and content.get('content_name'):
        values.append('作者：' + str(content['content_name']))
    if msg_type == 30 and content.get('duration'):
        values.insert(0, '视频 ' + str(content['duration']) + ' 秒')
    if not values:
        return '[' + label + ']'
    text = '\n'.join(values)
    return text if msg_type in {1, 7} else '[' + label + '] ' + text


def urls_in(value: object) -> list[str]:
    """Collect distinct http(s) references from a content blob."""
    found: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key not in REFERENCE_URL_SKIP_KEYS:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and item.startswith(('http://', 'https://')):
            if item not in found:
                found.append(item)

    visit(value)
    return found


# -------------------------------------------------------------------- local media
def _attachment_records_from_summary(output: Path) -> list[dict]:
    """Local-media records written by the previous pass, if any."""
    summary = _read_json(output / paths.ROOT_FILES['summary'])
    records = summary.get('local_media_files')
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _tokens_for(name: str, digest: str) -> set[str]:
    """Matching tokens; short names are dropped so they cannot match by chance."""
    return {token for token in (name.lower(), Path(name).stem.lower(), digest)
            if len(token) >= 16}


def _english_path(value: str) -> str:
    helper = _paths_helper('english_relative_path')
    return helper(value) if helper else value


def _copy_local_media(app_dir: Path | None, output: Path,
                      im_dir: Path | None = None) -> tuple[list[dict], dict[str, str]]:
    """Copy the backup's ``files/im`` tree into ``attachments`` and index it.

    Returns the copied records and the token lookup used to match a message row
    to its local attachment. Matching keys are the file name, the stem and the
    MD5 digest, as in the verified workflow; only tokens of 16+ characters are
    used so short names cannot match unrelated text.
    """
    attachments = paths.DIRECTORIES['attachments']
    media_dir = (Path(im_dir) if im_dir else
                 ((Path(app_dir) / 'files' / 'im') if app_dir else None))
    records: list[dict] = []
    lookup: dict[str, str] = {}
    if media_dir is None or not media_dir.is_dir():
        return records, lookup
    for source in sorted(media_dir.rglob('*')):
        if not source.is_file():
            continue
        relative = source.relative_to(media_dir)
        exported = _english_path(attachments + '/' + relative.as_posix())
        destination = output / exported
        if not destination.suffix:
            try:
                with source.open('rb') as handle:
                    head = handle.read(16)
            except OSError:
                head = b''
            suffix = '.mp3' if head[:3] == b'ID3' or head[:2] in (b'\xff\xfb', b'\xff\xf3') else '.bin'
            destination = destination.with_name(destination.name + suffix)
            exported = destination.relative_to(output).as_posix()
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        try:
            digest = hashlib.md5(source.read_bytes()).hexdigest()
        except OSError:
            digest = ''
        records.append({'source': relative.as_posix(), 'exported': exported,
                        'size': source.stat().st_size})
        for token in _tokens_for(source.name, digest):
            lookup[token] = exported
    return records, lookup


def _lookup_from_records(output: Path, records: Sequence[dict]) -> dict[str, str]:
    """Rebuild the media lookup from an existing export without the backup."""
    lookup: dict[str, str] = {}
    for record in records:
        exported = str(record.get('exported') or '')
        if not exported:
            continue
        path = output / exported
        if not path.is_file():
            continue
        try:
            digest = hashlib.md5(path.read_bytes()).hexdigest()
        except OSError:
            digest = ''
        for token in _tokens_for(path.name, digest):
            lookup[token] = exported
    return lookup


def _lookup_from_attachment_tree(output: Path) -> dict[str, str]:
    """Fallback lookup that scans an existing attachments tree."""
    attachments = output / paths.DIRECTORIES['attachments']
    if not attachments.is_dir():
        return {}
    records = [{'exported': path.relative_to(output).as_posix()}
               for path in sorted(attachments.rglob('*')) if path.is_file()]
    return _lookup_from_records(output, records)


def match_media(raw: dict, lookup: dict[str, str]) -> list[str]:
    """Local attachments whose token appears anywhere in the raw row."""
    if not lookup:
        return []
    search = dump({
        'content': raw.get('content'),
        'local_info': raw.get('local_info'),
        'ext': raw.get('ext'),
    }).lower()
    return sorted({destination for token, destination in lookup.items() if token in search})


# ------------------------------------------------------------------------ html
def doc(title: object, body: str, depth: int = 0, search: bool = False) -> str:
    """Wrap page content in the offline reader document with its CSP."""
    prefix = '../' * depth
    search_script = '''<script>document.querySelector('#filter')?.addEventListener('input',function(){const q=this.value.toLocaleLowerCase();for(const row of document.querySelectorAll('[data-search]'))row.hidden=!row.dataset.search.toLocaleLowerCase().includes(q);});document.addEventListener('play',function(event){if(event.target.tagName==='AUDIO')for(const audio of document.querySelectorAll('audio'))if(audio!==event.target)audio.pause();},true);document.addEventListener('error',function(event){if(event.target.tagName==='AUDIO'&&!event.target.nextElementSibling?.classList.contains('playback-error')){const text=document.createElement('small');text.className='playback-error';text.textContent='浏览器无法播放此文件，请用下方的附件链接在本地打开。';event.target.after(text);}},true);</script>''' if search else ''
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        'style-src \'self\' \'unsafe-inline\' file:; script-src \'unsafe-inline\'; '
        'img-src \'self\' data: file:; media-src \'self\' file:; base-uri \'none\'; '
        'form-action \'none\'"><title>' + e(title) + '</title>'
        '<link rel="stylesheet" href="' + prefix + 'reader.css"></head><body><main>'
        + body + '</main>' + search_script + '</body></html>'
    )


def navigation(page_files: Sequence[str], index: int) -> str:
    """First/Previous/Next/Latest links plus the current page position."""
    links = []
    for target, label in ((0, '最早'), (index - 1, '上一页'), (index + 1, '下一页'),
                          (len(page_files) - 1, '最新')):
        if 0 <= target < len(page_files) and target != index:
            links.append('<a href="' + quote(page_files[target]) + '">' + label + '</a>')
    return ('<nav>' + ' · '.join(links)
            + f'<span> 第 {index + 1} / {len(page_files)} 页</span></nav>')


def render_message(msg: dict, uid: str, reader: ReaderMedia) -> str:
    """One message article: header, bubble/media render, attachments, raw block."""
    raw, meta = msg['raw'], msg['meta']
    classes = 'message own' if str(raw.get('sender')) == uid else 'message'
    deleted = (' <span class="badge warning">删除标记 ' + e(raw['deleted']) + '</span>'
               if raw.get('deleted') else '')
    body = (
        '<article class="' + classes + '" id="msg-' + e(raw.get('msg_uuid'))
        + '" data-search="' + e(str(meta.get('sender_name', '')) + ' ' + str(meta.get('text', '')))
        + '"><header><strong>'
        + e(('我 · ' if str(raw.get('sender')) == uid else '') + str(meta.get('sender_name', '')))
        + '</strong><span>' + e(meta.get('time')) + '</span><span class="badge">'
        + e(meta.get('type_name')) + '</span>' + deleted + '</header>'
        + reader.render(msg)
    )
    media = meta.get('media') or {}
    for resource in meta.get('local_media') or []:
        url = '../../' + quote(resource)
        if resource != media.get('path') and resource.lower().endswith(AUDIO_SUFFIXES):
            body += '<audio controls preload="none" src="' + url + '"></audio>'
        label = '打开语音文件' if media.get('kind') == 'voice' else '打开本地附件'
        body += '<p class="attachment-link"><a href="' + url + '">' + label + '</a></p>'
    links = urls_in(msg.get('content'))
    if links:
        body += '<details class="links"><summary>原始网络地址</summary><ul>'
        for index, url in enumerate(links[:8], 1):
            body += ('<li><a rel="noreferrer noopener" target="_blank" href="' + e(url) + '">'
                     + e(f'链接 {index} · ' + urlsplit(url).netloc) + '</a></li>')
        body += '</ul></details>'
    bubble = meta.get('bubble') or {}
    decoration = ''
    if bubble.get('id') not in (None, '-1'):
        if bubble.get('applied'):
            state = ' · 已使用原始素材'
        elif not bubble.get('available'):
            state = ' · 备份未留存此款素材'
        else:
            state = ' · 此类消息不套气泡'
        decoration = '<small>气泡 ID：' + e(bubble['id']) + state + '</small>'
    body += ('<details><summary>原始消息内容 · ' + e(raw.get('msg_uuid', ''))
             + '</summary>' + decoration + '<pre>' + e(dump(msg['content'], pretty=True))
             + '</pre></details></article>')
    return body


# ------------------------------------------------------------------------ style
STYLE = '''*{box-sizing:border-box}body{margin:0;background:#f3f5f8;color:#202734;font-family:system-ui,"Microsoft YaHei",sans-serif;font-size:15px;line-height:1.65}main{max-width:1040px;margin:36px auto;padding:30px;background:white;border:1px solid #e3e7ee;border-radius:16px}h1{font-size:27px;line-height:1.35;margin:16px 0}h2{font-size:20px;margin:0 0 10px}a{color:#275dbb;text-decoration:none}a:hover{text-decoration:underline}.muted,small{color:#687387}small{display:block;font-size:11px;overflow-wrap:anywhere}.back{font-size:14px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px;margin:24px 0}.card{padding:24px;border:1px solid #dce3ed;border-radius:12px;background:#f8faff}.big{font-size:30px;font-weight:650}.downloads{display:flex;flex-wrap:wrap;gap:18px;margin:18px 0}.notice{background:#fff8e8;border-left:3px solid #d7a947;padding:14px 18px;font-size:14px}.toolbar{display:flex;align-items:center;gap:20px}input{width:min(100%,480px);padding:11px 14px;border:1px solid #cad3e1;border-radius:8px;font:inherit;margin:12px 0}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;text-align:left}th,td{padding:14px 10px;border-bottom:1px solid #e5e9f0;vertical-align:top}th{background:#f6f8fb;font-size:13px;white-space:nowrap}.dates{white-space:nowrap;font-size:12px}nav{display:flex;flex-wrap:wrap;gap:8px;align-items:center;position:relative;padding:14px 0;border-bottom:1px solid #e4e8f0;margin-bottom:18px}nav span{margin-left:auto;color:#6c7788}.message{padding:18px 0;border-bottom:1px solid #edf0f5;max-width:94%;overflow-wrap:anywhere}.message.own{margin-left:auto}.message header{display:flex;flex-wrap:wrap;align-items:center;gap:10px;font-size:12px;color:#687387}.message header strong{color:#283750;font-size:14px}.bubble{padding:11px 14px;background:#f2f4f8;border-radius:8px;white-space:pre-wrap;margin:8px 0}.own .bubble{background:#eaf2ff}.badge{background:#e8edf4;padding:1px 6px;border-radius:4px;font-size:11px}.warning{color:#92521b;background:#fff0db}details{font-size:12px;color:#667184;margin:6px 0}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:450px;overflow:auto;background:#f5f7fa;padding:12px;border-radius:6px;color:#424e62;font:12px/1.6 ui-monospace,Consolas,monospace}audio{display:block;width:min(100%,360px);margin:8px 0}[hidden]{display:none!important}@media(max-width:680px){main{margin:0;border:0;border-radius:0;padding:18px}h1{font-size:23px}.message{max-width:100%}.toolbar{display:block}th,td{padding:10px 6px}.cards{grid-template-columns:1fr}}'''

# Bubble rendering: the nine-patch base is drawn by ::before (z-index -1), the
# animated corner artwork sits above it at z-index 0 and the text stays on top at
# z-index 1 so the artwork can never obscure the message text.
STYLE += '''
.message{display:flex;flex-direction:column;max-width:100%;padding:17px 0;border-bottom:0}.message header{margin-bottom:5px}.own header{justify-content:flex-end}.bubble{position:relative;align-self:flex-start;max-width:min(82%,690px);width:fit-content;white-space:normal;margin:4px 0 7px;overflow:visible;isolation:isolate}.own .bubble{align-self:flex-end}.bubble-content{position:relative;z-index:1;white-space:pre-wrap;overflow-wrap:anywhere}.original-bubble,.own .original-bubble{background:transparent;border-radius:0}.original-bubble::before{content:"";position:absolute;inset:0;z-index:-1;pointer-events:none}.bubble audio{width:300px;max-width:100%;min-width:200px;margin:7px 0 0}.inline-emoji{height:1.55em;width:1.55em;object-fit:contain;vertical-align:-.38em;display:inline-block}.bubble.sticker-bubble{padding:0;background:transparent;border-radius:0}.sticker-link{display:block;line-height:0}.sticker{display:block;max-width:230px;max-height:230px;width:auto;height:auto;object-fit:contain}.bubble-corner{position:absolute;z-index:2;pointer-events:none;object-fit:contain}.corner-top-left{left:0;top:0}.corner-top-right{right:0;top:0}.corner-bottom-left{left:0;bottom:0}.corner-bottom-right{right:0;bottom:0}.message>details{max-width:88%;align-self:flex-start;margin:2px 0}.message.own>details{align-self:flex-end;text-align:right}.message.own>details[open]{text-align:left;width:88%}.attachment-link{margin:0;font-size:12px;align-self:flex-start}.own .attachment-link{align-self:flex-end}.reader-note{color:#687387;font-size:13px}.media-unavailable,.playback-error{margin-top:6px;color:#8b633d}.sticker-bubble .bubble-content{white-space:normal}@media(max-width:680px){.bubble{max-width:94%}.sticker{max-width:190px;max-height:190px}.bubble audio{width:240px;min-width:190px}.message>details{max-width:98%}.message.own>details[open]{width:98%}}
'''


# ------------------------------------------------------------------- db reading
@contextmanager
def _open_exported_database(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an exported plaintext database read-only and always close it.

    ``with sqlite3.connect(...)`` only wraps a transaction and leaves the file
    handle open, which keeps the file locked on Windows and breaks temp/output
    cleanup. The connection is therefore closed explicitly here, so every read
    handle in this module is released deterministically. Originals are never
    written: the database is opened with ``mode=ro``.
    """
    absolute = Path(path).resolve()
    if not absolute.is_file():
        raise ExportError('Exported database is missing: ' + str(absolute))
    db = sqlite3.connect(absolute.as_uri() + '?mode=ro', uri=True)
    try:
        yield db
    finally:
        db.close()


def _load_account_tables(db_path: Path) -> dict:
    """Read the tables a reader page needs; missing tables are tolerated."""
    with _open_exported_database(Path(db_path)) as db:
        core = {row['conversation_id']: row for row in read_rows_optional(db, 'conversation_core')}
        conv_rows = {row['conversation_id']: row for row in read_rows_optional(db, 'conversation_list')}
        participants = read_rows_optional(db, 'participant')
        messages = read_rows_optional(db, 'msg')
        attachments = read_rows_optional(db, 'attchment')
    return {
        'conversation_core': core,
        'conversation_list': conv_rows,
        'participant': participants,
        'messages': messages,
        'attchment': attachments,
    }


# ------------------------------------------------------------------ account pass
class _AccountState:
    """Everything needed to render one account into the output tree."""

    def __init__(self, uid: str, db_path: Path, contacts_rows: list[dict],
                 contacts_status: str, own_name: str, names: _Names) -> None:
        self.uid = uid
        self.db_path = Path(db_path)
        self.contacts_rows = contacts_rows
        self.contacts_status = contacts_status
        self.own_name = own_name
        self.names = names


def _conversation_name(cid: object, uid: str, info: dict, conv_type: object,
                       names: _Names) -> str:
    """Private-chat name from the peer id, otherwise the stored/group name."""
    cid_text = str(cid)
    if cid_text.startswith('0:1:'):
        peer_ids = [part for part in cid_text.split(':')[2:]
                    if part.isdecimal() and part != uid]
        if peer_ids:
            return ' / '.join(names.name(peer) for peer in peer_ids)
    return info.get('name') or ('群聊 ' if conv_type == 2 else '会话 ') + cid_text


# Recognised generated reader files. Only names matching these are ever pruned,
# so a page-size change cannot leave duplicate HTML and no user file is touched.
_CONVERSATION_PAGE_RE = re.compile(r'^conversation_\d+_page_\d+\.html$')
_CONVERSATION_TEXT_RE = re.compile(r'^conversation_\d+\.txt$')


def _prune_generated_files(directory: Path, pattern: 're.Pattern[str]',
                          expected: set[str]) -> int:
    """Delete superseded generated files for this account; return the count.

    A smaller ``page_size`` writes more pages but a larger one writes fewer, so
    pages from the previous run must go. Files are compared by exact name after
    a strict pattern match, and the whole account's expected set is known before
    pruning, so unrelated files are never removed.
    """
    if not directory.is_dir():
        return 0
    removed = 0
    for candidate in sorted(directory.iterdir()):
        if not candidate.is_file() or candidate.name in expected:
            continue
        if not pattern.fullmatch(candidate.name):
            continue
        try:
            candidate.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def _render_account(config: 'ExportConfig', reader: ReaderMedia, output: Path,
                    state: _AccountState, media_lookup: dict[str, str]) -> dict:
    """Write the full per-account output tree and return its summary.

    The database must already exist at ``state.db_path``; this function only
    reads it, so the same code path serves the first export and a rerender.
    """
    tzinfo = config.tzinfo
    tz_label = getattr(config, 'timezone', 'UTC') or 'UTC'
    page_size = _page_size(config)
    uid = state.uid
    tables = _load_account_tables(state.db_path)
    core = tables['conversation_core']
    conv_rows = tables['conversation_list']
    participants = tables['participant']
    message_rows = tables['messages']

    account_dir = output / uid
    pages_dir = account_dir / paths.DIRECTORIES['pages']
    text_dir = account_dir / paths.DIRECTORIES['transcripts']
    pages_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    (account_dir / paths.ACCOUNT_FILES['contacts_json']).write_text(
        dump(state.contacts_rows, pretty=True), encoding='utf-8')
    write_csv(
        account_dir / paths.ACCOUNT_FILES['contacts_csv'],
        ['User ID', 'Nickname', 'Remark', 'Douyin ID', 'Short ID', 'Deleted'],
        ([row.get(k, '') for k in
          ('uid', 'nickname', 'remark_name', 'unique_id', 'short_id', 'deleted')]
         for row in state.contacts_rows),
        (0, 4),
    )
    (account_dir / paths.ACCOUNT_FILES['conversations']).write_text(
        dump({
            'conversation_core': list(core.values()),
            'conversation_list': list(conv_rows.values()),
            'participant': participants,
            'attchment': tables['attchment'],
        }, pretty=True), encoding='utf-8')

    aliases = {(str(p.get('conversation_id')), str(p.get('user_id'))): p['alias']
               for p in participants if p.get('alias')}
    groups: dict[Any, list[dict]] = collections.defaultdict(list)
    for row in message_rows:
        groups[row.get('conversation_id')].append(row)
    ordered = sorted(
        groups,
        key=lambda cid: max((row.get('created_time') or 0) for row in groups[cid]),
        reverse=True,
    )

    conv_summary: list[dict] = []
    type_counts: collections.Counter = collections.Counter()
    deleted_count = local_matches = 0
    expected_pages: set[str] = set()
    expected_texts: set[str] = set()
    basename_helper = _paths_helper('conversation_basename')
    page_helper = _paths_helper('conversation_page')
    jsonl_path = account_dir / paths.ACCOUNT_FILES['messages_jsonl']
    csv_path = account_dir / paths.ACCOUNT_FILES['messages_csv']
    with jsonl_path.open('w', encoding='utf-8', newline='\n') as jf, \
            csv_path.open('w', encoding='utf-8-sig', newline='') as cf:
        writer = csv.writer(cf)
        writer.writerow(['Account UID', 'Conversation', 'Conversation ID',
                         f'Time ({tz_label})', 'Sender', 'Sender ID', 'Message Type',
                         'Type Code', 'Text', 'Deleted', 'Message UUID',
                         'Server Message ID', 'Local Attachments'])
        for conv_index, cid in enumerate(ordered, 1):
            rows = sorted(
                groups[cid],
                key=lambda row: (row.get('created_time') or 0, row.get('order_index') or 0,
                                 str(row.get('msg_uuid'))),
            )
            info = core.get(cid, {}) or {}
            conv_type = ((conv_rows.get(cid, {}) or {}).get('type')
                         or rows[0].get('conversation_type'))
            conv_name = _conversation_name(cid, uid, info, conv_type, state.names)
            base_name = basename_helper(conv_index) if basename_helper else f'conversation_{conv_index:03d}'
            page_count = max(1, (len(rows) + page_size - 1) // page_size)
            page_files = [page_helper(conv_index, p + 1) if page_helper
                          else f'{base_name}_page_{p + 1:03d}.html'
                          for p in range(page_count)]
            page_messages: list[dict] = []
            text_path = text_dir / (base_name + '.txt')
            expected_pages.update(page_files)
            expected_texts.add(text_path.name)
            with text_path.open('w', encoding='utf-8', newline='\n') as tf:
                tf.write(f'会话：{conv_name}\n')
                tf.write(f'账号：{state.own_name} ({uid})\n')
                tf.write(f'会话ID：{cid}\n')
                tf.write(f'时区：{tz_label}\n')
                tf.write(f'记录数：{len(rows)}\n\n')
                for raw in rows:
                    content = parse(raw.get('content'))
                    sender_id = str(raw.get('sender') or '')
                    sender_name = aliases.get((cid, sender_id)) or state.names.name(sender_id)
                    text = readable_content(raw.get('type'), content)
                    local_media = match_media(raw, media_lookup)
                    meta = {
                        'account_uid': uid,
                        'conversation_name': conv_name,
                        'time': timestamp(raw.get('created_time'), tzinfo),
                        'timezone': tz_label,
                        'sender_name': sender_name,
                        'type_name': TYPE_NAMES.get(raw.get('type'), f"类型 {raw.get('type')}"),
                        'text': text,
                        'local_media': local_media,
                    }
                    reader.enrich(meta, raw, uid)
                    jf.write(dump({**raw, '_export': meta}) + '\n')
                    normalized = [uid, conv_name, cid, meta['time'], sender_name, sender_id,
                                  meta['type_name'], raw.get('type'), text, raw.get('deleted'),
                                  raw.get('msg_uuid'), raw.get('msg_server_id'),
                                  ' | '.join(local_media)]
                    writer.writerow([csv_value(v, i in {0, 2, 5, 11})
                                     for i, v in enumerate(normalized)])
                    flag = ' [删除标记]' if raw.get('deleted') else ''
                    tf.write(f"{meta['time']}  {sender_name} ({sender_id}){flag}\n{text}\n\n")
                    page_messages.append({'raw': raw, 'meta': meta, 'content': content})
                    type_counts[raw.get('type')] += 1
                    deleted_count += bool(raw.get('deleted'))
                    local_matches += bool(local_media)

            for page_index, start in enumerate(range(0, len(page_messages), page_size)):
                chunk = page_messages[start:start + page_size]
                nav = navigation(page_files, page_index)
                body = ('<a class="back" href="../index.html">← 会话列表</a><h1>'
                        + e(conv_name) + '</h1><p class="muted">账号 ' + e(state.own_name)
                        + ' · ' + e(uid) + '<br>会话 ' + e(cid)
                        + f'<br>{len(rows):,} 条记录 · ' + e(tz_label) + '</p>')
                body += ('<p class="reader-note">语音点 ▶ 播放，表情和已恢复的气泡素材均从本地读取。</p>'
                         '<div class="toolbar"><input id="filter" type="search" '
                         'placeholder="筛选本页正文或发送者"><a href="../'
                         + paths.DIRECTORIES['transcripts'] + '/' + quote(text_path.name)
                         + '">完整会话 TXT</a></div>' + nav)
                body += ''.join(render_message(message, uid, reader) for message in chunk) + nav
                (pages_dir / page_files[page_index]).write_text(
                    doc(conv_name, body, depth=2, search=True), encoding='utf-8')

            conv_summary.append({
                'conversation_id': cid, 'name': conv_name, 'count': len(rows),
                'first': timestamp(rows[0].get('created_time'), tzinfo),
                'last': timestamp(rows[-1].get('created_time'), tzinfo),
                'page': paths.DIRECTORIES['pages'] + '/' + page_files[0],
                'latest_page': paths.DIRECTORIES['pages'] + '/' + page_files[-1],
                'txt': paths.DIRECTORIES['transcripts'] + '/' + text_path.name,
                'html_pages': page_count,
            })

    _prune_generated_files(pages_dir, _CONVERSATION_PAGE_RE, expected_pages)
    _prune_generated_files(text_dir, _CONVERSATION_TEXT_RE, expected_texts)

    write_csv(
        account_dir / paths.ACCOUNT_FILES['index_csv'],
        ['Conversation', 'Conversation ID', 'Messages', f'First Time ({tz_label})',
         f'Last Time ({tz_label})'],
        ([c['name'], c['conversation_id'], c['count'], c['first'], c['last']]
         for c in conv_summary),
        (1,),
    )
    title = state.own_name + ' - 聊天记录'
    body = ('<a class="back" href="../index.html">← 全部账号</a><h1>' + e(title)
            + '</h1><p class="muted">账号 ID：' + e(uid) + ' · '
            + f'{len(message_rows):,} 条记录 · {len(conv_summary)} 个有消息的会话</p>'
            '<div class="downloads"><a href="' + paths.ACCOUNT_FILES['messages_csv']
            + '">消息 CSV</a><a href="' + paths.ACCOUNT_FILES['messages_jsonl']
            + '">完整消息 JSONL</a><a href="' + paths.ACCOUNT_FILES['database']
            + '">解密数据库</a><a href="' + paths.ACCOUNT_FILES['contacts_csv']
            + '">联系人 CSV</a></div><input id="filter" type="search" '
            'placeholder="搜索会话名称或 ID"><div class="table-wrap"><table>'
            '<thead><tr><th>会话</th><th>记录数</th><th>时间范围</th>'
            '<th>打开</th></tr></thead><tbody>')
    for item in conv_summary:
        body += ('<tr data-search="' + e(item['name'] + ' ' + item['conversation_id'])
                 + '"><td><strong>' + e(item['name']) + '</strong><small>'
                 + e(item['conversation_id']) + '</small></td><td>' + f"{item['count']:,}"
                 + '</td><td class="dates">' + e(item['first']) + '<br>' + e(item['last'])
                 + '</td><td><a href="' + quote(item['page']) + '">最早</a> · <a href="'
                 + quote(item['latest_page']) + '">最新</a> · <a href="' + quote(item['txt'])
                 + '">TXT</a></td></tr>')
    body += '</tbody></table></div>'
    (account_dir / 'index.html').write_text(doc(title, body, depth=1, search=True),
                                            encoding='utf-8')

    times = [row.get('created_time') for row in message_rows if row.get('created_time')]
    summary = {
        'uid': uid,
        'nickname': state.own_name,
        'message_count': len(message_rows),
        'conversation_count': len(conv_summary),
        'text_message_count': type_counts[7],
        'deleted_flag_count': deleted_count,
        'first': timestamp(min(times), tzinfo) if times else '',
        'last': timestamp(max(times), tzinfo) if times else '',
        'contact_cache_rows': len(state.contacts_rows),
        'contact_cache_status': state.contacts_status,
        'local_media_matched_messages': local_matches,
        'message_types': dict(type_counts),
        'conversations': conv_summary,
    }
    (account_dir / paths.ACCOUNT_FILES['statistics']).write_text(
        dump(summary, pretty=True), encoding='utf-8')
    return summary


# --------------------------------------------------------------- account discovery
def _select_accounts(config: 'ExportConfig', layout: 'SourceLayout') -> list['AccountSource']:
    """Accounts to export: the configured subset, else every discovered one."""
    source = _source_module()
    discovered = list(source.discover_accounts(layout))
    if not discovered:
        raise ExportError('No Duoshan accounts were found under ' + str(getattr(layout, 'source', '')))
    wanted = tuple(str(uid) for uid in (getattr(config, 'accounts', ()) or ()))
    if not wanted:
        return discovered
    by_uid = {str(account.uid): account for account in discovered}
    missing = [uid for uid in wanted if uid not in by_uid]
    if missing:
        available = ', '.join(sorted(by_uid)) or 'none'
        raise ExportError('Requested account(s) not found in the backup: ' + ', '.join(missing)
                          + '. Available accounts: ' + available)
    return [by_uid[uid] for uid in wanted]


def _decrypt_account(config: 'ExportConfig', layout: 'SourceLayout',
                     account: 'AccountSource', destination: Path) -> None:
    """Decrypt one account into a standalone SQLite file (source stays untouched)."""
    database = _database_module()
    destination.parent.mkdir(parents=True, exist_ok=True)
    database.decrypt_database(
        account.database,
        account.uid,
        destination,
        layout.work_dir,
        sqlcipher_module_path=getattr(config, 'sqlcipher_module_path', None),
    )
    if not destination.is_file():
        raise ExportError('Decryption did not produce a database for account '
                          + str(account.uid))


def _read_contacts(config: 'ExportConfig', layout: 'SourceLayout',
                   account: 'AccountSource') -> tuple[list[dict], str]:
    """Contact cache rows, or an empty list when the cache is missing/unreadable."""
    contacts_db = getattr(account, 'contacts_database', None)
    if not contacts_db:
        return [], 'missing'
    database = _database_module()
    try:
        with database.open_database(
            contacts_db,
            account.uid,
            layout.work_dir,
            password=CONTACTS_PASSPHRASE,
            compatibility=CONTACTS_COMPATIBILITY,
            sqlcipher_module_path=getattr(config, 'sqlcipher_module_path', None),
        ) as db:
            return read_rows_optional(db, 'user'), 'ok'
    except Exception as error:  # a missing or unreadable cache still exports
        return [], 'unavailable: ' + str(error).splitlines()[0][:160]


def _state_for_export(config: 'ExportConfig', layout: 'SourceLayout',
                      account: 'AccountSource', output: Path,
                      profiles: dict[str, str]) -> _AccountState:
    """Decrypt the account, load contacts and build its name resolver."""
    uid = str(account.uid)
    db_path = output / uid / paths.ACCOUNT_FILES['database']
    _decrypt_account(config, layout, account, db_path)
    contacts_rows, status = _read_contacts(config, layout, account)
    contacts = _contacts_map(contacts_rows)
    if uid not in contacts and profiles.get(uid):
        contacts[uid] = {'uid': uid, 'nickname': profiles[uid], 'remark_name': ''}
    names = _Names(contacts, profiles)
    return _AccountState(uid, db_path, contacts_rows, status, names.name(uid), names)


def _state_from_export(output: Path, uid: str) -> _AccountState:
    """Rebuild an account state from an existing export (no original backup)."""
    account_dir = output / uid
    contacts_rows = _read_json_list(account_dir / paths.ACCOUNT_FILES['contacts_json'])
    statistics = _read_json(account_dir / paths.ACCOUNT_FILES['statistics'])
    contacts = _contacts_map(contacts_rows)
    own_name = statistics.get('nickname') or ''
    if uid not in contacts and own_name:
        contacts[uid] = {'uid': uid, 'nickname': own_name, 'remark_name': ''}
    names = _Names(contacts, {})
    return _AccountState(
        uid,
        account_dir / paths.ACCOUNT_FILES['database'],
        contacts_rows,
        'reused from export',
        own_name or names.name(uid),
        names,
    )


def _read_json_list(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


# ---------------------------------------------------------------- presentation
def _app_version(layout: 'SourceLayout | None', previous: dict | None = None) -> str:
    """Best-effort app version: prepared source first, then a previous export.

    Never a fixed constant. When neither source knows it the value stays
    ``unknown`` so the gap is visible instead of being invented.
    """
    if layout is not None:
        for attribute in ('app_version', 'appVersion', 'version'):
            value = getattr(layout, attribute, None)
            if value:
                return str(value)
        candidates: list[Path] = []
        for base in (getattr(layout, 'app_dir', None), getattr(layout, 'source', None)):
            if base:
                candidates.extend([Path(base) / 'app_version.txt', Path(base) / 'version.txt'])
        for candidate in candidates:
            try:
                text = candidate.read_text(encoding='utf-8').strip()
            except OSError:
                continue
            if text:
                return text.splitlines()[0]
    if previous:
        value = previous.get('app_version')
        if value and str(value) != 'unknown':
            return str(value)
    return 'unknown'


def _write_root_page(config: 'ExportConfig', reader: ReaderMedia, output: Path,
                     summaries: Sequence[dict], layout: 'SourceLayout | None',
                     local_media_records: Sequence[dict],
                     previous_summary: dict | None = None) -> dict:
    """Write reader.css, the root index page, the summary and the README."""
    tz_label = getattr(config, 'timezone', 'UTC') or 'UTC'
    page_size = _page_size(config)
    (output / 'reader.css').write_text(STYLE + '\n' + reader.css(), encoding='utf-8')
    previous = previous_summary or {}

    total = sum(item['message_count'] for item in summaries)
    text_total = sum(item['text_message_count'] for item in summaries)
    presentation = reader.statistics()
    voice = (presentation['media'].get('voice') or {})
    emoji = (presentation['media'].get('emoji') or {})
    styles = presentation['original_bubble_styles']
    inline_assets = presentation['inline_emoji_assets']
    inline_occ = presentation['rendered'].get('inline_emoji_occurrences', 0)
    bubble_msgs = presentation['rendered'].get('original_bubble_messages', 0)
    bubble_missing = presentation['messages_missing_original_bubble_asset']
    app_version = _app_version(layout, previous)
    firsts = [item['first'] for item in summaries if item['first']]
    lasts = [item['last'] for item in summaries if item['last']]
    date_from = min(firsts) if firsts else ''
    date_to = max(lasts) if lasts else ''
    exported_at = datetime.now(config.tzinfo).isoformat()
    source = getattr(layout, 'source', None) if layout is not None else None
    if source is None:
        # A rerender has no layout, so keep the source recorded by the export.
        source = previous.get('source') or None

    body = ('<p class="muted">多闪 · Android 本地备份</p><h1>多闪聊天记录</h1>'
            + f'<p>已从 {len(summaries)} 个账号导出 <strong>{total:,}</strong> 条本地消息。'
            + f'所有时间均为 {e(tz_label)}。</p><div class="cards">')
    for item in summaries:
        body += ('<section class="card"><h2>' + e(item['nickname']) + '</h2><small>账号 '
                 + e(item['uid']) + '</small><p class="big">' + f"{item['message_count']:,}"
                 + '<small>条消息 · ' + str(item['conversation_count'])
                 + ' 个会话</small></p><p>' + e(item['first']) + '<br>至 ' + e(item['last'])
                 + '</p><a href="' + quote(str(item['uid']))
                 + '/index.html">打开聊天记录 →</a></section>')
    body += ('</div><div class="notice">语音已保存 '
             + f"{voice.get('messages_downloaded', 0):,} / {voice.get('message_total', 0):,}"
             + ' 条，打开会话后点 ▶ 播放。表情已补齐 '
             + f"{emoji.get('messages_downloaded', 0):,} / {emoji.get('message_total', 0):,}"
             + ' 条消息，保留动图；已恢复 ' + str(styles)
             + ' 款原始气泡，以及文字内的小表情。以上资源均从本地读取，离线可用。'
             '未能下载的媒体会显示缺失原因。<br>补充的语音、表情和气泡为可选项；'
             '其他图片、视频和分享卡片保留备份中的文件或原始引用。</div>'
             '<p class="muted">每个账号都有完整 SQLite 数据库，另提供 CSV、JSONL 和按会话 TXT。'
             '导出包含系统提示和数据库删除标记记录。昵称来自备份时的缓存，可能与历史昵称不同。'
             '详情见同目录的 ' + paths.ROOT_FILES['readme'] + '。</p>')
    (output / 'index.html').write_text(doc('多闪聊天记录', body), encoding='utf-8')

    summary = {
        'app': 'Duoshan',
        'app_version': app_version,
        'timezone': tz_label,
        'total_message_count': total,
        'total_text_message_count': text_total,
        'date_range': {'first': date_from, 'last': date_to},
        'account_count': len(summaries),
        'page_size': page_size,
        'accounts': list(summaries),
        'local_media_files': list(local_media_records),
        'media_and_presentation': presentation,
        'source': str(source) if source else '',
        'exported_at': exported_at,
    }
    (output / paths.ROOT_FILES['summary']).write_text(dump(summary, pretty=True),
                                                      encoding='utf-8')
    (output / paths.ROOT_FILES['readme']).write_text(
        _readme_text(summary, page_size, tz_label, source), encoding='utf-8')
    return summary


def _readme_text(summary: dict, page_size: int, tz_label: str, source: object) -> str:
    """README.txt: Chinese explanatory text referring to English output filenames."""
    presentation = summary['media_and_presentation']
    voice = (presentation['media'].get('voice') or {})
    emoji = (presentation['media'].get('emoji') or {})
    styles = presentation['original_bubble_styles']
    inline_assets = presentation['inline_emoji_assets']
    inline_occ = presentation['rendered'].get('inline_emoji_occurrences', 0)
    bubble_msgs = presentation['rendered'].get('original_bubble_messages', 0)
    bubble_missing = presentation['messages_missing_original_bubble_asset']
    total = summary['total_message_count']
    text_total = summary['total_text_message_count']
    date_range = summary['date_range']
    media_files = len(summary['local_media_files'])
    return f'''多闪聊天记录 - README

来源：{source if source else '本地备份'}。
App 版本：{summary['app_version']}。
导出时间：{summary['exported_at']}
聊天正文、联系人和消息数据来自本地备份；语音、表情和气泡素材仅在启用下载的 media 命令下才会补全。
导出过程未登录任何账号，原始备份文件未被修改。HTML 页面中的媒体引用全部指向本目录内的本地文件，离线即可打开。

{summary['account_count']} 个账号，共 {total:,} 条消息记录，其中 {text_total:,} 条为 type-7 文字消息。
时间范围：{date_range['first']} 至 {date_range['last']}。页面、CSV 和 TXT 中的时间均已转换为 {tz_label}。
若多个账号保存了同一会话的副本，将分别保留，不跨账号去重。

使用：双击 index.html，选择账号，再按会话浏览。每页最多 {page_size} 条，按时间从早到晚排序。
会话列表提供“最早”“最新”和完整会话 TXT。页面搜索框只筛选当前列表或当前消息页。
语音可在页面播放，或点“打开语音文件”——离线可用。
表情直接显示；GIF/动图 WebP 保留动画。正文中如 [smile] 的名称会用本地小表情素材渲染，原始文本在数据库、CSV、TXT 和 JSONL 中保持不变。
气泡按每条消息原有气泡 ID 和发送方向渲染；已恢复 {styles} 款原始气泡。素材未留存的气泡使用默认样式。
原始素材全部保留；页面只近似 Android 九宫格拉伸，不保证与手机端逐像素一致。

媒体补全情况：
  语音：{voice.get('messages_downloaded', 0)} / {voice.get('message_total', 0)} 条消息可用。
  表情：{emoji.get('messages_downloaded', 0)} / {emoji.get('message_total', 0)} 条消息可用，{emoji.get('unique_downloaded', 0)} 个素材，其中 {emoji.get('animated_unique', 0)} 个为动图。
  小表情：{inline_assets} 个本地素材，页面替换 {inline_occ} 处。
  原始气泡：{bubble_msgs} 条文字/语音消息使用原图样式；{bubble_missing} 条消息的历史气泡 ID 没有对应素材，使用默认样式。
  失败包括原始地址返回 HTTP 403/404 以及没有媒体地址的空消息；没有伪造地址，也没有忽略失败。
  {paths.ROOT_FILES['media_manifest']}：每个资源的下载结果、文件路径、哈希和消息关联。
  {paths.ROOT_FILES['decorations']}：气泡 ID、原始配置、素材路径和小表情映射。

本目录（导出根）文件：
  index.html                  - 入口页，列出各账号和媒体汇总。
  reader.css                  - HTML 页面使用的样式表。
  {paths.ROOT_FILES['media_manifest']}             - 每条消息/资源的下载结果，含失败与未保存地址的记录。
  {paths.ROOT_FILES['decorations']}     - 气泡和小表情素材。
  {paths.ROOT_FILES['summary']}       - 机器可读的导出与媒体统计。
  {paths.ROOT_FILES['data_validation']} / {paths.ROOT_FILES['media_validation']} / {paths.ROOT_FILES['reader_validation']} - 生成时的校验报告。
  {paths.ROOT_FILES['readme']}                  - 本文件。
  {paths.DIRECTORIES['attachments']}/      - 共享本地媒体，包括下载的语音、表情、气泡素材和从备份复制的文件。

各账号目录（<uid>/）：
  {paths.ACCOUNT_FILES['database']}             - 解密后的主聊天库，包含全部原始消息、会话、成员和附属表；附件以独立文件保存。
  {paths.ACCOUNT_FILES['messages_csv']}         - 可读的正文、时间、发送者和消息标识。
  {paths.ACCOUNT_FILES['messages_jsonl']}       - 每行一条完整原始 msg 表记录并附带 _export 块；正文和原始字段不截断。
  {paths.ACCOUNT_FILES['contacts_json']} / {paths.ACCOUNT_FILES['contacts_csv']}   - 联系人缓存（昵称/备注），用于补全显示名。不是手机系统通讯录。
  {paths.ACCOUNT_FILES['conversations']} - 会话列表、群信息、成员和附件表记录。
  {paths.ACCOUNT_FILES['statistics']} / {paths.ACCOUNT_FILES['index_csv']} - 数量、类型和时间范围。
  {paths.DIRECTORIES['pages']}/            - 可离线打开的 HTML，每个会话一页：conversation_001_page_001.html 等。
  {paths.DIRECTORIES['transcripts']}/      - 每个会话的可读文本：conversation_001.txt 等。

说明：
  主数据库按备份自身的 SQLCipher 参数读取；导出后是单个独立 SQLite 文件，原始备份不会被修改。
  msg.deleted 非 0 的行不会被过滤；页面会显式显示删除标记。该标记不等于撕回，也不等于已恢复删除的内容。
  统计包含系统/内部消息，不代表每一行都曾在 App 界面中可见。
  媒体类型名称根据正文内容推断；无法识别的类型保留数字编号。JSONL 和数据库保留原始值。
  备份的 files/im 目录包含 {media_files} 个媒体文件，多为音效素材；具体命中多少条消息见统计。
  图片、表情、视频和语音的原始网络地址与媒体标识保留在 JSONL、数据库和原始 HTML 内容中；已下载的语音、表情和气泡位于 {paths.DIRECTORIES['attachments']}/。
  备份中缺失的媒体、数据库中已删除的历史和服务器未缓存的内容无法保证可从本目录恢复。
  CSV 使用带 BOM 的 UTF-8。为避免表格软件破坏长 ID 或把正文当公式执行，部分单元格会带前导单引号；JSONL 和 SQLite 保留原始值。
  JSONL 中的 ID 可能超出 JavaScript 安全整数范围；请用 Python、SQLite 或支持大整数的 JSON 读取器处理。
'''


# ---------------------------------------------------------------- public entries
def _output_dir(config: 'ExportConfig') -> Path:
    output = Path(getattr(config, 'output'))
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ExportError('Cannot create the output directory ' + str(output) + ': ' + str(error))
    return output


def export_chats(config: 'ExportConfig', layout: 'SourceLayout') -> dict:
    """Export every selected account of a prepared source into the reader tree.

    Writes the database, contacts, conversations, raw JSONL, spreadsheet-safe
    CSV, texts, HTML pages, root page, stylesheet, summary and README. Media and
    bubble manifests are honoured when present but are not required.
    """
    output = _output_dir(config)
    accounts = _select_accounts(config, layout)
    reader = ReaderMedia(output)
    reader.reset()

    app_dir = getattr(layout, 'app_dir', None)
    media_records, media_lookup = _copy_local_media(
        app_dir, output, getattr(layout, 'im_dir', None))
    profiles = _load_profiles(layout, [str(account.uid) for account in accounts])

    summaries = []
    for account in accounts:
        state = _state_for_export(config, layout, account, output, profiles)
        summaries.append(_render_account(config, reader, output, state, media_lookup))

    summary = _write_root_page(config, reader, output, summaries, layout, media_records,
                               _read_json(output / paths.ROOT_FILES['summary']))
    return {
        'output': str(output),
        'accounts': [item['uid'] for item in summaries],
        'account_summaries': summaries,
        'total_message_count': summary['total_message_count'],
        'total_text_message_count': summary['total_text_message_count'],
        'timezone': summary['timezone'],
        'app_version': summary['app_version'],
        'page_size': summary['page_size'],
        'date_range': summary['date_range'],
        'local_media_files': len(media_records),
        'local_media_matched_messages': sum(
            item['local_media_matched_messages'] for item in summaries),
        'presentation': summary['media_and_presentation'],
        'summary_file': str(output / paths.ROOT_FILES['summary']),
        'readme': str(output / paths.ROOT_FILES['readme']),
        'index': str(output / 'index.html'),
        'stylesheet': str(output / 'reader.css'),
        'exported_at': summary['exported_at'],
        'mode': 'export',
    }


def rerender_export(config: 'ExportConfig') -> dict:
    """Rebuild the reader pages from an existing export, without the backup.

    Used by the media command after new voice/sticker/bubble resources land:
    the exported per-account databases, contacts and local attachment tree are
    reused, the decoration manifest is re-read, and no decryption is needed.
    """
    output = Path(getattr(config, 'output'))
    if not output.is_dir():
        raise ExportError('Cannot rerender: the export output directory does not exist: '
                          + str(output))
    wanted = tuple(str(uid) for uid in (getattr(config, 'accounts', ()) or ()))
    account_dirs = sorted(
        (path for path in output.iterdir()
         if path.is_dir() and (path / paths.ACCOUNT_FILES['database']).is_file()),
        key=lambda path: path.name,
    )
    if wanted:
        by_uid = {path.name: path for path in account_dirs}
        missing = [uid for uid in wanted if uid not in by_uid]
        if missing:
            raise ExportError('Cannot rerender: no exported database for account(s) '
                              + ', '.join(missing) + ' in ' + str(output))
        account_dirs = [by_uid[uid] for uid in wanted]
    if not account_dirs:
        raise ExportError('Cannot rerender: no exported account databases were found in '
                          + str(output))

    reader = ReaderMedia(output)
    reader.reset()
    media_records = _attachment_records_from_summary(output)
    media_lookup = _lookup_from_records(output, media_records)
    if not media_lookup:
        media_lookup = _lookup_from_attachment_tree(output)

    summaries = []
    for account_dir in account_dirs:
        state = _state_from_export(output, account_dir.name)
        summaries.append(_render_account(config, reader, output, state, media_lookup))

    summary = _write_root_page(config, reader, output, summaries, None, media_records,
                               _read_json(output / paths.ROOT_FILES['summary']))
    return {
        'output': str(output),
        'accounts': [item['uid'] for item in summaries],
        'account_summaries': summaries,
        'total_message_count': summary['total_message_count'],
        'total_text_message_count': summary['total_text_message_count'],
        'timezone': summary['timezone'],
        'app_version': summary['app_version'],
        'page_size': summary['page_size'],
        'date_range': summary['date_range'],
        'local_media_files': len(media_records),
        'local_media_matched_messages': sum(
            item['local_media_matched_messages'] for item in summaries),
        'presentation': summary['media_and_presentation'],
        'summary_file': str(output / paths.ROOT_FILES['summary']),
        'readme': str(output / paths.ROOT_FILES['readme']),
        'index': str(output / 'index.html'),
        'stylesheet': str(output / 'reader.css'),
        'exported_at': summary['exported_at'],
        'mode': 'rerender',
    }
