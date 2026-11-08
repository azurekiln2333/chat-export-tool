"""Windows WeChat/QQ snapshots and independent message-schema adapters.

Only private copies are opened. Unknown schemas/encryption fail explicitly.
Message schemas were cross-checked with upstream wxexport and QQNT exporters;
raw SQL values (including binary payloads) remain in every exported record.
"""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import closing, nullcontext
from datetime import datetime
import hashlib
import io
from itertools import groupby
import json
from pathlib import Path
import re
import sqlite3
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from ..desktop_sources import discover_desktop_sources, database_files
from ..errors import ExportError
from ..progress import StageReporter
from .desktop_crypto import decrypt_snapshot, recover_wechat_keys, recover_qq_keys

TYPE_LABELS = {1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频", 47: "表情", 48: "位置", 49: "应用消息", 50: "语音/视频通话", 62: "视频", 10000: "系统消息", 10002: "撤回消息"}
APP_LABELS = {1: "链接", 3: "音乐", 4: "链接", 5: "文章", 6: "文件", 8: "图片", 17: "位置共享",
              19: "聊天记录", 24: "笔记", 33: "小程序", 36: "小程序", 40: "聊天记录", 51: "视频号",
              57: "引用回复", 62: "拍一拍", 63: "直播", 74: "文件", 2000: "转账", 2001: "红包"}
QQ_TABLES = {
    "c2c_msg_table": ("peer", False),
    "group_msg_table": ("group", True),
    "discuss_msg_table": ("discuss", True),
    "c2c_temp_msg_table": ("temp", False),
    "dataline_msg_table": ("dataline", False),
    "service_assistant_msg_table": ("service", False),
    "ai_assistant_msg_table": ("ai", False),
    "group_ai_assistant_msg_table": ("group_ai", True),
}


def _check(cancel):
    if cancel and cancel():
        raise ExportError("PC 聊天记录处理已取消；工作副本保留。")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _raw(value):
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return value


def _quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _hash(path, cancel=None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _check(cancel)
            data = stream.read(4 * 1024 * 1024)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def _separate(source, destination):
    if source == destination or source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ExportError("PC 数据源与工作/导出目录必须分开，不能在原始目录中解析。")


def _descriptor(source):
    found = discover_desktop_sources([source])
    if len(found) != 1:
        raise ExportError("请选择一个已识别的 PC 微信或 QQ 账号目录。")
    return found[0]


def prepare_backup(source, work, *, progress=print, cancel=None):
    """Copy a stable DB/WAL set first; retain failed attempts, never touch source."""
    source = Path(source).expanduser().resolve()
    work = Path(work).expanduser().resolve()
    descriptor = _descriptor(source)
    root = Path(descriptor["source"])
    _separate(root, work)
    work.mkdir(parents=True, exist_ok=True)
    files = database_files(root)
    if not files:
        raise ExportError("账号目录中没有受支持的消息数据库。")
    for attempt in range(3):
        target = work / ("snapshot-" + descriptor["uid"] + "-" + str(attempt))
        # Callers allocate fresh runs. Refuse silently mixing unrelated snapshots.
        if target.exists():
            from uuid import uuid4
            target = target.with_name(target.name + "-" + uuid4().hex[:8])
        target.mkdir()
        before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in files}
        expected = sum(size for size, _ in before.values())
        done = 0
        copied = {}
        with StageReporter(progress, "复制 PC 数据库到工作目录", completed=0, total=expected, unit="字节", cancel=cancel) as stage:
            for path in files:
                _check(cancel)
                relative = path.relative_to(root)
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                stage.update(detail=str(relative))
                with path.open("rb") as inp, destination.open("xb") as out:
                    while True:
                        _check(cancel)
                        block = inp.read(4 * 1024 * 1024)
                        if not block:
                            break
                        out.write(block)
                        digest.update(block)
                        done += len(block)
                        stage.update(completed=done, total=max(done, expected))
                copied[str(relative)] = digest.hexdigest()
        stable = set(database_files(root)) == set(files)
        with StageReporter(progress, "校验 PC 工作副本与源数据一致", completed=0, total=len(files), unit="文件", cancel=cancel) as stage:
            for index, path in enumerate(files, 1):
                _check(cancel)
                state = path.stat()
                stable &= before[str(path)] == (state.st_size, state.st_mtime_ns)
                relative = str(path.relative_to(root))
                stable &= _hash(path, cancel) == copied[relative]
                stable &= _hash(target / relative, cancel) == copied[relative]
                stage.update(completed=index, detail=relative)
        if stable:
            (target / "snapshot.json").write_text(_json({"account": descriptor["account"], "format": descriptor["format"], "files": copied}), encoding="utf-8")
            return descriptor, target
        progress("原客户端在复制期间修改了数据库，保留本次副本并重新复制。")
        files = database_files(root)
    raise ExportError("PC 数据库持续变化，无法取得一致副本。请退出对应客户端后重试；源文件未修改。")


def _message_file(path, fmt):
    if fmt == "wechat4":
        return path.parent.name == "message" and re.fullmatch(r"(?:biz_)?message_\d+\.db", path.name, re.I) is not None
    if fmt == "wechat3":
        return re.fullmatch(r"MSG\d*\.db", path.name, re.I) is not None
    return path.name.lower() in {"nt_msg.db", "msg3.0.db"}


def _database_format(relative, fmt):
    """A QQ account may retain both generations; dispatch each copied DB."""
    if fmt == "qqnt" and Path(relative).name.lower() in {"msg3.0.db", "misc.db", "info.db"}:
        return "qq_legacy"
    return fmt


def _prepare(source, work, *, keys, progress, cancel, sqlcipher_module_path):
    descriptor, snapshot = prepare_backup(source, work, progress=progress, cancel=cancel)
    originals = [p for p in database_files(snapshot) if p.suffix.lower() == ".db"]
    # Snapshot directory has no account name, but preserves the original layout.
    if not originals:
        raise ExportError("PC 工作副本缺少数据库。")
    resolved_keys = dict(keys) if isinstance(keys, dict) else keys
    if isinstance(resolved_keys, dict):
        for alias in (descriptor["uid"], descriptor["source"]):
            if alias in resolved_keys:
                resolved_keys[descriptor["account"]] = resolved_keys[alias]
    # A supplied key file is an explicit choice. Do not scan process memory
    # when the caller already provided credentials (including a wrong key).
    if descriptor["format"] in {"wechat4", "qqnt"} and descriptor.get("encrypted") and not resolved_keys:
        recover = recover_wechat_keys if descriptor["format"] == "wechat4" else recover_qq_keys
        recovered = recover(snapshot, progress=progress, cancel=cancel)
        if isinstance(resolved_keys, dict):
            resolved_keys = {**recovered, **resolved_keys}
        elif resolved_keys is None:
            resolved_keys = recovered
        elif recovered:
            resolved_keys = {**recovered, "default": resolved_keys}
    readable = []
    warnings = []
    failed_databases = []
    output = Path(work).resolve() / "decrypted"
    output.mkdir(parents=True, exist_ok=True)
    with StageReporter(progress, "解密并校验 PC 数据库", completed=0, total=len(originals), unit="个", cancel=cancel) as stage:
        for index, path in enumerate(originals, 1):
            _check(cancel)
            relative = path.relative_to(snapshot).as_posix()
            fmt = _database_format(relative, descriptor["format"])
            dest = output / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            stage.update(detail=relative)
            try:
                plain = decrypt_snapshot(path, dest, keys=resolved_keys, account=descriptor["account"], relative_path=relative,
                                         app=fmt, progress=progress, cancel=cancel,
                                         sqlcipher_module_path=sqlcipher_module_path)
                readable.append((relative, Path(plain)))
            except ExportError as error:
                _check(cancel)
                if _message_file(path, fmt):
                    if not descriptor.get("legacy_database_present"):
                        raise
                    failed_databases.append({"database": relative, "format": fmt, "error": str(error)})
                    warning = f"消息库未导入：{relative}；{error}；本次只能部分导入，仍需重试此数据库。"
                else:
                    warning = f"联系人/辅助库未读取：{relative}；{error}"
                warnings.append(warning)
                progress(warning)
            stage.update(completed=index)
    if not any(_message_file(Path(relative), descriptor["format"]) for relative, _ in readable):
        details = "；".join(item["error"] for item in failed_databases)
        raise ExportError("没有可读取的 PC 消息数据库。" + details)
    descriptor["failed_databases"] = failed_databases
    return descriptor, readable, warnings


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db, table):
    return {r[1] for r in db.execute("PRAGMA table_info(" + _quoted(table) + ")")}


def _open(path):
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _value(row, *names, default=None):
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def _text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _integer(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


def _timestamp(value):
    number = _integer(value)
    if not number:
        return 0
    if abs(number) < 100_000_000_000:
        return number * 1000
    return number


def _content(value, compression=0):
    if isinstance(value, bytes) and (compression == 4 or value.startswith(b"\x28\xb5\x2f\xfd")):
        try:
            import zstandard
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(value)) as stream:
                value = stream.read(32 * 1024 * 1024 + 1)
            if len(value) > 32 * 1024 * 1024:
                return "[超大压缩正文：原始数据已保留]"
        except zstandard.ZstdError:
            return "[正文解压失败：原始数据已保留]"
    return _text(value)


def _wechat_label(raw_type, text):
    base = raw_type & 0xFFFFFFFF
    if base != 49:
        return TYPE_LABELS.get(base, "其他")
    subtype = raw_type >> 32
    if not subtype and text.lstrip().startswith("<") and len(text) <= 8 * 1024 * 1024:
        try:
            root = ET.fromstring(text)
            subtype = _integer(root.findtext(".//appmsg/type") or root.findtext("./type"))
        except ET.ParseError:
            pass
    return APP_LABELS.get(subtype, "应用消息")


def _display(text, typ, label=None):
    if typ == 1:
        return text
    if text.lstrip().startswith("<") and len(text) <= 8 * 1024 * 1024:
        try:
            root = ET.fromstring(text)
            title = root.findtext(".//title") or root.findtext(".//des") or ""
            return "[" + (label or TYPE_LABELS.get(typ, "其他")) + (": " + title if title else "") + "]"
        except ET.ParseError:
            pass
    return text or "[" + (label or TYPE_LABELS.get(typ, "其他")) + "]"


def _contacts(readable, fmt, cancel=None):
    names, identifiers = {}, {}
    for relative, path in readable:
        _check(cancel)
        with closing(_open(path)) as db:
            tables = _tables(db)
            if fmt.startswith("wechat"):
                for table in tables:
                    if table.lower() not in {"contact", "rcontact", "name2id"}:
                        continue
                    if table.lower() == "name2id":
                        if "user_name" in _columns(db, table):
                            for r in db.execute("SELECT user_name FROM " + _quoted(table)):
                                names.setdefault(_text(r[0]), _text(r[0]))
                        continue
                    for raw in db.execute("SELECT * FROM " + _quoted(table)):
                        row = dict(raw)
                        user = _text(_value(row, "username", "UserName", "user_name"))
                        if user:
                            names[user] = _text(_value(row, "remark", "Remark", "conRemark") or _value(row, "nick_name", "NickName", "nickname") or user)
            elif fmt == "qqnt":
                for table in ("profile_info_v6", "profile_info_v7"):
                    if table in tables and "1000" in _columns(db, table):
                        for raw in db.execute("SELECT * FROM " + _quoted(table)):
                            row = dict(raw)
                            user = _text(row.get("1000"))
                            qq = _text(row.get("1002"))
                            label = _text(row.get("20009") or row.get("20002") or qq or user)
                            if user:
                                names[user] = label
                            if qq:
                                names[qq] = label
                                identifiers[user] = qq
                if "nt_uid_mapping_table" in tables and {"48902", "1002"} <= _columns(db, "nt_uid_mapping_table"):
                    for row in db.execute('SELECT "48902","1002" FROM nt_uid_mapping_table'):
                        identifiers[_text(row[0])] = _text(row[1])
                if "group_list" in tables and {"60001", "60007"} <= _columns(db, "group_list"):
                    for row in db.execute('SELECT "60001","60007" FROM group_list'):
                        names[_text(row[0])] = _text(row[1])
    return names, identifiers


def _message_tables(db, fmt):
    tables = _tables(db)
    result = []
    if fmt == "wechat4":
        for table in sorted(tables):
            if not re.fullmatch(r"Msg_[a-fA-F0-9]{32}", table, re.I):
                continue
            if not {"local_id", "local_type", "create_time", "message_content", "real_sender_id"} <= _columns(db, table):
                raise ExportError("PC 微信 4 消息表结构不受支持：" + table)
            result.append(table)
    elif fmt == "wechat3":
        for table in tables:
            if table.lower() == "msg":
                if not {"localId", "IsSender", "StrTalker", "CreateTime", "StrContent", "Type"} <= _columns(db, table):
                    raise ExportError("PC 微信 3 MSG 表结构不受支持。")
                result.append(table)
    elif fmt == "qqnt":
        for table in QQ_TABLES:
            if table in tables:
                if not {"40021", "40050", "40800"} <= _columns(db, table):
                    raise ExportError("QQ NT 消息表结构不受支持：" + table)
                result.append(table)
    elif fmt == "qq_legacy":
        from .qq_legacy import message_tables
        return message_tables(db)
    return result


def _varint(data, offset):
    number = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("truncated protobuf")
        byte = data[offset]
        offset += 1
        number |= (byte & 127) << shift
        if byte < 128:
            return number, offset
    raise ValueError("protobuf integer overflow")


def protobuf_fields(data):
    """Strict bounded wire reader; malformed payload remains available in raw JSON."""
    if not isinstance(data, bytes) or len(data) > 32 * 1024 * 1024:
        return []
    result = []
    offset = 0
    try:
        while offset < len(data):
            tag, offset = _varint(data, offset)
            field, wire = tag >> 3, tag & 7
            if not field or len(result) > 100000:
                return []
            if wire == 0:
                value, offset = _varint(data, offset)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
                if offset + size > len(data):
                    return []
                value = data[offset:offset + size]
                offset += size
            elif wire == 2:
                size, offset = _varint(data, offset)
                if offset + size > len(data):
                    return []
                value = data[offset:offset + size]
                offset += size
            else:
                return []
            result.append((field, wire, value))
    except ValueError:
        return []
    return result


def _field_values(data, wanted, depth=0):
    if depth > 7:
        return []
    result = []
    for field, wire, value in protobuf_fields(data):
        # A quote holds the original message, not this reply's text or media.
        # The untouched body remains available in _desktop_raw.
        if field == 47423:
            continue
        if field == wanted:
            result.append(value)
        if wire == 2 and depth < 7 and isinstance(value, bytes):
            result.extend(_field_values(value, wanted, depth + 1))
    return result


def decode_qq_body(blob):
    if isinstance(blob, str):
        return blob, "文本", []
    root = protobuf_fields(blob)
    elements = [v for f, w, v in root if f == 40800 and w == 2]
    if not elements and any(f == 45002 for f, _, _ in root):
        elements = [blob]
    texts, types, paths = [], [], []
    for element in elements:
        kind = next((v for v in _field_values(element, 45002) if isinstance(v, int)), 0)
        if kind == 1:
            texts.extend(_text(v) for v in _field_values(element, 45101) if isinstance(v, bytes))
            types.append("文本")
        elif kind in {2, 3, 4, 5, 6}:
            label = {2: "图片", 3: "文件", 4: "语音", 5: "视频", 6: "表情"}[kind]
            names = [_text(v) for v in _field_values(element, 45402 if kind != 6 else 47602) if isinstance(v, bytes)]
            face = next((v for v in _field_values(element, 47601) if isinstance(v, int)), None) if kind == 6 else None
            texts.append("[QQ 表情 " + str(face) + "]" if face is not None and not names
                         else "[" + label + (": " + names[0] if names else "") + "]")
            types.append(label)
            if kind != 6:
                paths.extend(names)
        elif kind in {10, 11}:
            cards = [_text(v) for v in _field_values(element, 47901) if isinstance(v, bytes)]
            texts.extend(cards or ["[卡片]"])
            types.append("卡片")
        else:
            visible = []
            for field in (45101, 48153, 48157, 48214, 48271):
                visible.extend(_text(v) for v in _field_values(element, field) if isinstance(v, bytes))
            texts.extend(visible or ["[未识别消息元素]"])
            types.append("其他")
    return "".join(texts) or "[未识别消息：原始数据已保留]", (types[0] if len(set(types)) == 1 else "混合消息") if types else "其他", paths


def _base_record(descriptor, relative, table, row, names, conv_key, talker, sender, timestamp, typ, label, text, server_id, local_id, is_group, direction_known=True):
    uid = descriptor["uid"]
    server = _text(server_id)
    if server not in {"", "0", "-1"}:
        identity = "server:" + conv_key + ":" + server
    elif local_id not in (None, ""):
        # Local sequences may be reused by another conversation/client import.
        # Do not collapse distinct records merely because their sequence matches.
        # Unknown or compressed bodies can share the same display placeholder.
        # Their bytes distinguish messages while delivery/status changes do not.
        body = {field: _raw(row[field]) for field in
                ("40800", "message_content", "WCDB_CT_message_content", "StrContent", "CompressContent")
                if field in row}
        body_digest = hashlib.sha256(_json(body).encode("utf-8")).hexdigest()
        local_context = [conv_key, _text(local_id), timestamp, typ, sender, text, body_digest]
        fingerprint = hashlib.sha256(_json(local_context).encode("utf-8")).hexdigest()[:24]
        identity = "local:" + relative + ":" + table + ":" + fingerprint
    else:
        identity = "fallback:" + hashlib.sha256(_json({k: _raw(v) for k, v in row.items()}).encode()).hexdigest()
    key = descriptor["app"] + ":" + identity
    conversation = conv_key + "@" + uid
    own = sender == descriptor["account"]
    if own:
        sender = uid
    try:
        shown_time = datetime.fromtimestamp(timestamp / 1000, ZoneInfo(descriptor["timezone"])).strftime("%Y-%m-%d %H:%M:%S") if timestamp else ""
    except (OverflowError, OSError, ValueError):
        shown_time = ""
    name = names.get(talker, talker)
    return {"msg_uuid": key, "conversation_id": conversation, "sender": sender,
            "created_time": timestamp, "type": typ, "msg_server_id": server,
            "_desktop_raw": {"database": relative, "table": table, "row": {k: _raw(v) for k, v in row.items()}},
            "_export": {"app": descriptor["app"], "source_key": key, "source_conversation": conv_key,
                        "account_uid": uid, "account_identifier": descriptor["account"], "time": shown_time,
                        "text": text, "type_name": label, "conversation_name": name, "conversation_id": conversation,
                        "sender_name": "我" if own else names.get(sender, sender), "is_group": is_group,
                        "direction_known": direction_known, "local_media": [],
                        "media_status": "missing" if label in {"图片", "语音", "视频", "表情", "文件"} else "none"}}


def _wechat_maps(db, hash_names):
    name_ids = {}
    if "Name2Id" in _tables(db) and "user_name" in _columns(db, "Name2Id"):
        name_ids = {r[0]: _text(r[1]) for r in db.execute("SELECT rowid,user_name FROM Name2Id")}
        hash_names.update({hashlib.md5(k.encode("utf-8")).hexdigest(): k for k in name_ids.values()})
    return name_ids, hash_names


def _records(db, descriptor, relative, table, names, identifiers, wechat_maps=None, *, cancel=None):
    fmt = descriptor["format"]
    if fmt == "qq_legacy":
        from .qq_legacy import records
        yield from records(db, descriptor, relative, table, names, cancel=cancel)
        return
    if fmt == "wechat4":
        name_ids, hash_names = wechat_maps if wechat_maps is not None else _wechat_maps(db, {hashlib.md5(k.encode("utf-8")).hexdigest(): k for k in names})
    for raw in db.execute("SELECT * FROM " + _quoted(table)):
        _check(cancel)
        row = dict(raw)
        if fmt == "wechat4":
            digest = table[4:].lower()
            talker = hash_names.get(digest, "会话 " + digest)
            conv_key = "wx:" + digest
            sender = name_ids.get(row.get("real_sender_id"), "未知发送者:" + _text(row.get("real_sender_id")))
            raw_type = _integer(row.get("local_type"))
            typ = raw_type & 0xFFFFFFFF
            text = _content(row.get("message_content"), row.get("WCDB_CT_message_content"))
            group = talker.endswith("@chatroom")
            if group and ":\n" in text:
                prefix, rest = text.split(":\n", 1)
                if prefix == sender:
                    text = rest
            label = _wechat_label(raw_type, text)
            record = _base_record(descriptor, relative, table, row, names, conv_key, talker, sender,
                                  _timestamp(row.get("create_time")), typ, label, _display(text, typ, label),
                                  row.get("server_id"), row.get("local_id"), group, row.get("real_sender_id") in name_ids)
        elif fmt == "wechat3":
            talker = _text(row.get("StrTalker"))
            group = talker.endswith("@chatroom")
            own = _integer(row.get("IsSender")) == 1
            sender = descriptor["account"] if own else talker
            text = _content(row.get("StrContent"))
            if group and not own and ":\n" in text:
                sender, text = text.split(":\n", 1)
            typ = _integer(row.get("Type")) & 0xFFFFFFFF
            if not text and row.get("CompressContent"):
                # Some WeChat 3 application payloads use LZ4; preserve bytes if codec is absent.
                text = "[压缩正文：原始数据已保留]"
            label = _wechat_label(_integer(row.get("Type")), text)
            record = _base_record(descriptor, relative, table, row, names, "wx:" + hashlib.md5(talker.encode()).hexdigest(), talker, sender,
                                  _timestamp(row.get("CreateTime")), typ, label, _display(text, typ, label),
                                  row.get("MsgSvrID"), row.get("localId"), group)
        else:
            scope, group = QQ_TABLES[table]
            talker = _text(row.get("40021") or row.get("40027") or row.get("40030"))
            numeric_sender = _text(row.get("40033"))
            sender_uid = _text(row.get("40020"))
            numeric_known = numeric_sender.isdecimal() and int(numeric_sender) > 0
            mapped_sender = identifiers.get(sender_uid) or identifiers.get(numeric_sender)
            mapped_known = bool(mapped_sender and mapped_sender.isdecimal() and int(mapped_sender) > 0)
            sender = numeric_sender if numeric_known else (mapped_sender if mapped_known else sender_uid or numeric_sender)
            direction_known = numeric_known or mapped_known
            # 40013 is a delivery/status field. Real databases contain both
            # incoming flag=1/2 and outgoing flag=0 rows; it is not direction.
            own = direction_known and sender == descriptor["account"]
            if own:
                sender = descriptor["account"]
            elif sender in {"", "0", "-1"}:
                sender = ("未知群成员" if group else identifiers.get(talker, talker))
            text, label, _paths = decode_qq_body(row.get("40800"))
            record = _base_record(descriptor, relative, table, row, names, scope + ":" + talker,
                                  talker, sender, _timestamp(row.get("40050")), _integer(row.get("40011")), label, text,
                                  row.get("40001"), row.get("40002") or row.get("40003"), group, direction_known)
            nick = _text(row.get("40090") or row.get("40093"))
            if nick and not own:
                record["_export"]["sender_name"] = nick
            element_kinds = {kind for kind in _field_values(row.get("40800"), 45002) if isinstance(kind, int)}
            if element_kinds & {2, 3, 4, 5}:
                record["_export"]["media_status"] = "missing"
            elif 6 in element_kinds:
                from .desktop_media import extract_qqnt_media
                record["_export"]["media_status"] = "missing" if extract_qqnt_media(row.get("40800")) else "none"
        yield record


def _inventory(descriptor, readable, cancel=None):
    tables = []
    for relative, path in readable:
        _check(cancel)
        fmt = _database_format(relative, descriptor["format"])
        if not _message_file(Path(relative), fmt):
            continue
        with closing(_open(path)) as db:
            selected = _message_tables(db, fmt)
            if not selected:
                # WeChat creates empty shards containing just its name index.
                # Accept these only alongside an independently validated message
                # table elsewhere; an unknown nonempty schema still fails.
                present = _tables(db)
                metadata_only = (fmt == "wechat4" and "Name2Id" in present
                                 and "user_name" in _columns(db, "Name2Id")
                                 and all(t in {"Name2Id", "TimeStamp", "MessageGroupTimeInfo", "DeleteInfo"}
                                         or t.lower().startswith(("sqlite_", "wcdb_")) for t in present))
                if metadata_only:
                    continue
                raise ExportError("PC 消息库未找到受支持的消息表：" + relative)
            for table in selected:
                count = db.execute("SELECT COUNT(*) FROM " + _quoted(table)).fetchone()[0]
                tables.append((relative, path, table, count))
    if not tables:
        raise ExportError("未找到可解析的 PC 消息表。")
    return tables


def _read_metadata(descriptor, readable, progress, cancel):
    with StageReporter(progress, "统计 PC 聊天消息数量", cancel=cancel):
        inventory = _inventory(descriptor, readable, cancel)
    with StageReporter(progress, "读取 PC 联系人与会话名称", cancel=cancel):
        names, identifiers = _contacts(readable, descriptor["format"], cancel)
    return inventory, names, identifiers


def inspect_backup(source, work, *, progress=print, cancel=None, keys=None, sqlcipher_module_path=None):
    descriptor, readable, warnings = _prepare(source, work, keys=keys, progress=progress, cancel=cancel, sqlcipher_module_path=sqlcipher_module_path)
    inventory, names, _ = _read_metadata(descriptor, readable, progress, cancel)
    count = sum(item[3] for item in inventory)
    return {"accounts": [{"uid": descriptor["uid"], "account": descriptor["account"], "nickname": names.get(descriptor["account"], descriptor["account"]), "message_count": count}],
            "status": "partial" if descriptor.get("failed_databases") else "inspected", "format": descriptor["format"],
            "failed_databases": descriptor.get("failed_databases", []), "warnings": warnings}


def export_backup(source, output, work, *, timezone="Asia/Shanghai", progress=print, cancel=None, keys=None, sqlcipher_module_path=None, media_output=None):
    output = Path(output).expanduser().resolve()
    work = Path(work).expanduser().resolve()
    original = Path(_descriptor(source)["source"])
    _separate(original, output)
    _separate(original, work)
    _separate(output, work)
    if media_output:
        _separate(original, Path(media_output).resolve())
    ZoneInfo(timezone)
    descriptor, readable, warnings = _prepare(source, work, keys=keys, progress=progress, cancel=cancel, sqlcipher_module_path=sqlcipher_module_path)
    descriptor["timezone"] = timezone
    inventory, names, identifiers = _read_metadata(descriptor, readable, progress, cancel)
    hash_names = {hashlib.md5(k.encode("utf-8")).hexdigest(): k for k in names} if descriptor["format"] == "wechat4" else {}
    uid = descriptor["uid"]
    account_dir = output / uid
    account_dir.mkdir(parents=True, exist_ok=True)
    total = sum(item[3] for item in inventory)
    seen, count, processed, missing = set(), 0, 0, 0
    kinds = Counter()
    recovered_media, thumbnail_media = 0, 0
    media_context = nullcontext(None)
    if descriptor["app"] == "qq_pc":
        from .desktop_media import DesktopMediaIndex
        media_context = DesktopMediaIndex(original, media_output or output, work, progress=progress, cancel=cancel)
    with media_context as media_index, StageReporter(progress, "解析 PC 聊天记录 " + descriptor["account"], completed=0, total=total, unit="条", cancel=cancel) as stage:
        with (account_dir / "messages.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
            for (relative, path), database_tables in groupby(inventory, key=lambda item: (item[0], item[1])):
                with closing(_open(path)) as db:
                    db_descriptor = {**descriptor, "format": _database_format(relative, descriptor["format"])}
                    context = _wechat_maps(db, hash_names) if db_descriptor["format"] == "wechat4" else None
                    for _, _, table, _ in database_tables:
                        stage.update(detail=relative + " · " + table)
                        for record in _records(db, db_descriptor, relative, table, names, identifiers, context, cancel=cancel):
                            _check(cancel)
                            processed += 1
                            stage.update(completed=processed)
                            identity = record["msg_uuid"]
                            if identity in seen:
                                continue
                            seen.add(identity)
                            if media_index is not None:
                                media_index.attach(record)
                            assets = record["_export"].get("desktop_media", [])
                            recovered_media += len(record["_export"].get("original_media", []))
                            thumbnail_media += sum(item.get("variant") == "thumbnail" for item in assets)
                            stream.write(_json(record) + "\n")
                            count += 1
                            missing += record["_export"]["media_status"] in {"missing", "partial", "thumbnail"}
                            kinds[record["_export"]["type_name"]] += 1
    stats = {"app": descriptor["app"], "account_uid": uid, "account_identifier": descriptor["account"],
             "nickname": names.get(descriptor["account"], descriptor["account"]), "message_count": count,
             "source_record_count": total, "media_missing_messages": missing, "types": dict(kinds), "format": descriptor["format"],
             "media_original_references": recovered_media, "media_thumbnail_references": thumbnail_media}
    (account_dir / "statistics.json").write_text(_json(stats), encoding="utf-8")
    (account_dir / "contacts.json").write_text(_json([{"user_id": key, "nickname": value} for key, value in names.items()]), encoding="utf-8")
    if missing:
        warnings.append(f"仍有 {missing:,} 条消息未找到全部原媒体；找到的缩略图已单独标记，原始字段保留。")
    incomplete = bool(descriptor.get("failed_databases"))
    summary = {"app": descriptor["app"], "status": "partial" if incomplete else "completed", "accounts": [stats],
               "failed_databases": descriptor.get("failed_databases", []), "warnings": warnings, "message_count": count}
    (output / "export_summary.json").write_text(_json(summary), encoding="utf-8")
    progress(f"PC 账号{'部分导入' if incomplete else '解析完成'}：{count:,} 条；原媒体 {recovered_media:,} 个，缩略图 {thumbnail_media:,} 个，仍缺原媒体 {missing:,} 条。")
    return summary
