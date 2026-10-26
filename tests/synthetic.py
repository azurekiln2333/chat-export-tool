"""Synthetic export fixtures for the automated tests.

Everything here is generated at run time: no chat content, account ID or file
from a real backup is stored in the repository. Account IDs are arbitrary and
deliberately different from any real one.

The fixtures deliberately do not reuse the package exporter or reader; they
build a complete export tree independently so that ``validate_export`` is
tested against a realistic but separate producer. Assets are tiny generated
files (PNG/GIF/WAV) so the tests stay fast and deterministic.
"""

from __future__ import annotations

import binascii
import hashlib
import html
import importlib
import json
from pathlib import Path
import shutil
import struct
import tarfile
import unittest
import wave
import zlib

ACCOUNT_ME = "9000000000000001"
ACCOUNT_OTHER = "7012345678901234"

# The verified Android ``msg`` table schema (column names and order).
MSG_COLUMNS = [
    "msg_uuid", "msg_server_id", "conversation_id", "conversation_short_id",
    "conversation_type", "type", "index_in_conversation", "order_index", "status",
    "net_status", "version", "deleted", "created_time", "sender", "content", "ext",
    "local_info", "read_status", "sec_sender", "property_list",
    "index_in_conversation_v2", "table_flag", "need_build_fts_index", "label_str_list",
    "content_byte", "thread_id", "sub_type",
]

MSG_DDL = """CREATE TABLE msg(msg_uuid TEXT PRIMARY KEY,msg_server_id BIGINT,
conversation_id TEXT NOT NULL,conversation_short_id BIGINT,conversation_type INTEGER,
type INTEGER,index_in_conversation BIGINT,order_index BIGINT,status INTEGER,
net_status INTEGER,version INTEGER,deleted INTEGER,created_time INTEGER,sender BIGINT,
content TEXT,ext TEXT,local_info TEXT,read_status INTEGER,sec_sender TEXT,
property_list TEXT,index_in_conversation_v2 BIGINT default -1,table_flag BIGINT default 0,
need_build_fts_index INTEGER default 1,label_str_list TEXT,content_byte BLOB,
thread_id TEXT,sub_type INTEGER DEFAULT -1)"""

SIDE_TABLES_DDL = [
    "CREATE TABLE attchment (uuid TEXT NOT NULL,local_uri TEXT,remote_uri TEXT,size BIGINT,"
    "type TEXT,hash TEXT,position INTEGER,status INTEGER,ext TEXT,display_type TEXT,mime_type TEXT)",
    "CREATE TABLE conversation_core (conversation_id TEXT PRIMARY KEY,info_version BIGINT,"
    "name TEXT,desc TEXT,icon TEXT,notice TEXT,owner_id INTEGER DEFAULT -1,sec_owner TEXT,"
    "silent INTEGER DEFAULT 0,silent_normal_only INTEGER DEFAULT 0,mode INTEGER DEFAULT -1,"
    "ext TEXT,creator_uid INTEGER DEFAULT -1,create_time INTEGER,silent_source INTEGER DEFAULT 0,"
    "silent_util_time INTEGER DEFAULT 0)",
    "CREATE TABLE conversation_list (conversation_id TEXT PRIMARY KEY,short_id BIGINT,type INTEGER,"
    "role INTEGER,last_msg_index BIGINT,updated_time INTEGER,unread_count INTEGER,read_index BIGINT,"
    "inbox INTEGER,min_index BIGINT,drafted_time INTEGER,ticket TEXT,draft_content TEXT,"
    "local_info TEXT,is_member INTEGER,has_more INTEGER,member_count INTEGER,status INTEGER,"
    "participant TEXT,last_show_msg_uuid TEXT,last_msg_order_index BIGINT,stranger INTEGER default 0)",
    "CREATE TABLE participant (user_id INTEGER NOT NULL,sort_order INTEGER,role INTEGER,"
    "conversation_id TEXT,alias TEXT,sec_uid TEXT,silent INTEGER,silent_time INTEGER,"
    "USER_LOCAL_CREATE_TIME INTEGER,COLUMN_MENTION_TIME INTEGER,COLUMN_USER_INFO_EXT TEXT)",
]


# --------------------------------------------------------------------------- helpers


def import_or_skip(name: str):
    """Import a package module, or skip the test while its owner has not landed it."""
    try:
        return importlib.import_module(name)
    except ImportError as error:  # pragma: no cover - depends on delivery order
        raise unittest.SkipTest(f"module not available yet: {name} ({error})") from error


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png_bytes(width: int = 3, height: int = 3, color=(30, 60, 90)) -> bytes:
    """Minimal valid RGB PNG, no third-party dependency."""
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", binascii.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def wav_bytes(seconds: float = 0.2, rate: int = 8000) -> bytes:
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = int(seconds * rate)
        handle.writeframes(b"".join(struct.pack("<h", (index % 64) * 200 - 6000)
                                    for index in range(frames)))
    return buffer.getvalue()


def write_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def csv_value(value, identifier=False) -> str:
    """Neutralise spreadsheet formulas and long identifiers, as the exporter must."""
    text = "" if value is None else str(value)
    if (identifier and text.isdecimal() and len(text) > 15) \
            or text.lstrip().startswith(("=", "+", "-", "@")) \
            or text.startswith(("\t", "\r")):
        return "'" + text
    return text


def write_csv(path: Path, headings, rows, id_columns=()) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headings)
        for row in rows:
            writer.writerow([csv_value(value, index in id_columns)
                             for index, value in enumerate(row)])


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- messages


def message_row(uuid: str, *, msg_type: int, content, sender: int, created: int,
                conversation: str = "0:1:9000000000000001:4174180243746380",
                deleted: int = 0, server_id: int = 7549955327504959011,
                ext: str = "", local_info: str = "", sub_type: int = -1) -> dict:
    row = {column: None for column in MSG_COLUMNS}
    row.update({
        "msg_uuid": uuid,
        "msg_server_id": server_id,
        "conversation_id": conversation,
        "conversation_short_id": 7370387642841629193,
        "conversation_type": 1,
        "type": msg_type,
        "index_in_conversation": created * 1000,
        "order_index": created * 1000000,
        "status": 5,
        "net_status": 0,
        "version": 0,
        "deleted": deleted,
        "created_time": created,
        "sender": sender,
        "content": dump(content) if not isinstance(content, str) else content,
        "ext": ext,
        "local_info": local_info,
        "read_status": 1,
        "sec_sender": "",
        "property_list": "",
        "index_in_conversation_v2": 2849,
        "table_flag": 0,
        "need_build_fts_index": 1,
        "label_str_list": "",
        "content_byte": None,
        "thread_id": None,
        "sub_type": sub_type,
    })
    return row


def base_rows(account: str) -> list[dict]:
    """A small conversation covering every case the validator cares about."""
    own = int(account)
    peer = 4174180243746380
    created = 1757860962579
    bubble_ext = dump({"a:chat_bubble": {"bubble_id": "7100000000000000001"}})
    rows = [
        message_row("11111111-1111-4111-8111-111111111111", msg_type=7,
                    content={"text": "plain greeting"}, sender=own, created=created),
        message_row("22222222-2222-4222-8222-222222222222", msg_type=7,
                    content={"text": "<script>alert(1)</script> & <b>x</b>"},
                    sender=peer, created=created + 1000),
        message_row("33333333-3333-4333-8333-333333333333", msg_type=7,
                    content={"text": "=SUM(A1:A2)"}, sender=peer, created=created + 2000),
        message_row("44444444-4444-4444-8444-444444444444", msg_type=7,
                    content={"text": "hello [smile] world"}, sender=own,
                    created=created + 3000, ext=bubble_ext),
        message_row("55555555-5555-4555-8555-555555555555", msg_type=5,
                    content={"display_name": "Happy", "url": {"uri": "ies.fe.effect/abc",
                                                              "url_list": ["https://example.invalid/a"]}},
                    sender=peer, created=created + 4000),
        message_row("66666666-6666-4666-8666-666666666666", msg_type=17,
                    content={"duration": 1500, "url": {"url_list": ["https://example.invalid/v"]}},
                    sender=own, created=created + 5000),
        message_row("77777777-7777-4777-8777-777777777777", msg_type=9999,
                    content={"unknown_field": [1, 2, {"nested": "kept"}]},
                    sender=peer, created=created + 6000),
        message_row("88888888-8888-4888-8888-888888888888", msg_type=1,
                    content={"tips": "System notice"}, sender=peer,
                    created=created + 7000),
        message_row("99999999-9999-4999-8999-999999999999", msg_type=7,
                    content={"text": "flagged row"}, sender=peer,
                    created=created + 8000, deleted=1),
        message_row("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", msg_type=7,
                    content={"text": "unavailable sticker"},
                    sender=peer, created=created + 9000),
    ]
    return rows


# --------------------------------------------------------------------------- HTML


def _escape(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_page(rows, meta_by_uuid, decorations, media_by_uuid, title: str) -> str:
    """Build one conversation page with the classes the reader and validator expect."""
    bubbles = decorations.get("bubbles") or {}
    inline = decorations.get("inline_emoji") or {}
    articles = []
    for row in rows:
        uuid = row["msg_uuid"]
        meta = meta_by_uuid[uuid]
        resource = media_by_uuid.get(uuid) or {}
        bubble = meta.get("bubble") or {}
        classes = "bubble"
        if row["type"] == 5 and resource.get("status") == "ok":
            classes += " sticker-bubble"
        applied = bool(bubble.get("applied"))
        if applied:
            classes += f" original-bubble theme-{bubble['id']} side-{bubble['side']}"
        body = [f'<div class="{classes}"><div class="bubble-content">']
        if row["type"] == 5 and resource.get("status") == "ok":
            url = "../../" + resource["path"]
            body.append(f'<a class="sticker-link" href="{url}"><img class="sticker" src="{url}" '
                        f'alt="{_escape(meta["text"])}"></a>')
        else:
            body.append(_inline_text(meta["text"], inline))
        if resource.get("status") == "ok" and resource.get("kind") == "voice":
            url = "../../" + resource["path"]
            body.append(f'<audio controls preload="none" src="{url}" '
                        f'aria-label="Voice message"><a href="{url}">Open voice file</a></audio>')
        elif resource and resource.get("status") != "ok":
            body.append('<small class="media-unavailable">Media unavailable</small>')
        body.append("</div>")
        if applied:
            corners = (bubbles.get(bubble["id"]) or {}).get("corners") or {}
            for corner in ("top_left", "top_right", "bottom_left", "bottom_right"):
                asset = corners.get(corner + ("_self" if bubble["side"] == "1" else ""))
                if not asset:
                    continue
                body.append(f'<img class="bubble-corner corner-{corner.replace("_", "-")}" '
                            f'src="../../{asset["path"]}" alt="" aria-hidden="true">')
        body.append("</div>")
        articles.append(
            f'<article class="message" id="msg-{uuid}">'
            f'<header><strong>{_escape(meta["sender_name"])}</strong>'
            f'<span>{_escape(meta["time"])}</span></header>'
            + "".join(body) + "</article>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{_escape(title)}</title>"
        '<link rel="stylesheet" href="../../reader.css"></head><body><main>'
        f'<a class="back" href="../index.html">Conversations</a><h1>{_escape(title)}</h1>'
        + "".join(articles) + "</main></body></html>")


def _inline_text(text: str, inline: dict) -> str:
    """Escape text, replacing known bracket tokens with local inline-emoji images."""
    out = []
    position = 0
    while True:
        start = text.find("[", position)
        end = text.find("]", start + 1) if start >= 0 else -1
        if start < 0 or end < 0:
            break
        token = text[start:end + 1]
        asset = inline.get(token)
        if not asset:
            position = start + 1
            continue
        out.append(_escape(text[position:start]))
        out.append(f'<img class="inline-emoji" src="../../{asset["path"]}" '
                   f'alt="{_escape(token)}" title="{_escape(token)}">')
        position = end + 1
    out.append(_escape(text[position:]))
    return "".join(out)


# --------------------------------------------------------------------------- export


def _media_entries(account: str, rows: list[dict]):
    """Decide the media manifest entry for each message and build the asset files."""
    entries = {}
    uuid_by_type = {row["type"]: row["msg_uuid"] for row in rows}
    sticker_uuid = uuid_by_type.get(5)
    voice_uuid = uuid_by_type.get(17)
    if sticker_uuid:
        entries[sticker_uuid] = {"kind": "emoji", "status": "ok",
                                 "resource_id": "emoji_synthetic_sticker",
                                 "path": "attachments/stickers/sticker.png",
                                 "bytes": None, "width": 3, "height": 3, "frames": 1,
                                 "format": "PNG", "mime": "image/png"}
    if voice_uuid:
        entries[voice_uuid] = {"kind": "voice", "status": "ok",
                               "resource_id": "voice_synthetic_voice",
                               "path": "attachments/voice/voice.wav",
                               "bytes": None, "mime": "audio/wav",
                               "codec": "pcm_s16le", "duration": 0.2}
    unavailable = next((row["msg_uuid"] for row in rows
                        if row["type"] == 7 and "unavailable" in (row["content"] or "")), None)
    if unavailable:
        entries[unavailable] = {"kind": "emoji", "status": "missing_url",
                                "resource_id": "emoji_synthetic_missing"}
    return entries


def _decorations() -> dict:
    return {
        "bubbles": {
            "7100000000000000001": {
                "sides": {
                    side: {"path": f"attachments/bubbles/7100000000000000001/{name}.png",
                           "width": 3, "height": 3,
                           "nine_patch": {"padding": [1, 1, 1, 1],
                                          "slice": [1, 1, 1, 1], "scale": 3}}
                    for side, name in (("1", "self"), ("0", "guest"))
                },
                "properties": {"tc": "#112233"},
                "corners": {
                    "top_left_self": {"path": "attachments/bubbles/7100000000000000001/top_left_self.png",
                                      "width": 1, "height": 1, "frames": 1},
                },
            }
        },
        "inline_emoji": {
            "[smile]": {"path": "attachments/inline_emoji/smile.png",
                        "width": 3, "height": 3, "version": 1},
        },
    }


def build_export(root: Path, accounts=None, *, with_media: bool = True,
                 with_decorations: bool = True, page_size: int = 600,
                 protect_csv: bool = True) -> dict:
    """Create a complete synthetic export tree and return handles for tampering."""
    import sqlite3

    root = Path(root)
    accounts = list(accounts or [ACCOUNT_ME])
    decorations = _decorations() if with_decorations else {"bubbles": {}, "inline_emoji": {}}
    media_messages: dict[str, dict] = {}
    manifest_resources: list[dict] = []
    summaries = []
    counters = {"emoji_ok": 0, "voice_ok": 0, "inline_emoji_occurrences": 0,
                "original_bubble_messages": 0}
    row_by_uuid: dict[str, dict] = {}
    meta_by_uuid: dict[str, dict] = {}
    media_by_uuid: dict[str, dict] = {}

    # ---- pass 1: decide content and finish every asset before it is referenced
    per_account: dict[str, tuple[list[dict], dict]] = {}
    for account in accounts:
        rows = base_rows(account)
        entries = _media_entries(account, rows) if with_media else {}
        per_account[account] = (rows, entries)
        for row in rows:
            resource = entries.get(row["msg_uuid"])
            if resource:
                media_messages[f"{account}:{row['msg_uuid']}"] = resource

    assets = {}
    if with_media:
        assets["emoji"] = write_bytes(root / "attachments/stickers/sticker.png", png_bytes())
        assets["voice"] = write_bytes(root / "attachments/voice/voice.wav", wav_bytes())
        for resource in media_messages.values():
            if resource.get("status") != "ok":
                continue
            target = assets[resource["kind"]]
            resource["bytes"] = target.stat().st_size
            resource["sha256"] = sha256_file(target)
            manifest_resources.append(dict(resource))

    if with_decorations:
        for bubble in decorations["bubbles"].values():
            for asset in list(bubble["sides"].values()) + list((bubble.get("corners") or {}).values()):
                path = write_bytes(root / asset["path"], png_bytes(asset["width"], asset["height"]))
                asset["sha256"] = sha256_file(path)
        for asset in decorations["inline_emoji"].values():
            write_bytes(root / asset["path"], png_bytes(asset["width"], asset["height"]))

    # ---- pass 2: write one folder per account
    for account in accounts:
        rows, entries = per_account[account]
        folder = root / account
        for row in rows:
            uuid = row["msg_uuid"]
            row_by_uuid[uuid] = row
            resource = entries.get(uuid) or {}
            meta = {
                "account_uid": account,
                "conversation_name": "Demo conversation",
                "time": "2025-09-14 22:42:42",
                "timezone": "Asia/Shanghai",
                "sender_name": "Me" if str(row["sender"]) == account else "Peer",
                "type_name": "Text" if row["type"] == 7 else f"Type {row['type']}",
                "text": _readable(row),
                "local_media": [resource["path"]] if resource.get("status") == "ok" else [],
                "bubble": _bubble_meta(row, account, decorations),
            }
            if resource:
                meta["media"] = resource
                if resource.get("status") == "ok":
                    counters["emoji_ok" if resource["kind"] == "emoji" else "voice_ok"] += 1
            meta_by_uuid[uuid] = meta
            media_by_uuid[uuid] = resource
            counters["inline_emoji_occurrences"] += meta["text"].count("[smile]") \
                if decorations.get("inline_emoji") else 0
            if meta["bubble"].get("applied"):
                counters["original_bubble_messages"] += 1

        folder.mkdir(parents=True, exist_ok=True)
        _write_database(folder / "chat.sqlite", rows)
        _write_jsonl(folder / "messages.jsonl", rows, meta_by_uuid, account)
        _write_messages_csv(folder / "messages.csv", account, rows, meta_by_uuid,
                            protect=protect_csv)
        write_csv(folder / "contacts.csv",
                  ["User ID", "Nickname", "Remark", "Douyin ID", "Short ID", "Deleted"],
                  [[account, "Me", "", "", "", 0], ["4174180243746380", "Peer", "", "", "", 0]],
                  (0,))
        (folder / "contacts.json").write_text(
            json.dumps([{"uid": account, "nickname": "Me", "remark_name": ""},
                        {"uid": "4174180243746380", "nickname": "Peer", "remark_name": ""}],
                       ensure_ascii=False, indent=1), encoding="utf-8")
        (folder / "conversation_metadata.json").write_text(
            json.dumps({"conversation_core": [], "conversation_list": [],
                        "participant": [], "attchment": []}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        write_csv(folder / "conversation_index.csv",
                  ["Conversation", "Conversation ID", "Messages", "First Time", "Last Time"],
                  [["Demo conversation", "0:1:9000000000000001:4174180243746380", len(rows),
                    "2025-09-14 22:42:42", "2025-09-14 22:42:50"]], (1,))
        pages = folder / "conversations"
        pages.mkdir(exist_ok=True)
        page_name = "conversation_001_page_001.html"
        (pages / page_name).write_text(
            render_page(rows, meta_by_uuid, decorations, media_by_uuid, "Demo conversation"),
            encoding="utf-8")
        transcripts = folder / "transcripts"
        transcripts.mkdir(exist_ok=True)
        (transcripts / "conversation_001.txt").write_text(
            "Conversation: Demo conversation\nMessages: %d\n" % len(rows), encoding="utf-8")
        (folder / "index.html").write_text(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<link rel="stylesheet" href="../reader.css"></head><body>'
            f'<a href="conversations/{page_name}">First</a>'
            '<a href="messages.csv">Messages CSV</a>'
            '<a href="chat.sqlite">Decrypted database</a></body></html>', encoding="utf-8")
        statistics = {
            "uid": account, "nickname": "Me", "message_count": len(rows),
            "conversation_count": 1, "text_message_count": 6, "deleted_flag_count": 1,
            "first": "2025-09-14 22:42:42", "last": "2025-09-14 22:42:50",
            "message_types": {"7": 6}, "conversations": [
                {"conversation_id": "0:1:9000000000000001:4174180243746380",
                 "name": "Demo conversation", "count": len(rows),
                 "page": f"conversations/{page_name}",
                 "latest_page": f"conversations/{page_name}",
                 "txt": "transcripts/conversation_001.txt"}],
        }
        (folder / "statistics.json").write_text(
            json.dumps(statistics, ensure_ascii=False, indent=1), encoding="utf-8")
        summaries.append({"uid": account, "message_count": len(rows),
                          "conversation_count": 1})

    if with_media:
        (root / "media_manifest.json").write_text(json.dumps({
            "resources": manifest_resources,
            "messages": media_messages,
            "statistics": {"voice": {"messages_downloaded": counters["voice_ok"]},
                           "emoji": {"messages_downloaded": counters["emoji_ok"]}},
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    if with_decorations:
        (root / "decoration_manifest.json").write_text(
            json.dumps(decorations, ensure_ascii=False, indent=1), encoding="utf-8")

    css = [".message{padding:0}"]
    if with_decorations:
        css.append('.original-bubble::before{border-image-source:url("'
                   'attachments/bubbles/7100000000000000001/self.png");}')
    (root / "reader.css").write_text("\n".join(css) + "\n", encoding="utf-8")
    (root / "index.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="reader.css"></head><body>'
        + "".join(f'<a href="{account}/index.html">{account}</a>' for account in accounts)
        + "</body></html>", encoding="utf-8")
    summary = {
        "app": "Duoshan", "app_version": "40.1.0", "timezone": "Asia/Shanghai",
        "total_message_count": sum(item["message_count"] for item in summaries),
        "total_text_message_count": 6, "accounts": summaries, "local_media_files": [],
        "media_and_presentation": {"rendered": dict(counters)},
        "exported_at": "2026-10-27T00:00:00+08:00",
    }
    (root / "export_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return {
        "root": root,
        "accounts": accounts,
        "rows": row_by_uuid,
        "meta": meta_by_uuid,
        "media": media_by_uuid,
        "media_messages": media_messages,
        "decorations": decorations,
        "summary": summary,
        "page_size": page_size,
    }


def _readable(row: dict) -> str:
    try:
        content = json.loads(row["content"] or "{}")
    except (TypeError, ValueError):
        return str(row["content"])
    if isinstance(content, dict):
        for key in ("text", "display_name", "tips"):
            value = content.get(key)
            if isinstance(value, str):
                return value
        if row["type"] == 17:
            return "Voice message"
        if row["type"] == 5:
            return "Sticker"
        return f"[Type {row['type']}]"
    return str(content)


def _bubble_meta(row: dict, account: str, decorations: dict) -> dict:
    bubbles = decorations.get("bubbles") or {}
    try:
        ext = json.loads(row.get("ext") or "{}")
    except ValueError:
        ext = {}
    explicit = (ext.get("a:chat_bubble") or {}).get("bubble_id")
    bubble_id = str(explicit) if explicit else "-1"
    side = "1" if str(row["sender"]) == account else "0"
    available = bool(((bubbles.get(bubble_id) or {}).get("sides") or {}).get(side, {}).get("path"))
    eligible = row["type"] in {7, 8, 17, 21, 77, 83, 105, 114, 118}
    return {"id": bubble_id, "side": side, "source": "a:chat_bubble" if explicit else "default",
            "available": available, "applied": bool(available and eligible)}


def _write_database(path: Path, rows: list[dict]) -> None:
    import sqlite3

    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.execute(MSG_DDL)
        for statement in SIDE_TABLES_DDL:
            connection.execute(statement)
        placeholders = ",".join("?" for _ in MSG_COLUMNS)
        connection.executemany(
            f"INSERT INTO msg ({','.join(MSG_COLUMNS)}) VALUES ({placeholders})",
            [[row[column] for column in MSG_COLUMNS] for row in rows])
        connection.commit()
    finally:
        connection.close()


def _write_jsonl(path: Path, rows, meta_by_uuid, account: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            payload = dict(row)
            payload["_export"] = meta_by_uuid[row["msg_uuid"]]
            handle.write(dump(payload) + "\n")


CSV_HEADINGS = ["Account UID", "Conversation", "Conversation ID", "Time (UTC+08:00)",
                "Sender", "Sender ID", "Message Type", "Type Code", "Text", "Deleted",
                "Message UUID", "Server Message ID", "Local Attachments"]
CSV_ID_COLUMNS = (0, 2, 5, 11)


def _write_messages_csv(path: Path, account: str, rows, meta_by_uuid, *, protect: bool) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADINGS)
        for row in rows:
            meta = meta_by_uuid[row["msg_uuid"]]
            values = [account, meta["conversation_name"], row["conversation_id"],
                      meta["time"], meta["sender_name"], str(row["sender"]),
                      meta["type_name"], row["type"], meta["text"], row["deleted"],
                      row["msg_uuid"], str(row["msg_server_id"]),
                      " | ".join(meta.get("local_media") or [])]
            if protect:
                values = [csv_value(value, index in CSV_ID_COLUMNS)
                          for index, value in enumerate(values)]
            writer.writerow(values)


# --------------------------------------------------------------------------- source


USER_DDL = ("CREATE TABLE user (uid INTEGER PRIMARY KEY, nickname TEXT, remark_name TEXT,"
            " unique_id TEXT, short_id TEXT, deleted INTEGER DEFAULT 0, sec_uid TEXT)")


def write_contacts_database(path: Path, uid: str, contacts=None) -> Path:
    """A plaintext contact cache in the layout the tool reads."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    rows = list(contacts or [(int(uid), "Me", "", "", "", 0, ""),
                             (4174180243746380, "Peer", "", "", "", 0, "")])
    connection = sqlite3.connect(path)
    try:
        connection.execute(USER_DDL)
        connection.executemany("INSERT INTO user VALUES (?,?,?,?,?,?,?)", rows)
        connection.commit()
    finally:
        connection.close()
    return path


def build_wal_database(path: Path, rows: list[dict], extra_rows=None) -> Path:
    """Write a database whose newest commit lives only in a ``-wal`` sidecar.

    ``rows`` are written to the main database file in rollback-journal mode;
    ``extra_rows`` are then committed in WAL mode with checkpointing disabled and
    the file plus its ``-wal`` are copied while the connection is still open. The
    copy therefore holds committed rows that exist only in the sidecar, which is
    exactly the state a phone backup captures.
    """
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_database(path, rows)
    extra_rows = list(extra_rows or [])
    if not extra_rows:
        raise ValueError("build_wal_database needs rows that live only in the WAL")
    copy = path.with_name(path.name + ".walcopy")
    for suffix in ("", "-wal", "-shm"):
        leftover = Path(str(copy) + suffix)
        if leftover.exists():
            leftover.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        placeholders = ",".join("?" for _ in MSG_COLUMNS)
        connection.executemany(
            f"INSERT INTO msg ({','.join(MSG_COLUMNS)}) VALUES ({placeholders})",
            [[row[column] for column in MSG_COLUMNS] for row in extra_rows])
        connection.commit()
        shutil.copyfile(path, copy)
        wal = Path(str(path) + "-wal")
        if wal.is_file():
            shutil.copyfile(wal, Path(str(copy) + "-wal"))
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        stale = Path(str(path) + suffix)
        if stale.exists():
            stale.unlink()
    shutil.move(str(copy), str(path))
    copied_wal = Path(str(copy) + "-wal")
    if copied_wal.is_file():
        shutil.move(str(copied_wal), str(path) + "-wal")
    return path


def add_extra_local_audio(root: Path, account: str, uuid: str,
                          name: str = "local_note.m4a") -> str:
    """Give a message a legitimate local audio attachment with no manifest voice entry.

    This mirrors a real export where a message carries an audio file copied from
    the backup next to (or instead of) a downloaded voice resource. The extra
    ``<audio>`` element must not be mistaken for a manifest voice render, so the
    reader counter still equals the number of manifest voice messages.
    """
    root = Path(root)
    relative = f"attachments/voice/{name}"
    write_bytes(root / relative, wav_bytes())

    page = root / account / "conversations" / "conversation_001_page_001.html"
    text = page.read_text(encoding="utf-8")
    marker = f'id="msg-{uuid}"'
    index = text.index(marker)
    end = text.index("</article>", index)
    element = (f'<audio controls preload="none" src="../../{relative}" '
               f'aria-label="Local attachment"></audio>')
    page.write_text(text[:end] + element + text[end:], encoding="utf-8")

    path = root / account / "messages.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        row = json.loads(line)
        if row.get("msg_uuid") == uuid:
            local = row.setdefault("_export", {}).setdefault("local_media", [])
            if relative not in local:
                local.append(relative)
        out.append(dump(row))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return relative


def build_app_source(root: Path, uids=None, *, wal: bool = False,
                     rows_by_uid=None, wal_rows=None) -> Path:
    """Create an extracted Android app-data folder holding plaintext chat databases.

    With ``wal=True`` the newest committed rows exist only in the ``-wal`` sidecar
    of each account database, as they do in a real phone backup.
    """
    uids = list(uids or [ACCOUNT_ME])
    rows_by_uid = dict(rows_by_uid or {})
    wal_rows = dict(wal_rows or {})
    root = Path(root)
    databases = root / "databases"
    databases.mkdir(parents=True, exist_ok=True)
    for uid in uids:
        path = databases / f"encrypted_{uid}_im.db"
        rows = rows_by_uid.get(uid) or base_rows(uid)
        if wal:
            extra = wal_rows.get(uid) or [message_row(
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", msg_type=7,
                content={"text": "committed only in the write-ahead log"},
                sender=int(uid), created=1757860972579)]
            build_wal_database(path, rows, extra)
        else:
            _write_database(path, rows)
        write_contacts_database(databases / f"relation_uc_v3_{uid}", uid)
    (root / "files" / "im").mkdir(parents=True, exist_ok=True)
    (root / "files" / "im" / "sample.bin").write_bytes(b"\x00synthetic")
    (root / "files" / "keva" / "repo" / f"{uids[0]}_aweme_user_info").mkdir(
        parents=True, exist_ok=True)
    return root


def build_backup_archive(target: Path, uids=None, *, compress: bool = True,
                         members=None) -> Path:
    """Create a DataBackup-style tar (or tar.zst) archive with the Android layout."""
    import io
    import tempfile

    uids = list(uids or [ACCOUNT_ME])
    target = Path(target)
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary)
        _write_payload(staging, uids)
        mode = "w:gz" if compress else "w"
        with tarfile.open(target, mode) as archive:
            for path in sorted(staging.rglob("*")):
                archive.add(path, arcname=str(path.relative_to(staging)))
            for name, payload in (members or {}).items():
                data = payload.encode("utf-8") if isinstance(payload, str) else payload
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return target


def _write_payload(staging: Path, uids) -> None:
    base = staging / "my.maya.android" / "user_0"
    for uid in uids:
        databases = base / "databases"
        databases.mkdir(parents=True, exist_ok=True)
        path = databases / f"encrypted_{uid}_im.db"
        _write_database(path, base_rows(uid))
        write_contacts_database(databases / f"relation_uc_v3_{uid}", uid)
    (base / "files" / "im").mkdir(parents=True, exist_ok=True)
    (base / "files" / "im" / "sample.bin").write_bytes(b"\x00synthetic")


UNSAFE_MEMBERS = {
    "databases/../../escaped.db": b"not a database",
    "/absolute/databases/escaped.db": b"not a database",
    "databases/C:\\windows.db": b"not a database",
    "databases/..\\escaped.db": b"not a database",
}


def copy_tree(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return destination
