"""Export Xiaohongshu chat history into the portable offline reader tree.

Produces the same output contract as the Duoshan exporter: one directory per
account with a decrypted standalone ``chat.sqlite``, raw ``messages.jsonl``,
spreadsheet-safe ``messages.csv``, contacts, per-conversation HTML pages and
TXT transcripts, plus a root index page and summary. The generic reader
helpers, stylesheet and file naming are reused from :mod:`..exporter` and
:mod:`..paths`, so both apps share the look and structure while keeping their
schemas independent.

A single ``msgDB`` can hold rows from several logged-in accounts; each
account export keeps only the rows whose ``local_chat_user_id`` /
``local_group_chat_id`` owner suffix matches, so account copies stay separate
just like the Duoshan exporter keeps per-account databases.

The backup contains no Xiaohongshu IM media files, so nothing is downloaded
or copied: image/video/emoji messages keep their original network URLs, which
are shown in the HTML and preserved in the JSONL/CSV/SQLite output.
"""

from __future__ import annotations

import collections
import csv
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .. import paths
from ..errors import ExportError
from . import xhs
from .xhs import XhsSource

from ..exporter import (  # generic, shared reader helpers
    STYLE,
    csv_value,
    dump,
    e,
    navigation,
    parse,
    read_rows,
    read_rows_optional,
    timestamp,
    write_csv,
)

# Message content types observed in the verified databases. Unknown codes
# fall back to "类型 <code>", exactly like the Duoshan reader.
TYPE_NAMES = {
    0: '系统消息', 1: '文字', 2: '图片', 3: '分享卡片', 4: '系统提示',
    9: '视频', 10: '群通知', 11: '视频', 13: '表情', 16: '表情',
}

# Text keys used, in order, when summarising a content blob.
CONTENT_TEXT_KEYS = ('content', 'commentContent', 'title', 'emojiKey',
                     'notice_title', 'desc', 'description', 'text')

MEDIA_URL_KEYS = ('url', 'link', 'cover', 'image')


def _inner_content(content: Any) -> Any:
    """Unwrap the double-encoded ``content`` blobs some message types store."""
    if isinstance(content, str) and content.lstrip().startswith(('{', '[')):
        content = parse(content)
    if isinstance(content, dict):
        inner = content.get('content')
        if isinstance(inner, str) and inner.lstrip().startswith('{'):
            parsed = parse(inner)
            if isinstance(parsed, dict):
                merged = dict(content)
                merged['content'] = parsed
                return merged
    return content


def readable_content(msg_type: object, content: Any) -> str:
    """Readable summary of a Xiaohongshu message content blob."""
    content = _inner_content(content)
    label = TYPE_NAMES.get(msg_type, f'类型 {msg_type}')
    if not isinstance(content, dict):
        return content if isinstance(content, str) else dump(content)
    values: list[str] = []
    for key in CONTENT_TEXT_KEYS:
        value = content.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in values:
            values.append(value.strip())
    if not values:
        return '[' + label + ']'
    return values[0] if msg_type in {0, 1, 4, 10} else '[' + label + '] ' + '\n'.join(values)


def media_urls(content: Any) -> list[str]:
    """Distinct http(s) URLs referenced by a message content blob."""
    content = _inner_content(content)
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key == 'imageDataMap' and isinstance(child, dict):
                    for variant in child.values():
                        visit(variant)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and item.startswith(('http://', 'https://')):
            if item not in found:
                found.append(item)

    visit(content)
    return found


def _looks_like_image(url: str) -> bool:
    """Conservative image-URL test; video and deep links are never shown inline."""
    path = url.split('?')[0].lower()
    if path.endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif')):
        return True
    host = url.split('/')[2] if '://' in url else ''
    return any(marker in host for marker in ('msg-img.', 'picasso-static', 'sns-img'))


# --------------------------------------------------------------------- html
def render_message(msg: dict, uid: str, local_media: dict[str, str] | None = None,
                   depth: int = 2) -> str:
    """One message article: header, bubble, media (local when downloaded),
    remote links and the raw JSON block.

    ``local_media`` maps a media URL to its export-root-relative path in
    ``attachments/`` (from ``media_manifest.json``); when a URL is mapped the
    page references the local file so the archive stays offline-viewable, and
    the original URL is always kept in the link list and the raw block.
    """
    raw, meta = msg['raw'], msg['meta']
    own = str(raw.get('sender_id')) == uid
    classes = 'message own' if own else 'message'
    body = (
        '<article class="' + classes + '" id="msg-' + e(raw.get('uuid'))
        + '" data-search="' + e(str(meta.get('sender_name', '')) + ' ' + str(meta.get('text', '')))
        + '"><header><strong>'
        + e(('我 · ' if own else '') + str(meta.get('sender_name', '')))
        + '</strong><span>' + e(meta.get('time')) + '</span><span class="badge">'
        + e(meta.get('type_name')) + '</span></header>'
        + msg['bubble_html']
    )
    urls = media_urls(msg.get('content'))
    images = [u for u in urls if _looks_like_image(u)]
    videos = [u for u in urls if not _looks_like_image(u)
              and ('msg-video.' in u or u.split('?')[0].lower().endswith(('.mp4', '.mov')))]
    prefix = '../' * depth

    def src_for(url: str) -> str:
        # Exact match first, then the query-less base so resize/format
        # variants (?imageView2/...) map to the same downloaded file.
        media_map = local_media or {}
        mapped = media_map.get(url) or media_map.get(url.split('?', 1)[0])
        return quote(prefix + mapped) if mapped else e(url)

    for url in images[:4]:
        body += ('<p class="remote-media"><img loading="lazy" src="' + src_for(url)
                 + '" alt=""'
                 + ('' if (local_media or {}).get(url)
                    else ' onerror="this.remove()"')
                 + '></p>')
    for url in videos[:2]:
        mapped = (local_media or {}).get(url)
        body += ('<p class="remote-media"><video controls preload="none" src="'
                 + src_for(url) + '"></video></p>')
    if urls:
        body += '<details class="links"><summary>原始网络地址</summary><ul>'
        for index, url in enumerate(urls[:8], 1):
            host = url.split('/')[2] if '://' in url else url
            body += ('<li><a rel="noreferrer noopener" target="_blank" href="' + e(url) + '">'
                     + e(f'链接 {index} · {host}') + '</a></li>')
        body += '</ul></details>'
    body += ('<details><summary>原始消息内容 · ' + e(raw.get('uuid', ''))
             + '</summary><pre>' + e(dump(msg['content'], pretty=True)) + '</pre></details></article>')
    return body


DOC_CSP = (
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    'style-src \'self\' \'unsafe-inline\' file:; script-src \'unsafe-inline\'; '
    'img-src \'self\' data: https: file:; media-src \'self\' file: https:; base-uri \'none\'; '
    'form-action \'none\'"><title>'
)


def xhs_doc(title: object, body: str, depth: int = 0, search: bool = False) -> str:
    """Reader document wrapper: same layout as the shared doc(), but the CSP
    also allows https media because Xiaohongshu keeps IM media on its CDN."""
    prefix = '../' * depth
    search_script = ('<script>document.querySelector("#filter")?.addEventListener("input",function(){'
                     'const q=this.value.toLocaleLowerCase();'
                     'for(const row of document.querySelectorAll("[data-search]"))'
                     'row.hidden=!row.dataset.search.toLocaleLowerCase().includes(q);});</script>'
                     ) if search else ''
    return (DOC_CSP + e(title) + '</title><link rel="stylesheet" href="' + prefix
            + 'reader.css"></head><body><main>' + body + '</main>' + search_script + '</body></html>')


# ------------------------------------------------------------------- helpers
def _owner_of(row: dict) -> str:
    """Owning account id of a message row ('' when the row has none)."""
    for column in ('local_chat_user_id', 'local_group_chat_id'):
        text = str(row.get(column) or '')
        if '@' in text:
            return text.rsplit('@', 1)[1]
    return ''


def _conversation_key(row: dict) -> tuple[str, str]:
    """(kind, conversation key) of a message row: ('direct'|'group', id)."""
    group = str(row.get('local_group_chat_id') or '')
    if row.get('is_group_chat') or group:
        return 'group', group.split('@', 1)[0]
    direct = str(row.get('local_chat_user_id') or '')
    return 'direct', direct.split('@', 1)[0]


def _chat_display_name(kind: str, key: str, chats: dict[str, dict],
                       groups: dict[str, dict], users: dict[str, dict]) -> str:
    if kind == 'group':
        info = groups.get(key) or {}
        return info.get('group_name') or ('群聊 ' + key)
    info = chats.get(key) or {}
    peer = info.get('nickname') or (users.get(key) or {}).get('nickname') or ''
    if not peer and key.startswith('0:1:'):
        parts = [part for part in key.split(':')[2:] if part and part != '']
        peer = ' / '.join((users.get(part) or {}).get('nickname') or part for part in parts)
    return peer or ('会话 ' + key)


def _prune_generated(directory: Path, pattern, expected: set[str]) -> int:
    """Delete superseded generated reader files; never touch foreign files."""
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


# -------------------------------------------------------------------- export
def _prepare_databases(config, layout, source: XhsSource, work_root: Path) -> dict[str, Path]:
    """Decrypt every needed database into the private work directory."""
    work_root.mkdir(parents=True, exist_ok=True)
    plain: dict[str, Path] = {}
    for spec in xhs.SUPPORTED_DATABASES:
        if spec.name not in source.databases:
            continue
        destination = work_root / (spec.name + '.plain.sqlite')
        result = xhs.decrypt_database(source, spec, destination)
        plain[spec.name] = destination
    return plain


def export_chats(config, layout) -> dict:
    """Export every selected Xiaohongshu account of a prepared source."""
    output = Path(config.output)
    output.mkdir(parents=True, exist_ok=True)
    source = xhs.discover(layout)
    if source is None:
        raise ExportError(
            "No Xiaohongshu (com.xingin.xhs) app data was found in this backup. "
            "The databases directory must contain one of: " + ", ".join(sorted(xhs.MARKER_DATABASES))
            + ". For Duoshan backups, run with --app duoshan.")

    work_root = Path(config.work) / 'xhs'
    plain = _prepare_databases(config, layout, source, work_root)
    message_db_path = plain.get(xhs.MESSAGE_DB.name)
    if message_db_path is None:
        raise ExportError("This backup has no msgDB; there are no chat records to export.")

    db = sqlite3.connect(message_db_path)
    try:
        discovered = xhs.account_ids(db)
        if not discovered:
            raise ExportError("msgDB contains no account-owned messages; nothing to export.")
        wanted = tuple(config.accounts) or tuple(discovered)
        missing = [uid for uid in wanted if uid not in discovered]
        if missing:
            raise ExportError('Requested account(s) not present in msgDB: ' + ', '.join(missing)
                              + '. Available: ' + ', '.join(discovered))

        message_rows = [row for row in read_rows(db, 'message') if _owner_of(row) in wanted]
        chat_rows = [row for row in read_rows(db, 'chat')
                     if str(row.get('local_chat_user_id') or '').endswith(
                         tuple('@' + uid for uid in wanted))]
        group_rows = [row for row in read_rows(db, 'group_chat')
                      if str(row.get('local_group_chat_id') or '').endswith(
                          tuple('@' + uid for uid in wanted))]
        chat_set_rows = read_rows_optional(db, 'chat_set')
        msgheader_rows = [row for row in read_rows_optional(db, 'msgheader')
                          if str(row.get('uid') or '') in wanted]
        user_rows = read_rows_optional(db, 'user')
        relation_rows = _read_relation_rows(plain.get(xhs.CONTACTS_DB.name))
    finally:
        db.close()

    chats = {str(row.get('chat_id')): row for row in chat_rows}
    groups = {str(row.get('group_id')): row for row in group_rows}
    users = {str(row.get('user_id')): row for row in user_rows}
    login_uid = xhs.login_user_id(source)
    login_name = _login_name(source, users)

    from . import xhs_media

    local_media = xhs_media.local_lookup(output)
    summaries = []
    for uid in wanted:
        summaries.append(_export_account(
            config, output, message_db_path, uid,
            [row for row in message_rows if _owner_of(row) == uid],
            chats, groups, users, user_rows, relation_rows,
            chat_rows, group_rows, chat_set_rows, msgheader_rows,
            login_uid == uid, login_name, local_media))

    summary = _write_root(config, output, summaries, layout, source)
    return {
        'output': str(output),
        'app': 'xhs',
        'accounts': [item['uid'] for item in summaries],
        'account_summaries': summaries,
        'total_message_count': summary['total_message_count'],
        'timezone': summary['timezone'],
        'app_version': summary['app_version'],
        'date_range': summary['date_range'],
        'summary_file': str(output / paths.ROOT_FILES['summary']),
        'index': str(output / 'index.html'),
        'exported_at': summary['exported_at'],
        'mode': 'export',
    }


def _read_relation_rows(path: Path | None) -> list[dict]:
    if path is None:
        return []
    connection = sqlite3.connect(path)
    try:
        return read_rows_optional(connection, 'local_relation_user')
    finally:
        connection.close()


def _login_name(source: XhsSource, users: dict[str, dict]) -> str:
    """Display name of the last logged-in account, when discoverable."""
    profile = xhs.login_user_profile(source)
    nickname = profile.get("nickname")
    if isinstance(nickname, str) and nickname:
        return nickname
    uid = xhs.login_user_id(source)
    if uid and uid in users:
        return str((users[uid] or {}).get('nickname') or uid)
    return uid or ''


def _export_account(config, output: Path, message_db_path: Path, uid: str,
                    message_rows: list[dict], chats: dict[str, dict],
                    groups: dict[str, dict], users: dict[str, dict],
                    user_rows: list[dict], relation_rows: list[dict],
                    chat_rows: list[dict], group_rows: list[dict],
                    chat_set_rows: list[dict], msgheader_rows: list[dict],
                    is_login_account: bool, login_name: str,
                    local_media: dict[str, str] | None = None) -> dict:
    """Write one account's reader tree and return its summary."""
    tzinfo = config.tzinfo
    tz_label = getattr(config, 'timezone', 'UTC') or 'UTC'
    page_size = int(getattr(config, 'page_size', 0) or 0) or 600
    account_dir = output / uid
    pages_dir = account_dir / paths.DIRECTORIES['pages']
    text_dir = account_dir / paths.DIRECTORIES['transcripts']
    pages_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    # Standalone decrypted database (the shared plaintext msgDB copy).
    database_path = account_dir / paths.ACCOUNT_FILES['database']
    shutil.copyfile(message_db_path, database_path.with_name('.chat.sqlite.tmp'))
    database_path.with_name('.chat.sqlite.tmp').replace(database_path)

    own_name = (users.get(uid) or {}).get('nickname') or login_name or ('UID ' + uid)

    (account_dir / paths.ACCOUNT_FILES['contacts_json']).write_text(
        dump({'user_table': user_rows, 'relation_users': relation_rows}, pretty=True),
        encoding='utf-8')
    write_csv(
        account_dir / paths.ACCOUNT_FILES['contacts_csv'],
        ['User ID', 'Nickname', 'UserName', 'Account Name', 'Is Friend', 'Is Official'],
        ([row.get(k, '') for k in ('user_id', 'nickname', 'userName', 'account_name',
                                   'is_friend', 'is_official')] for row in user_rows),
        (0,),
    )
    (account_dir / paths.ACCOUNT_FILES['conversations']).write_text(
        dump({'chat': chat_rows, 'group_chat': group_rows, 'chat_set': chat_set_rows,
              'msgheader': msgheader_rows, 'relation_users': relation_rows},
             pretty=True), encoding='utf-8')

    sender_names = {uid: own_name}
    for row in user_rows:
        sender_names.setdefault(str(row.get('user_id')),
                                str(row.get('nickname') or row.get('user_id')))

    groups_by_key: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for row in message_rows:
        groups_by_key[_conversation_key(row)].append(row)
    ordered = sorted(groups_by_key,
                     key=lambda key: max((row.get('create_time') or 0)
                                         for row in groups_by_key[key]),
                     reverse=True)

    conv_summary: list[dict] = []
    type_counts: collections.Counter = collections.Counter()
    expected_pages: set[str] = set()
    expected_texts: set[str] = set()
    jsonl_path = account_dir / paths.ACCOUNT_FILES['messages_jsonl']
    csv_path = account_dir / paths.ACCOUNT_FILES['messages_csv']
    page_pattern = re.compile(r'^conversation_\d+_page_\d+\.html$')
    text_pattern = re.compile(r'^conversation_\d+\.txt$')
    with jsonl_path.open('w', encoding='utf-8', newline='\n') as jf, \
            csv_path.open('w', encoding='utf-8-sig', newline='') as cf:
        writer = csv.writer(cf)
        writer.writerow(['Account UID', 'Conversation', 'Conversation ID', 'Kind',
                         f'Time ({tz_label})', 'Sender', 'Sender ID', 'Message Type',
                         'Type Code', 'Text', 'Message UUID', 'Server Message ID'])
        for conv_index, key in enumerate(ordered, 1):
            kind, conv_key = key
            rows = sorted(groups_by_key[key],
                          key=lambda row: (row.get('create_time') or 0,
                                           row.get('store_id') or 0,
                                           str(row.get('uuid'))))
            conv_name = _chat_display_name(kind, conv_key, chats, groups, users)
            base_name = paths.conversation_basename(conv_index)
            page_count = max(1, (len(rows) + page_size - 1) // page_size)
            page_files = [paths.conversation_page(conv_index, p + 1)
                          for p in range(page_count)]
            text_path = text_dir / (base_name + '.txt')
            expected_pages.update(page_files)
            expected_texts.add(text_path.name)
            page_messages: list[dict] = []
            with text_path.open('w', encoding='utf-8', newline='\n') as tf:
                tf.write(f'会话：{conv_name}\n')
                tf.write(f'账号：{own_name} ({uid})\n')
                tf.write(f'会话ID：{conv_key} ({kind})\n')
                tf.write(f'时区：{tz_label}\n')
                tf.write(f'记录数：{len(rows)}\n\n')
                for raw in rows:
                    content = parse(raw.get('content'))
                    content = _inner_content(content)
                    sender_id = str(raw.get('sender_id') or '')
                    if sender_id == uid:
                        sender_name = '我'
                    elif sender_names.get(sender_id):
                        sender_name = sender_names[sender_id]
                    elif kind == 'direct' and sender_id == conv_key:
                        sender_name = conv_name  # the peer of a direct chat
                    else:
                        sender_name = sender_id or '未知发送者'
                    text = readable_content(raw.get('content_type'), content)
                    meta = {
                        'account_uid': uid,
                        'conversation_name': conv_name,
                        'conversation_key': conv_key,
                        'conversation_kind': kind,
                        'time': timestamp(raw.get('create_time'), tzinfo),
                        'timezone': tz_label,
                        'sender_name': sender_name,
                        'type_name': TYPE_NAMES.get(raw.get('content_type'),
                                                    f"类型 {raw.get('content_type')}"),
                        'text': text,
                    }
                    jf.write(dump({**raw, '_export': meta}) + '\n')
                    normalized = [uid, conv_name, conv_key, kind, meta['time'], sender_name,
                                  sender_id, meta['type_name'], raw.get('content_type'),
                                  text, raw.get('uuid'), raw.get('message_id')]
                    writer.writerow([csv_value(v, i in {0, 2})
                                     for i, v in enumerate(normalized)])
                    tf.write(f"{meta['time']}  {sender_name} ({sender_id})\n{text}\n\n")
                    page_messages.append({'raw': raw, 'meta': meta, 'content': content,
                                          'bubble_html': '<div class="bubble"><span class="bubble-content">'
                                                         + e(text) + '</span></div>'})
                    type_counts[raw.get('content_type')] += 1

            for page_index, start in enumerate(range(0, len(page_messages), page_size)):
                chunk = page_messages[start:start + page_size]
                nav = navigation(page_files, page_index)
                body = ('<a class="back" href="../index.html">← 会话列表</a><h1>' + e(conv_name)
                        + '</h1><p class="muted">账号 ' + e(own_name) + ' · ' + e(uid)
                        + '<br>会话 ' + e(conv_key) + (' (群聊)' if kind == 'group' else '')
                        + f'<br>{len(rows):,} 条记录 · ' + e(tz_label) + '</p>')
                body += ('<p class="reader-note">小红书备份不含聊天媒体文件，图片、视频与表情保留原始网络地址，'
                         '联网时可直接显示。</p><div class="toolbar"><input id="filter" type="search" '
                         'placeholder="筛选本页正文或发送者"><a href="../'
                         + paths.DIRECTORIES['transcripts'] + '/' + quote(text_path.name)
                         + '">完整会话 TXT</a></div>' + nav)
                body += ''.join(render_message(message, uid, local_media) for message in chunk) + nav
                (pages_dir / page_files[page_index]).write_text(
                    xhs_doc(conv_name, body, depth=2, search=True), encoding='utf-8')

            conv_summary.append({
                'conversation_key': conv_key, 'kind': kind, 'name': conv_name,
                'count': len(rows),
                'first': timestamp(rows[0].get('create_time'), tzinfo),
                'last': timestamp(rows[-1].get('create_time'), tzinfo),
                'page': paths.DIRECTORIES['pages'] + '/' + page_files[0],
                'latest_page': paths.DIRECTORIES['pages'] + '/' + page_files[-1],
                'txt': paths.DIRECTORIES['transcripts'] + '/' + text_path.name,
                'html_pages': page_count,
            })

    _prune_generated(pages_dir, page_pattern, expected_pages)
    _prune_generated(text_dir, text_pattern, expected_texts)

    write_csv(
        account_dir / paths.ACCOUNT_FILES['index_csv'],
        ['Conversation', 'Conversation ID', 'Kind', 'Messages',
         f'First Time ({tz_label})', f'Last Time ({tz_label})'],
        ([c['name'], c['conversation_key'], c['kind'], c['count'], c['first'], c['last']]
         for c in conv_summary),
        (1,),
    )
    title = own_name + ' - 聊天记录'
    body = ('<a class="back" href="../index.html">← 全部账号</a><h1>' + e(title)
            + '</h1><p class="muted">账号 ID：' + e(uid) + (' · 当前登录账号' if is_login_account else '')
            + f'<br>{len(message_rows):,} 条记录 · {len(conv_summary)} 个有消息的会话</p>'
            '<div class="downloads"><a href="' + paths.ACCOUNT_FILES['messages_csv']
            + '">消息 CSV</a><a href="' + paths.ACCOUNT_FILES['messages_jsonl']
            + '">完整消息 JSONL</a><a href="' + paths.ACCOUNT_FILES['database']
            + '">解密数据库</a><a href="' + paths.ACCOUNT_FILES['contacts_csv']
            + '">联系人 CSV</a></div><input id="filter" type="search" '
            'placeholder="搜索会话名称或 ID"><div class="table-wrap"><table>'
            '<thead><tr><th>会话</th><th>类型</th><th>记录数</th><th>时间范围</th>'
            '<th>打开</th></tr></thead><tbody>')
    for item in conv_summary:
        body += ('<tr data-search="' + e(item['name'] + ' ' + item['conversation_key'])
                 + '"><td><strong>' + e(item['name']) + '</strong><small>'
                 + e(item['conversation_key']) + '</small></td><td>'
                 + ('群聊' if item['kind'] == 'group' else '私聊') + '</td><td>'
                 + f"{item['count']:,}" + '</td><td class="dates">' + e(item['first'])
                 + '<br>' + e(item['last']) + '</td><td><a href="'
                 + quote(item['page']) + '">最早</a> · <a href="'
                 + quote(item['latest_page']) + '">最新</a> · <a href="'
                 + quote(item['txt']) + '">TXT</a></td></tr>')
    body += '</tbody></table></div>'
    (account_dir / 'index.html').write_text(xhs_doc(title, body, depth=1, search=True),
                                            encoding='utf-8')

    times = [row.get('create_time') for row in message_rows if row.get('create_time')]
    summary = {
        'uid': uid,
        'nickname': own_name,
        'login_account': is_login_account,
        'message_count': len(message_rows),
        'conversation_count': len(conv_summary),
        'first': timestamp(min(times), tzinfo) if times else '',
        'last': timestamp(max(times), tzinfo) if times else '',
        'contact_rows': len(user_rows),
        'relation_rows': len(relation_rows),
        'message_types': dict(type_counts),
        'conversations': conv_summary,
    }
    (account_dir / paths.ACCOUNT_FILES['statistics']).write_text(
        dump(summary, pretty=True), encoding='utf-8')
    return summary


def _write_root(config, output: Path, summaries: list[dict], layout,
                source: XhsSource) -> dict:
    """Root index page, stylesheet, summary, README and validation report."""
    tz_label = getattr(config, 'timezone', 'UTC') or 'UTC'
    total = sum(item['message_count'] for item in summaries)
    firsts = [item['first'] for item in summaries if item['first']]
    lasts = [item['last'] for item in summaries if item['last']]
    app_version = getattr(layout, 'app_version', None) or 'unknown'
    exported_at = timestamp(_now_ms(), config.tzinfo)
    summary = {
        'app': 'Xiaohongshu',
        'app_version': app_version,
        'timezone': tz_label,
        'total_message_count': total,
        'date_range': {'first': min(firsts) if firsts else '',
                       'last': max(lasts) if lasts else ''},
        'account_count': len(summaries),
        'accounts': list(summaries),
        'source': str(getattr(layout, 'source', '')),
        'exported_at': exported_at,
        'databases': sorted(source.databases),
        'login_user_id': xhs.login_user_id(source) or '',
    }
    body = ('<p class="muted">小红书 · Android 本地备份</p><h1>小红书聊天记录</h1>'
            + f'<p>已从 {len(summaries)} 个账号导出 <strong>{total:,}</strong> 条本地消息。'
            + f'所有时间均为 {e(tz_label)}。</p><div class="cards">')
    for item in summaries:
        body += ('<section class="card"><h2>' + e(item['nickname']) + '</h2><small>账号 '
                 + e(item['uid']) + (item['login_account'] and ' · 当前登录账号' or '')
                 + '</small><p class="big">' + f"{item['message_count']:,}"
                 + '<small>条消息 · ' + str(item['conversation_count'])
                 + ' 个会话</small></p><p>' + e(item['first']) + '<br>至 ' + e(item['last'])
                 + '</p><a href="' + quote(str(item['uid'])) + '/index.html">打开聊天记录 →</a></section>')
    body += ('</div><div class="notice">备份未包含小红书聊天媒体文件；图片、视频和表情保留原始网络地址，'
             '联网时显示。消息正文、联系人和数据库均来自本地备份，导出过程未登录任何账号，'
             '原始备份未被修改。</div>'
             '<p class="muted">每个账号都有完整解密 SQLite 数据库，另提供 CSV、JSONL 和按会话 TXT。'
             '详情见同目录的 ' + paths.ROOT_FILES['readme'] + '。</p>')
    (output / 'index.html').write_text(xhs_doc('小红书聊天记录', body), encoding='utf-8')
    (output / 'reader.css').write_text(STYLE + _XHS_STYLE_EXTRA, encoding='utf-8')
    (output / paths.ROOT_FILES['summary']).write_text(dump(summary, pretty=True),
                                                      encoding='utf-8')
    (output / paths.ROOT_FILES['readme']).write_text(
        _readme_text(summary, page_size=int(getattr(config, 'page_size', 0) or 600),
                     tz_label=tz_label), encoding='utf-8')
    (output / paths.ROOT_FILES['data_validation']).write_text(
        dump(_validation_report(output, summaries), pretty=True), encoding='utf-8')
    return summary


_XHS_STYLE_EXTRA = '''
.remote-media{margin:8px 0}.remote-media img{max-width:min(100%,420px);border-radius:8px;display:block}
.remote-media video{max-width:min(100%,420px);border-radius:8px;display:block}
'''


def _now_ms() -> int:
    return int(time.time() * 1000)


def _readme_text(summary: dict, page_size: int, tz_label: str) -> str:
    total = summary['total_message_count']
    date_range = summary['date_range']
    accounts = summary['accounts']
    return f'''小红书聊天记录 - README

来源：{summary['source'] or '本地备份'}。
App 版本：{summary['app_version']}。
导出时间：{summary['exported_at']}
聊天数据来自本地备份的 msgDB / localRelationDB；导出过程未登录任何账号，原始备份文件未被修改。

{summary['account_count']} 个账号，共 {total:,} 条消息记录。
时间范围：{date_range['first']} 至 {date_range['last']}。页面、CSV 和 TXT 中的时间均已转换为 {tz_label}。
一个 msgDB 可包含多个历史登录账号的记录；每个账号目录只保留其名下的消息，不跨账号去重。
当前登录账号：{summary['login_user_id'] or '未知'}。

使用：双击 index.html，选择账号，再按会话浏览。每页最多 {page_size} 条，按时间从早到晚排序。
图片、视频与表情的原始网络地址保留在页面、CSV、JSONL 和数据库中；联网时可直接显示，
备份中不含这些媒体文件，因此无法离线显示。

本目录（导出根）文件：
  index.html                  - 入口页，列出各账号。
  reader.css                  - HTML 页面使用的样式表。
  export_summary.json         - 机器可读的导出统计。
  validation_results.json     - 导出完整性检查结果。
  README.txt                  - 本文件。

各账号目录（<uid>/）：
  chat.sqlite                 - 解密后的 msgDB 完整副本（含该备份内所有账号的原始行）。
  messages.jsonl              - 每行一条本账号原始 message 行 + _export 块；正文不截断。
  messages.csv                - 可读的正文、时间、发送者和消息标识。
  contacts.json / contacts.csv - user 表联系人缓存与 localRelationDB 关系用户。
  conversation_metadata.json  - chat / group_chat / chat_set / msgheader / relation_users 原始行。
  statistics.json             - 数量、类型和时间范围。
  conversation_index.csv      - 会话索引。
  conversations/              - 可离线打开的 HTML 会话页。
  transcripts/                - 每个会话的可读文本。

说明：
  解密使用备份内 MMKV 存储的数据库口令（WCDB/SQLCipher 变体：PBKDF2-HMAC-SHA1 64000 次、
  AES-256-CBC、页大小 1024、HMAC-SHA256）；导出后为独立 SQLite 文件。
  JSONL 中的 content 字段保留原始值（部分类型为二次编码的 JSON 字符串），未做改写。
  CSV 使用带 BOM 的 UTF-8；为防表格软件破坏长 ID，部分单元格带前导单引号。
'''


def _validation_report(output: Path, summaries: list[dict]) -> dict:
    """Minimal data-integrity report written to validation_results.json."""
    checks: list[dict] = []
    for item in summaries:
        uid = item['uid']
        account_dir = output / uid
        database = account_dir / paths.ACCOUNT_FILES['database']
        checks.append({'account': uid, 'check': 'database_exists',
                       'passed': database.is_file(), 'detail': str(database)})
        jsonl = account_dir / paths.ACCOUNT_FILES['messages_jsonl']
        lines = -1
        if jsonl.is_file():
            with jsonl.open('rb') as handle:
                lines = sum(1 for _ in handle)
        checks.append({'account': uid, 'check': 'jsonl_row_count',
                       'passed': lines == item['message_count'],
                       'expected': item['message_count'], 'found': max(0, lines)})
        pages_dir = account_dir / paths.DIRECTORIES['pages']
        page_files = [page for page in pages_dir.glob('conversation_*.html')] if pages_dir.is_dir() else []
        expected_pages = sum(conv['html_pages'] for conv in item['conversations'])
        checks.append({'account': uid, 'check': 'conversation_pages',
                       'passed': len(page_files) >= expected_pages,
                       'expected': expected_pages, 'found': len(page_files)})
    return {
        'status': 'all_data_checks_passed' if all(c['passed'] for c in checks)
        else 'failed',
        'app': 'xhs',
        'checks': checks,
    }
