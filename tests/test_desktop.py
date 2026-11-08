"""Desktop adapters exercised against independently constructed, retained fixtures."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from duoshan_export import desktop_sources as discovery
from duoshan_export.apps import desktop
from duoshan_export.errors import ExportError
from duoshan_export.progress import ProgressEvent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def varint(n):
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128)
        n >>= 7
    result.append(n)
    return bytes(result)


def field(number, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return varint((number << 3) | 2) + varint(len(value)) + value
    return varint(number << 3) + varint(value)


def qqtext(text):
    return field(40800, field(45002, 1) + field(45101, text))


class DesktopTests(unittest.TestCase):
    def setUp(self):
        # No cleanup: the workspace explicitly requires retained test artifacts.
        self.root = Path(tempfile.mkdtemp(prefix="desktop-parser-"))
        self.sequence = 0

    def database(self, path, script):
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        db.executescript(script)
        db.commit()
        db.close()
        return path

    def wx4(self, account="wxid_example_1a2b", rows=None, wal=False):
        root = self.root / "Documents" / "xwechat_files" / account
        db_path = root / "db_storage/message/message_0.db"
        table = "Msg_" + hashlib.md5(b"wxid_friend").hexdigest()
        self.database(db_path, f'''
            CREATE TABLE Name2Id(user_name TEXT);
            INSERT INTO Name2Id VALUES ('wxid_example'), ('wxid_friend');
            CREATE TABLE {table}(local_id INTEGER,server_id INTEGER,local_type INTEGER,
                create_time INTEGER,real_sender_id INTEGER,message_content BLOB,
                WCDB_CT_message_content INTEGER,extra BLOB);
        ''')
        db = sqlite3.connect(db_path)
        if wal:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
        db.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?)", rows or [
            (1, 9007199254741001, 1, 1700000000, 1, "我发的😀", 0, b"\x00\xff"),
            (2, 9007199254741002, 1, 1700000001, 2, "对方回复", 0, None),
        ])
        db.commit()
        self.database(root / "db_storage/contact/contact.db", '''
            CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT);
            INSERT INTO contact VALUES ('wxid_friend','昵称','朋友备注');
            INSERT INTO contact VALUES ('wxid_example','本人昵称','');
        ''')
        if wal:
            self.addCleanup(db.close)
        else:
            db.close()
        return root

    def export(self, source):
        self.sequence += 1
        output, work = self.root / f"export{self.sequence}", self.root / f"work{self.sequence}"
        logs = []
        result = desktop.export_backup(source, output, work, progress=logs.append)
        uid = result["accounts"][0]["account_uid"]
        rows = [json.loads(line) for line in (output / uid / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
        return result, rows, logs

    def test_redirected_documents_and_nested_selection(self):
        source = self.wx4()
        second = self.wx4("wxid_second_f00d")
        ignored = self.root / "Documents/xwechat_files/all_users/db_storage/message"
        ignored.mkdir(parents=True)
        received = source / "msg/file/received.db"
        received.parent.mkdir(parents=True)
        received.write_bytes(b"not a chat database")
        with patch.object(discovery, "documents_directory", return_value=self.root / "Documents"):
            found = discovery.discover_desktop_sources()
        self.assertEqual(len(found), 2)
        selected = discovery.discover_desktop_sources([source / "db_storage/message/message_0.db"])
        self.assertEqual([item["source"] for item in selected], [str(source.resolve())])
        self.assertEqual(selected[0]["account"], "wxid_example")
        self.assertNotIn(received, discovery.database_files(source))
        self.assertNotEqual(found[0]["uid"], found[1]["uid"])
        self.assertNotEqual(source, second)

    def test_wechat4_export_preserves_raw_ids_and_both_directions(self):
        source = self.wx4()
        before = {p: digest(p) for p in discovery.database_files(source)}
        result, rows, logs = self.export(source)
        self.assertEqual(result["message_count"], 2)
        own, incoming = rows
        self.assertEqual(own["sender"], result["accounts"][0]["account_uid"])
        self.assertEqual(incoming["sender"], "wxid_friend")
        self.assertEqual(incoming["_export"]["conversation_name"], "朋友备注")
        self.assertEqual(own["_export"]["text"], "我发的😀")
        self.assertEqual(own["_desktop_raw"]["row"]["server_id"], 9007199254741001)
        self.assertEqual(own["_desktop_raw"]["row"]["extra"], {"encoding": "base64", "data": "AP8="})
        self.assertEqual(before, {p: digest(p) for p in before})
        events = [line for line in logs if isinstance(line, ProgressEvent)]
        self.assertTrue(any(e.total == 2 and e.completed == 2 and e.state == "completed" for e in events))
        self.assertTrue(any(e.unit == "字节" and e.completed == e.total for e in events))

    def test_wechat4_committed_wal_rows_are_included_without_source_changes(self):
        source = self.wx4(wal=True)
        before = {p: digest(p) for p in discovery.database_files(source)}
        self.assertTrue(any(p.name.endswith("-wal") for p in before))
        result, rows, _ = self.export(source)
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(before, {p: digest(p) for p in before})
        self.assertEqual(rows[1]["_export"]["text"], "对方回复")

    def test_wechat4_zstd_and_unknown_sender_preserve_content(self):
        import zstandard
        compressed = zstandard.ZstdCompressor().compress("压缩消息".encode())
        source = self.wx4(rows=[(1, 100, 1, 1700000000, 1, compressed, 4, None),
                                (2, 101, 777, 1700000001, 99, b"opaque", 0, b"raw")])
        _, rows, _ = self.export(source)
        self.assertEqual(rows[0]["_export"]["text"], "压缩消息")
        self.assertFalse(rows[1]["_export"]["direction_known"])
        self.assertEqual(rows[1]["_desktop_raw"]["row"]["extra"]["data"], "cmF3")

    def test_empty_wechat_auxiliary_shard_does_not_hide_unknown_schemas(self):
        source = self.wx4()
        self.database(source / "db_storage/message/biz_message_0.db", "CREATE TABLE Name2Id(user_name TEXT); CREATE TABLE TimeStamp(timestamp INTEGER); CREATE TABLE wcdb_builtin_compression_record(tableName TEXT);")
        result, _, _ = self.export(source)
        self.assertEqual(result["message_count"], 2)
        self.database(source / "db_storage/message/message_1.db", "CREATE TABLE SomeFutureMessage(payload BLOB);")
        with self.assertRaisesRegex(ExportError, "消息表"):
            self.export(source)

    def test_wechat_application_subtypes_are_not_all_missing_files(self):
        source = self.wx4(rows=[
            (1, 100, (57 << 32) | 49, 1700000000, 1, "<msg><appmsg><title>回复内容</title></appmsg></msg>", 0, None),
            (2, 101, 49, 1700000001, 2, "<msg><appmsg><type>6</type><title>example.pdf</title></appmsg></msg>", 0, None),
        ])
        result, rows, _ = self.export(source)
        self.assertEqual(rows[0]["_export"]["type_name"], "引用回复")
        self.assertEqual(rows[0]["_export"]["media_status"], "none")
        self.assertEqual(rows[1]["_export"]["type_name"], "文件")
        self.assertEqual(rows[1]["_export"]["media_status"], "missing")
        self.assertEqual(result["accounts"][0]["media_missing_messages"], 1)

    def test_wechat4_multiple_conversations_and_group_prefix(self):
        source = self.wx4()
        group = "fixture@chatroom"
        table = "Msg_" + hashlib.md5(group.encode()).hexdigest()
        path = source / "db_storage/message/message_0.db"
        db = sqlite3.connect(path)
        db.execute("INSERT INTO Name2Id VALUES (?)", (group,))
        db.execute(f"CREATE TABLE {table}(local_id INTEGER, server_id INTEGER, local_type INTEGER, create_time INTEGER, real_sender_id INTEGER, message_content TEXT)")
        db.execute(f"INSERT INTO {table} VALUES(1,999,1,1700000002,2,?)", ("wxid_friend:\n群内回复",))
        db.commit()
        db.close()
        result, rows, _ = self.export(source)
        self.assertEqual(result["message_count"], 3)
        message = next(r for r in rows if r["_export"]["is_group"])
        self.assertEqual(message["_export"]["text"], "群内回复")
        self.assertEqual(message["sender"], "wxid_friend")
        self.assertEqual(len({r["conversation_id"] for r in rows}), 2)

    def test_explicit_keys_do_not_scan_client_memory(self):
        source = self.wx4()
        descriptor, snapshot = desktop.prepare_backup(source, self.root / "prepared", progress=lambda _: None)
        descriptor["encrypted"] = True
        with patch.object(desktop, "prepare_backup", return_value=(descriptor, snapshot)), \
             patch.object(desktop, "recover_wechat_keys", side_effect=AssertionError("unexpected memory scan")):
            result = desktop.inspect_backup(source, self.root / "keyed", keys={"default": "user-key"}, progress=lambda _: None)
        self.assertEqual(result["accounts"][0]["message_count"], 2)

    def test_wechat3_missing_key_does_not_scan_wechat4_processes(self):
        source = self.root / "wx3_no_key/example"
        database = source / "Msg/Multi/MSG0.db"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"encrypted synthetic database")
        with patch.object(desktop, "recover_wechat_keys", side_effect=AssertionError("unexpected WeChat4 memory scan")):
            with self.assertRaisesRegex(ExportError, "密钥"):
                desktop.inspect_backup(source, self.root / "wx3-key-work", progress=lambda _: None)

    def test_mixed_qqnt_and_legacy_records_are_explicitly_disclosed(self):
        source = self.root / "mixed-qq/12345678"
        self.database(source / "nt_qq/nt_db/nt_msg.db", '''
            CREATE TABLE c2c_msg_table("40001" INTEGER,"40011" INTEGER,"40013" INTEGER,
              "40021" TEXT,"40033" TEXT,"40050" INTEGER,"40800" TEXT);
            INSERT INTO c2c_msg_table VALUES(1,2,1,'peer','12345678',1700000000,'NT text');
        ''')
        legacy = source / "Msg3.0.db"
        legacy.write_bytes(b"unhandled legacy messages")
        before = digest(legacy)
        found = discovery.discover_desktop_sources([source])[0]
        self.assertTrue(found["legacy_database_present"])
        self.assertIn("分别检查并导入", found["status"])
        self.assertIn(legacy, discovery.database_files(source))
        result, rows, logs = self.export(source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_databases"][0]["database"], "Msg3.0.db")
        self.assertTrue(any("只能部分导入" in value for value in result["warnings"]))
        self.assertTrue(any("只能部分导入" in value for value in logs))
        self.assertEqual(before, digest(legacy))

    def test_required_message_decryption_failure_is_not_silently_ignored(self):
        source = self.wx4()
        real_decrypt = desktop.decrypt_snapshot
        def fail_message(path, *args, **kwargs):
            if path.name == "message_0.db":
                raise ExportError("message page validation failed")
            return real_decrypt(path, *args, **kwargs)
        with patch.object(desktop, "decrypt_snapshot", side_effect=fail_message):
            with self.assertRaisesRegex(ExportError, "message page validation"):
                self.export(source)

    def test_stable_server_identity_across_local_shard_changes(self):
        source = self.wx4()
        _, first, _ = self.export(source)
        db = source / "db_storage/message/message_0.db"
        # This is a synthetic source mutation representing a later client backup.
        with sqlite3.connect(db) as conn:
            table = "Msg_" + hashlib.md5(b"wxid_friend").hexdigest()
            conn.execute(f"UPDATE {table} SET local_id=local_id+100")
        conn.close()
        _, second, _ = self.export(source)
        self.assertEqual([r["msg_uuid"] for r in first], [r["msg_uuid"] for r in second])

    def test_wechat3_outgoing_group_and_original_fields(self):
        root = self.root / "old/wechat_me"
        path = self.database(root / "Msg/Multi/MSG0.db", '''
            CREATE TABLE MSG(localId INTEGER, IsSender INTEGER, StrTalker TEXT,
                CreateTime INTEGER, StrContent TEXT, Type INTEGER, MsgSvrID INTEGER);
            INSERT INTO MSG VALUES(1,1,'friend',1700000000,'outgoing',1,101);
            INSERT INTO MSG VALUES(2,0,'group@chatroom',1700000001,'member:
group message',1,102);
        ''')
        before = digest(path)
        _, rows, _ = self.export(root)
        self.assertEqual(rows[0]["sender"], rows[0]["_export"]["account_uid"])
        self.assertEqual(rows[1]["sender"], "member")
        self.assertEqual(rows[1]["_export"]["text"], "group message")
        self.assertTrue(rows[1]["_export"]["is_group"])
        self.assertEqual(digest(path), before)

    def test_qqnt_numeric_columns_protobuf_contacts_and_group(self):
        source = self.root / "Tencent Files/123456789"
        path = self.database(source / "nt_qq/nt_db/nt_msg.db", '''
            CREATE TABLE c2c_msg_table("40001" INTEGER,"40011" INTEGER,"40013" INTEGER,
              "40021" TEXT,"40033" TEXT,"40050" INTEGER,"40800" BLOB);
            CREATE TABLE group_msg_table("40001" INTEGER,"40011" INTEGER,"40013" INTEGER,
              "40021" TEXT,"40033" TEXT,"40050" INTEGER,"40800" BLOB,"40090" TEXT);
        ''')
        selected = discovery.discover_desktop_sources([path])[0]
        self.assertEqual(selected["source"], str(source.resolve()))
        self.assertEqual(selected["account"], "123456789")
        db = sqlite3.connect(path)
        db.executemany('INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?,?)', [
            (1001, 2, 1, "u_friend", "123456789", 1700000000, qqtext("我发送")),
            (1002, 2, 0, "u_friend", "u_friend", 1700000001, qqtext("回复")),
            (1003, 999, 0, "u_friend", "u_friend", 1700000002, b"\xff"),
        ])
        db.execute('INSERT INTO group_msg_table VALUES (?,?,?,?,?,?,?,?)',
                   (1004, 2, 0, "987654321", "u_friend", 1700000003, qqtext("群内容"), "群名片"))
        db.commit()
        db.close()
        self.database(source / "nt_qq/nt_db/profile_info.db", '''
            CREATE TABLE profile_info_v6("1000" TEXT,"1002" TEXT,"20002" TEXT,"20009" TEXT);
            INSERT INTO profile_info_v6 VALUES ('u_friend','111222333','昵称','QQ备注');
        ''')
        self.database(source / "nt_qq/nt_db/group_info.db", '''
            CREATE TABLE group_list("60001" TEXT,"60007" TEXT);
            INSERT INTO group_list VALUES ('987654321','群名');
        ''')
        result, rows, _ = self.export(source)
        self.assertEqual(result["message_count"], 4)
        self.assertEqual(rows[0]["sender"], rows[0]["_export"]["account_uid"])
        self.assertEqual(rows[1]["sender"], "111222333")
        self.assertEqual(rows[1]["_export"]["conversation_name"], "QQ备注")
        self.assertEqual(rows[1]["_export"]["text"], "回复")
        self.assertIn("原始数据已保留", rows[2]["_export"]["text"])
        self.assertEqual(rows[2]["_desktop_raw"]["row"]["40800"]["data"], "/w==")
        self.assertEqual(rows[3]["_export"]["sender_name"], "群名片")
        self.assertEqual(rows[3]["_export"]["conversation_name"], "群名")

    def test_unknown_schema_and_legacy_qq_are_explicit_errors(self):
        bad = self.root / "unknown/db_storage/message/message_0.db"
        self.database(bad, "CREATE TABLE UnknownMessage(value TEXT);")
        with self.assertRaisesRegex(ExportError, "消息表"):
            self.export(bad.parents[2])
        legacy = self.root / "legacy/12345678"
        self.database(legacy / "Msg3.0.db", "CREATE TABLE Messages(value BLOB);")
        with self.assertRaisesRegex(ExportError, "消息表"):
            self.export(legacy)

    def test_qq_sender_identity_wins_over_message_status(self):
        source = self.root / "qq-directions/12345678"
        self.database(source / "nt_db/nt_msg.db", '''
            CREATE TABLE c2c_msg_table("40001" INTEGER,"40013" INTEGER,"40021" TEXT,
                "40033" TEXT,"40020" TEXT,"40050" INTEGER,"40800" TEXT);
            INSERT INTO c2c_msg_table VALUES(1,2,'u_peer','987654','u_peer',1700000000,'incoming flag two');
            INSERT INTO c2c_msg_table VALUES(2,0,'u_peer','12345678','u_self',1700000001,'outgoing flag zero');
            INSERT INTO c2c_msg_table VALUES(3,0,'u_peer','0','u_self',1700000002,'UID fallback outgoing');
            INSERT INTO c2c_msg_table VALUES(4,1,'u_peer','0','u_unknown',1700000003,'unknown remains unknown');
        ''')
        self.database(source / "nt_db/profile_info.db", '''
            CREATE TABLE profile_info_v6("1000" TEXT,"1002" TEXT,"20002" TEXT);
            INSERT INTO profile_info_v6 VALUES('u_self','12345678','本人'),('u_peer','987654','朋友');
        ''')
        result, rows, _ = self.export(source)
        uid = result["accounts"][0]["account_uid"]
        self.assertEqual([row["sender"] for row in rows], ["987654", uid, uid, "u_unknown"])
        self.assertEqual([row["_export"]["direction_known"] for row in rows], [True, True, True, False])

    def test_qq_auxiliary_message_tables_keep_independent_conversations_and_ids(self):
        source = self.root / "qq-extra-tables/12345678"
        scopes = {
            "c2c_msg_table": ("peer", False), "group_msg_table": ("group", True),
            "discuss_msg_table": ("discuss", True), "c2c_temp_msg_table": ("temp", False),
            "dataline_msg_table": ("dataline", False), "service_assistant_msg_table": ("service", False),
            "ai_assistant_msg_table": ("ai", False), "group_ai_assistant_msg_table": ("group_ai", True),
        }
        path = self.database(source / "nt_db/nt_msg.db", 'CREATE TABLE kv_tofu_msg_table(key TEXT,value BLOB);')
        with sqlite3.connect(path) as db:
            for table in scopes:
                db.execute(f'CREATE TABLE {table}("40001" INTEGER,"40021" TEXT,"40033" TEXT,"40050" INTEGER,"40800" BLOB)')
                db.execute(f'INSERT INTO {table} VALUES(1,\'same-peer\',\'987654\',1700000000,?)', (qqtext("相同消息号"),))
        db.close()
        before = digest(path)
        result, rows, _ = self.export(source)
        self.assertEqual(result["message_count"], len(scopes))
        self.assertEqual(len({row["msg_uuid"] for row in rows}), len(scopes))
        self.assertEqual(len({row["conversation_id"] for row in rows}), len(scopes))
        for row in rows:
            scope, group = scopes[row["_desktop_raw"]["table"]]
            self.assertEqual(row["_export"]["source_conversation"], scope + ":same-peer")
            self.assertEqual(row["_export"]["is_group"], group)
            self.assertEqual(row["_export"]["text"], "相同消息号")
        self.assertEqual(digest(path), before)

    def test_qq_opaque_bodies_do_not_collapse_when_local_sequence_is_reused(self):
        source = self.root / "qq-opaque-bodies/12345678"
        path = self.database(source / "nt_db/nt_msg.db", '''
            CREATE TABLE c2c_msg_table("40001" INTEGER,"40002" INTEGER,"40021" TEXT,"40033" TEXT,"40050" INTEGER,"40800" BLOB);
        ''')
        with sqlite3.connect(path) as db:
            db.executemany('INSERT INTO c2c_msg_table VALUES(0,1,\'peer\',\'987654\',1700000000,?)', [(b'\xff',), (b'\xfe',)])
        db.close()
        _, rows, _ = self.export(source)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["_export"]["text"], rows[1]["_export"]["text"])
        self.assertNotEqual(rows[0]["msg_uuid"], rows[1]["msg_uuid"])
        self.assertEqual([base64.b64decode(row["_desktop_raw"]["row"]["40800"]["data"]) for row in rows], [b'\xff', b'\xfe'])

    def test_standard_qq_face_does_not_report_a_missing_file(self):
        source = self.root / "qq-face/12345678"
        path = self.database(source / "nt_db/nt_msg.db", '''
            CREATE TABLE c2c_msg_table("40001" INTEGER,"40021" TEXT,"40033" TEXT,"40050" INTEGER,"40800" BLOB);
        ''')
        blob = field(40800, field(45002, 6) + field(47601, 14))
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO c2c_msg_table VALUES(1,\'peer\',\'987654\',1700000000,?)', (blob,))
        db.close()
        result, rows, _ = self.export(source)
        self.assertEqual(rows[0]["_export"]["text"], "[QQ 表情 14]")
        self.assertEqual(rows[0]["_export"]["media_status"], "none")
        self.assertEqual(result["accounts"][0]["media_missing_messages"], 0)

    def test_qq_quote_does_not_replace_reply_text_or_require_quoted_media(self):
        source = self.root / "qq-quote/12345678"
        path = self.database(source / "nt_db/nt_msg.db", '''
            CREATE TABLE c2c_msg_table("40001" INTEGER,"40021" TEXT,"40033" TEXT,"40050" INTEGER,"40800" BLOB);
        ''')
        quoted = qqtext("引用旧文") + field(40800, field(45002, 2) + field(45402, "quoted-only.png"))
        blob = field(40800, field(45002, 1) + field(45101, "当前回复") + field(47423, quoted))
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO c2c_msg_table VALUES(1,\'peer\',\'12345678\',1700000000,?)', (blob,))
        db.close()
        before = digest(path)
        result, rows, _ = self.export(source)
        self.assertEqual(rows[0]["_export"]["text"], "当前回复")
        self.assertEqual(rows[0]["_export"]["type_name"], "文本")
        self.assertEqual(rows[0]["_export"]["media_status"], "none")
        self.assertEqual(rows[0]["_export"]["local_media"], [])
        self.assertEqual(result["accounts"][0]["media_missing_messages"], 0)
        self.assertEqual(base64.b64decode(rows[0]["_desktop_raw"]["row"]["40800"]["data"]), blob)
        self.assertEqual(digest(path), before)

    def test_plain_classic_qq_imports_without_nt_and_keeps_raw_fields(self):
        source = self.root / "classic-only/12345678"
        path = self.database(source / "Msg3.0.db", '''
            CREATE TABLE mr_buddy_987654(Time INTEGER,Rand INTEGER,SenderUin INTEGER,MsgContent TEXT,Extra BLOB);
            INSERT INTO mr_buddy_987654 VALUES(1700000000,7,12345678,'旧版发送',X'00FF');
            INSERT INTO mr_buddy_987654 VALUES(1700000001,8,987654,'旧版接收',NULL);
        ''')
        before = digest(path)
        result, rows, _ = self.export(source)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(rows[0]["sender"], result["accounts"][0]["account_uid"])
        self.assertEqual(rows[1]["sender"], "987654")
        self.assertEqual(rows[0]["_desktop_raw"]["row"]["Extra"]["data"], "AP8=")
        self.assertEqual(digest(path), before)

    def test_qq_explicit_keys_bypass_process_memory_recovery(self):
        source = self.root / "qq-keys/12345678"
        self.database(source / "nt_db/nt_msg.db", '''
            CREATE TABLE c2c_msg_table("40001" INTEGER,"40021" TEXT,"40050" INTEGER,"40800" TEXT);
            INSERT INTO c2c_msg_table VALUES(1,'peer',1700000000,'text');
        ''')
        descriptor, snapshot = desktop.prepare_backup(source, self.root / "qq-prepared", progress=lambda _: None)
        descriptor["encrypted"] = True
        with patch.object(desktop, "prepare_backup", return_value=(descriptor, snapshot)), \
             patch.object(desktop, "recover_qq_keys", side_effect=AssertionError("unexpected QQ memory scan")):
            result = desktop.inspect_backup(source, self.root / "qq-keyed", keys={"default": "user-key"}, progress=lambda _: None)
        self.assertEqual(result["accounts"][0]["message_count"], 1)

    def test_source_overlap_rejected_before_writing(self):
        source = self.wx4()
        invalid = source / "work"
        with self.assertRaisesRegex(ExportError, "原始目录"):
            desktop.prepare_backup(source, invalid, progress=lambda _: None)
        self.assertFalse(invalid.exists())

    def test_protobuf_invalid_wire_and_truncation_are_retained_not_guessed(self):
        for value in (b"\x80", b"\x0f", b"\x0a\x05x", b"\x00", b"\xff" * 12):
            self.assertEqual(desktop.protobuf_fields(value), [])
            self.assertIn("原始数据已保留", desktop.decode_qq_body(value)[0])

    def test_local_identity_does_not_collapse_reused_sequence_or_conversation(self):
        descriptor = {"uid": "internal", "account": "owner", "app": "qq_pc", "timezone": "Asia/Shanghai"}
        def record(conv, text):
            return desktop._base_record(descriptor, "nt_db/nt_msg.db", "c2c_msg_table", {}, {}, conv, conv,
                                        "peer", 1700000000000, 1, "文本", text, 0, 1, False)["msg_uuid"]
        self.assertNotEqual(record("first", "text"), record("second", "text"))
        self.assertNotEqual(record("first", "text"), record("first", "different"))
        self.assertEqual(record("first", "text"), record("first", "text"))

    def test_local_identity_uses_raw_body_without_transient_delivery_state(self):
        for app, column in (("qq_pc", "40800"), ("wechat_pc", "message_content"), ("wechat_pc", "CompressContent")):
            with self.subTest(app=app, column=column):
                descriptor = {"uid": "internal", "account": "owner", "app": app, "timezone": "Asia/Shanghai"}
                def record(body, status):
                    row = {column: body, "delivery_state": status}
                    return desktop._base_record(descriptor, "messages.db", "messages", row, {}, "peer:friend", "friend",
                                                "peer", 1700000000000, 1, "其他", "[原始数据已保留]", 0, 1, False)["msg_uuid"]
                self.assertNotEqual(record(b'\xff', 1), record(b'\xfe', 1))
                self.assertEqual(record(b'\xff', 1), record(b'\xff', 2))


if __name__ == "__main__":
    unittest.main()
