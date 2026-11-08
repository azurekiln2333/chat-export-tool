"""Decode classic Windows QQ Msg3.0 rows from an already decrypted snapshot.

This is separate from Android QQ's XOR fields and QQ NT's protobuf schema.
The classic message body is a fixed header followed by bounded TLV elements.
Protocol references (no upstream implementation is included):
https://github.com/Akegarasu/qmsg-unpacker/blob/main/qqmsg/msg.go
https://github.com/shenjackyuanjie/qqdb-decode/blob/main/src/raw.rs

Unknown fields/elements and malformed bodies stay available in the raw row.
No source paths are opened here and no schema or database rows are modified.
"""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from ..errors import ExportError

MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_ELEMENTS = 65536
_LABELS = {1: "文本", 2: "表情", 3: "图片", 6: "图片", 7: "语音", 26: "视频"}
_MEDIA_TYPES = {3, 6, 7, 26}


def _raw(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _quoted(value):
    return '"' + value.replace('"', '""') + '"'


def _tlvs(data, warnings, context):
    """Keep a valid prefix on truncation; never read beyond an element's end."""
    offset = 0
    count = 0
    while offset < len(data):
        if count >= MAX_ELEMENTS:
            warnings.append(context + "元素数量超过解析上限；剩余字节保留在原始正文")
            return
        if len(data) - offset < 3:
            warnings.append(context + "末尾 TLV 头不完整")
            return
        kind = data[offset]
        size = int.from_bytes(data[offset + 1:offset + 3], "little")
        start = offset
        offset += 3
        if size > len(data) - offset:
            warnings.append(context + "元素长度超出正文边界")
            return
        yield kind, data[offset:offset + size], start
        offset += size
        count += 1


def _utf16(data, warnings, context):
    try:
        return data.decode("utf-16-le")
    except UnicodeDecodeError:
        warnings.append(context + "包含不完整的 UTF-16LE；原始字节已保留")
        return data.decode("utf-16-le", errors="replace")


def decode_body(value):
    """Return display content plus lossless raw-backed decoding diagnostics.

    ``header.random_le`` / ``random_be`` are explicitly byte interpretations:
    published decoders disagree on endian order, so SQL ``Rand`` remains the
    authoritative identity field. Media references are metadata, not local files.
    """
    result = {"text": "", "type_name": "其他", "sender_nickname": "", "header": {},
              "elements": [], "media_refs": [], "status": "unrecognized", "warnings": []}
    warnings = result["warnings"]
    if isinstance(value, str):
        result.update(text=value, type_name="文本", status="plaintext")
        return result
    if value is None or value == b"":
        result.update(text="[空消息正文]", status="empty")
        return result
    if not isinstance(value, (bytes, bytearray, memoryview)):
        result["text"] = "[未识别消息正文类型：原始数据已保留]"
        warnings.append("MsgContent 不是文本或二进制字段")
        return result
    data = bytes(value)
    if len(data) > MAX_BODY_BYTES:
        result["text"] = "[超大消息正文：原始数据已保留]"
        warnings.append("MsgContent 超过解析大小上限")
        return result
    if len(data) < 28:
        result["text"] = "[消息头不完整：原始数据已保留]"
        warnings.append("MsgContent 短于经典消息头")
        return result
    font_size = int.from_bytes(data[24:26], "little")
    body_offset = 28 + font_size
    if body_offset > len(data):
        result["text"] = "[消息头长度异常：原始数据已保留]"
        warnings.append("字体名称长度超出消息边界")
        return result
    result["header"] = {
        "prefix": _raw(data[:8]), "time": int.from_bytes(data[8:12], "little"),
        "random_le": int.from_bytes(data[12:16], "little"),
        "random_be": int.from_bytes(data[12:16], "big"),
        "color_le": int.from_bytes(data[16:20], "little"),
        "font_size": data[20], "font_style": data[21], "charset": data[22],
        "font_family": data[23], "font_name": _utf16(data[26:26 + font_size], warnings, "字体名称"),
        "reserved": _raw(data[26 + font_size:body_offset]),
    }
    texts, labels = [], []
    for kind, payload, offset in _tlvs(data[body_offset:], warnings, "消息"):
        element = {"type": kind, "offset": body_offset + offset, "length": len(payload)}
        # Only known element types have a documented nested TLV representation.
        if kind not in {*_LABELS, 18}:
            element.update(status="unknown", raw=_raw(payload))
            result["elements"].append(element)
            texts.append(f"[未识别消息元素 {kind}]")
            labels.append("其他")
            warnings.append(f"未识别消息元素 {kind}；原始字节已保留")
            continue
        before = len(warnings)
        fields = list(_tlvs(payload, warnings, f"消息元素 {kind} 的"))
        element["fields"] = [{"type": tag, "offset": at, "value": _raw(field)} for tag, field, at in fields]
        first = next((field for tag, field, _ in fields if tag == 1), None)
        if kind == 18:
            nick = next((field for tag, field, _ in fields if tag in {1, 2}), None)
            if nick is not None:
                element["nickname"] = _utf16(nick, warnings, "发送者昵称")
                result["sender_nickname"] = element["nickname"]
            else:
                warnings.append("昵称元素缺少已知字段")
        elif kind == 1:
            fragments = [_utf16(field, warnings, "消息文本") for tag, field, _ in fields if tag == 1]
            if not fragments:
                warnings.append("文本元素缺少已知文本字段")
            element["text"] = "".join(fragments) if fragments else "[未识别文本元素]"
            texts.append(element["text"])
            labels.append("文本")
        elif kind == 2:
            if first is not None and len(first) <= 8:
                element["face_id"] = int.from_bytes(first, "big")
                texts.append(f"[表情 {element['face_id']}]")
            else:
                warnings.append("表情元素缺少有效编号")
                texts.append("[表情]")
            labels.append("表情")
        else:
            media = {"kind": _LABELS[kind], "element_type": kind}
            if kind in {3, 6}:
                if first is not None:
                    media["hash"] = first.hex()
                path = next((field for tag, field, _ in fields if tag == 2), None)
                if path is not None:
                    media["recorded_path"] = _utf16(path, warnings, "图片路径")
            elif kind == 7 and first is not None:
                media["hash"] = first.hex()
            elif kind == 26:
                if first is not None and len(first) >= 260:
                    media["hash"] = bytes(byte ^ 0xEF for byte in first[244:260]).hex()
                else:
                    warnings.append("视频元素缺少完整摘要字段")
            if first is None:
                warnings.append("媒体元素缺少已知摘要字段")
            element["media"] = media
            result["media_refs"].append(media)
            texts.append("[" + media["kind"] + (": " + media["recorded_path"] if media.get("recorded_path") else "") + "]")
            labels.append(media["kind"])
        element["status"] = "partial" if len(warnings) != before else "decoded"
        result["elements"].append(element)
    result["text"] = "".join(texts) if texts else "[无可显示消息元素：原始数据已保留]"
    result["type_name"] = labels[0] if len(set(labels)) == 1 else ("混合消息" if labels else "其他")
    result["status"] = "partial" if warnings else ("decoded" if labels else "empty")
    return result


def _table_identity(table):
    # The classic table suffix is an account/group UIN. A group UIN is not
    # assumed to be the public group code stored by NT; keep separate aliases.
    match = re.search(r"(?:^|_)(buddy|group)_(\d+)$", table, re.I)
    return (match.group(1).lower(), match.group(2)) if match else ("unknown", table)


def message_tables(db):
    """Discover every MsgContent table, including unfamiliar legacy variants."""
    selected = []
    tables = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    for table in tables:
        if table.startswith("sqlite_"):
            continue
        columns = {row[1].casefold() for row in db.execute("PRAGMA table_info(" + _quoted(table) + ")")}
        if "msgcontent" in columns:
            selected.append(table)
        elif _table_identity(table)[0] != "unknown":
            raise ExportError("旧版 QQ 消息表缺少 MsgContent 字段，不能略过：" + table)
    return selected


def _integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (str, bytes)):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _uin(value):
    number = _integer(value)
    return str(number) if number is not None and number > 0 else ""


def _timestamp(value):
    number = _integer(value)
    if number is None or number <= 0:
        return 0
    return number * 1000 if number < 100_000_000_000 else number


def records(db, descriptor, relative, table, names=None, *, cancel=None):
    """Yield all rows in the shared desktop interchange; never dedup with NT.

    The database must be an already decrypted, validated private snapshot.
    Identity includes account, table, SQL time/random/sender and body digest;
    row offsets, snapshot paths and displayed names cannot change that identity.
    ``Info`` and all other source columns remain unchanged in ``_desktop_raw``.
    """
    names = names or {}
    kind, talker = _table_identity(table)
    uid, account = descriptor["uid"], str(descriptor["account"])
    app = descriptor.get("app", "qq_pc")
    timezone = ZoneInfo(descriptor.get("timezone", "Asia/Shanghai"))
    conv_key = "legacy:" + ("peer" if kind == "buddy" else kind) + ":" + talker
    conversation = conv_key + "@" + uid
    with closing(db.execute("SELECT * FROM " + _quoted(table))) as cursor:
        columns = [item[0] for item in cursor.description]
        for values in cursor:
            if cancel and cancel():
                raise ExportError("旧版 QQ 记录处理已取消；工作副本保留。")
            row = dict(zip(columns, values))
            lower = {key.casefold(): value for key, value in row.items()}
            decoded = decode_body(lower.get("msgcontent"))
            sender_uin = _uin(lower.get("senderuin"))
            own = bool(sender_uin) and sender_uin == account
            sender = uid if own else (sender_uin or "未知发送者")
            timestamp = _timestamp(lower.get("time"))
            time_source = "Time"
            if not timestamp:
                timestamp = _timestamp(decoded["header"].get("time"))
                time_source = "MsgContent.header.time" if timestamp else "unavailable"
            try:
                shown_time = datetime.fromtimestamp(timestamp / 1000, timezone).strftime("%Y-%m-%d %H:%M:%S") if timestamp else ""
            except (OverflowError, OSError, ValueError):
                shown_time = ""
            identity = [account, table, _raw(lower.get("time")), _raw(lower.get("rand")),
                        _raw(lower.get("senderuin")), hashlib.sha256(_json(_raw(lower.get("msgcontent"))).encode()).hexdigest()]
            if not sender_uin or not _timestamp(lower.get("time")) or _integer(lower.get("rand")) is None:
                # Unknown tables have no established message identity. Conservatively
                # include every raw column rather than merge distinct records.
                identity.append({key: _raw(value) for key, value in row.items()})
            key = app + ":legacy:" + hashlib.sha256(_json(identity).encode()).hexdigest()
            known_types = [element["type"] for element in decoded["elements"] if element["type"] != 18]
            typ = known_types[0] if len(set(known_types)) == 1 else 0
            if decoded["status"] == "plaintext":
                typ = 1
            name = names.get(talker, talker if kind != "unknown" else "旧版 QQ 会话 · " + table)
            yield {
                "msg_uuid": key, "conversation_id": conversation, "sender": sender,
                "created_time": timestamp, "type": typ, "msg_server_id": "",
                "_desktop_raw": {"database": relative, "table": table, "row": {key: _raw(value) for key, value in row.items()}},
                "_export": {"app": app, "source_key": key, "source_conversation": conv_key,
                            "account_uid": uid, "account_identifier": account, "time": shown_time,
                            "text": decoded["text"], "type_name": decoded["type_name"],
                            "conversation_name": name, "conversation_id": conversation,
                            "sender_name": "我" if own else (decoded["sender_nickname"] or names.get(sender, sender)),
                            "is_group": kind == "group", "direction_known": bool(sender_uin), "local_media": [],
                            "media_status": "missing" if any(typ in _MEDIA_TYPES for typ in known_types) else "none",
                            "legacy_format": "msg3.0", "legacy_table_kind": kind, "legacy_time_source": time_source,
                            "legacy_decode_status": decoded["status"], "legacy_decode_warnings": decoded["warnings"],
                            "legacy_header": decoded["header"], "legacy_elements": decoded["elements"],
                            "legacy_media_refs": decoded["media_refs"]},
            }
