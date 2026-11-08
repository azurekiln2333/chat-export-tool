"""Classic desktop QQ protocol fixtures; no real messages or credentials."""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from duoshan_export.apps import qq_legacy
from duoshan_export.errors import ExportError


def tlv(kind, data):
    return bytes([kind]) + len(data).to_bytes(2, "little") + data


def text(value):
    return tlv(1, tlv(1, value.encode("utf-16-le")))


def body(elements, *, timestamp=1700000000, rand=0x10203040, font="宋体"):
    encoded = font.encode("utf-16-le")
    return b"\x00\x01\x00\x00\x00\x00\x00\x00" + struct.pack("<IIIBBBBH", timestamp, rand, 0x00112233, 12, 0, 134, 0, len(encoded)) + encoded + b"\x00\x00" + elements


class LegacyQQTests(unittest.TestCase):
    def setUp(self):
        # Session runner points tempfile at the retained E-drive validation root.
        # Fixtures are intentionally retained, following workspace deletion rules.
        self.root = Path(tempfile.mkdtemp(prefix="legacy-qq-"))
        self.path = self.root / "Msg3.0.db"
        self.descriptor = {"app": "qq_pc", "uid": "f" * 32, "account": "100001", "timezone": "Asia/Shanghai"}

    def database(self, tables):
        with closing(sqlite3.connect(self.path)) as db:
            for table, rows in tables.items():
                escaped = '"' + table.replace('"', '""') + '"'
                db.execute("CREATE TABLE " + escaped + " (Time INTEGER,Rand INTEGER,SenderUin INTEGER,MsgContent BLOB,Info BLOB,Extra INTEGER,deleted INTEGER)")
                db.executemany("INSERT INTO " + escaped + " VALUES (?,?,?,?,?,?,?)", rows)
            db.commit()

    def read(self, table, **kwargs):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            return list(qq_legacy.records(db, self.descriptor, "Msg3.0.db", table, **kwargs))

    def test_literal_protocol_fixture_decodes_header_unicode_and_all_text_fragments(self):
        # Independently written bytes: 28-byte empty-font header, nested UTF-16
        # text field containing Chinese plus a surrogate pair, then another field.
        packet = bytes.fromhex(
            "0000000000000000 00f15365 40302010 33221100 0c008600 0000 0000"
            "01 1000 01 0600 604f3dd800de 01 0400 3c002600"
        )
        decoded = qq_legacy.decode_body(packet)
        self.assertEqual(decoded["text"], "你😀<&")
        self.assertEqual(decoded["status"], "decoded")
        self.assertEqual(decoded["type_name"], "文本")
        self.assertEqual(decoded["header"]["time"], 1700000000)
        self.assertEqual(decoded["header"]["random_le"], 0x10203040)
        self.assertEqual(decoded["header"]["random_be"], 0x40302010)
        self.assertEqual(decoded["elements"][0]["offset"], 28)

    def test_mixed_media_nickname_unknown_fields_are_preserved_without_file_access(self):
        digest = bytes(range(16))
        video = bytes(244) + bytes(v ^ 0xEF for v in digest)
        nickname = tlv(18, tlv(9, b"opaque") + tlv(2, "群昵称😀".encode("utf-16-le")))
        image = tlv(3, tlv(1, digest) + tlv(2, r"C:\images\合成.png".encode("utf-16-le")) + tlv(87, b"\x00\xff"))
        packet = body(text("说明\n") + image + tlv(7, tlv(1, digest)) + tlv(26, tlv(1, video)) + tlv(2, tlv(1, b"\x01\x02")) + nickname + tlv(250, b"unknown\x00\xff"))
        before = bytes(packet)
        decoded = qq_legacy.decode_body(packet)
        self.assertEqual(packet, before)
        self.assertEqual(decoded["sender_nickname"], "群昵称😀")
        self.assertEqual(decoded["header"]["font_name"], "宋体")
        self.assertEqual(decoded["type_name"], "混合消息")
        self.assertEqual(decoded["status"], "partial")
        self.assertIn("[表情 258]", decoded["text"])
        self.assertIn("[未识别消息元素 250]", decoded["text"])
        self.assertEqual([m["hash"] for m in decoded["media_refs"]], [digest.hex()] * 3)
        self.assertEqual(decoded["media_refs"][0]["recorded_path"], r"C:\images\合成.png")
        self.assertEqual(base64.b64decode(decoded["elements"][1]["fields"][-1]["value"]["data"]), b"\x00\xff")
        self.assertEqual(base64.b64decode(decoded["elements"][-1]["raw"]["data"]), b"unknown\x00\xff")

    def test_corrupt_element_bounds_keep_prefix_and_never_read_into_next_element(self):
        for tail in (b"\x01", b"\x01\xff\xffshort", tlv(1, b"\x01\xff\xffbad")):
            decoded = qq_legacy.decode_body(body(text("完整前缀") + tail))
            self.assertTrue(decoded["text"].startswith("完整前缀"))
            self.assertEqual(decoded["status"], "partial")
            self.assertTrue(decoded["warnings"])
        bad_utf16 = qq_legacy.decode_body(body(tlv(1, tlv(1, b"\x3d\xd8x"))))
        self.assertEqual(bad_utf16["status"], "partial")
        short_video = qq_legacy.decode_body(body(tlv(26, tlv(1, bytes(250)))))
        self.assertEqual(short_video["status"], "partial")
        self.assertNotIn("hash", short_video["media_refs"][0])

    def test_empty_plaintext_unknown_header_and_size_limits_are_explicit(self):
        self.assertEqual(qq_legacy.decode_body("原文本\n")['text'], "原文本\n")
        self.assertEqual(qq_legacy.decode_body(None)["status"], "empty")
        self.assertEqual(qq_legacy.decode_body(body(b""))["status"], "empty")
        for data in (123, b"tiny", bytes(24) + b"\xff\xff" + b"\x00\x00"):
            result = qq_legacy.decode_body(data)
            self.assertEqual(result["status"], "unrecognized")
            self.assertTrue(result["text"])
            self.assertTrue(result["warnings"])
        with patch.object(qq_legacy, "MAX_BODY_BYTES", 30):
            result = qq_legacy.decode_body(body(text("大正文")))
        self.assertEqual(result["status"], "unrecognized")
        with patch.object(qq_legacy, "MAX_ELEMENTS", 1):
            result = qq_legacy.decode_body(body(text("一") + text("二")))
        self.assertEqual(result["text"], "一")
        self.assertEqual(result["status"], "partial")

    def test_all_rows_raw_fields_outgoing_group_unknown_and_dates_survive(self):
        own = (1700000001, 100, 100001, body(text("自己")), b"\x00\xff", 9007199254741011, 0)
        incoming = (1700000002, 101, 200001, body(text("对方")), None, -1, 1)
        group = (1700000003, 102, 300001, body(text("群消息") + tlv(18, tlv(1, "群名片".encode("utf-16-le")))), b"info", 7, 0)
        unknown = (1700000004, 103, None, b"not a TLV body", b"opaque", 0, 0)
        tables = {"mr_buddy_200001": [own, incoming], "mr_group_202700001": [group], 'odd"message$shadow': [unknown]}
        self.database(tables)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(set(qq_legacy.message_tables(db)), set(tables))
        rows = []
        for table, originals in tables.items():
            actual = self.read(table)
            rows.extend(actual)
            for result, original in zip(actual, originals):
                raw = result["_desktop_raw"]["row"]
                for key, value in zip(("Time", "Rand", "SenderUin", "MsgContent", "Info", "Extra", "deleted"), original):
                    restored = base64.b64decode(raw[key]["data"]) if isinstance(value, bytes) else raw[key]
                    self.assertEqual(restored, value)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["sender"], self.descriptor["uid"])
        self.assertEqual(rows[1]["sender"], "200001")
        self.assertTrue(rows[2]["_export"]["is_group"])
        self.assertEqual(rows[2]["_export"]["sender_name"], "群名片")
        self.assertIn("legacy:group:202700001", rows[2]["conversation_id"])
        self.assertFalse(rows[3]["_export"]["direction_known"])
        self.assertEqual(rows[3]["_export"]["legacy_decode_status"], "unrecognized")
        self.assertEqual(min(r["created_time"] for r in rows), 1700000001000)
        self.assertEqual(max(r["created_time"] for r in rows), 1700000004000)
        self.assertEqual(rows[0]["_export"]["time"], "2023-11-15 06:13:21")
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_identity_survives_new_snapshot_and_metadata_updates_but_never_assumes_nt_ids(self):
        first = (1700000001, 5, 100001, body(text("相同序号")), b"old", 0, 0)
        self.database({"mr_buddy_200001": [first], "mr_buddy_200002": [first]})
        previous = self.read("mr_buddy_200001")[0]
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE mr_buddy_200001 SET Info=?,deleted=1", (b"new",))
            db.execute("INSERT INTO mr_buddy_200001 VALUES (?,?,?,?,?,?,?)", (1700000002, 5, 100001, body(text("新消息")), None, 0, 0))
            db.commit()
        current = self.read("mr_buddy_200001")
        self.assertEqual(current[0]["msg_uuid"], previous["msg_uuid"])
        self.assertNotEqual(current[1]["msg_uuid"], previous["msg_uuid"])
        self.assertNotEqual(self.read("mr_buddy_200002")[0]["msg_uuid"], previous["msg_uuid"])
        with closing(sqlite3.connect(self.path)) as db:
            moved = list(qq_legacy.records(db, self.descriptor, "moved/private.db", "mr_buddy_200001", {"200001": "新备注"}))
            other = list(qq_legacy.records(db, dict(self.descriptor, account="999001"), "Msg3.0.db", "mr_buddy_200001"))
        self.assertEqual(moved[0]["msg_uuid"], previous["msg_uuid"])
        self.assertNotEqual(other[0]["msg_uuid"], previous["msg_uuid"])
        self.assertTrue(previous["msg_uuid"].startswith("qq_pc:legacy:"))
        self.assertEqual(previous["msg_server_id"], "")

    def test_missing_direction_and_time_fields_use_only_documented_header_fallback(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE strange(msgcontent BLOB,unknown BLOB)")
            db.execute("INSERT INTO strange VALUES (?,?)", (body(text("缺少SQL字段"), timestamp=1700000010), b"a"))
            db.execute("INSERT INTO strange VALUES (?,?)", (body(text("缺少SQL字段"), timestamp=1700000010), b"b"))
            db.commit()
            self.assertEqual(qq_legacy.message_tables(db), ["strange"])
            rows = list(qq_legacy.records(db, self.descriptor, "Msg3.0.db", "strange"))
        self.assertEqual(rows[0]["created_time"], 1700000010000)
        self.assertEqual(rows[0]["_export"]["legacy_time_source"], "MsgContent.header.time")
        self.assertFalse(rows[0]["_export"]["direction_known"])
        self.assertNotEqual(rows[0]["msg_uuid"], rows[1]["msg_uuid"])

    def test_known_broken_schema_fails_instead_of_skipping_and_cancel_preserves_source(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE mr_buddy_200001(Time INTEGER)")
            with self.assertRaisesRegex(ExportError, "不能略过"):
                qq_legacy.message_tables(db)
        other = self.root / "cancel.db"
        self.path = other
        self.database({"mr_buddy_200001": [(1, 1, 100001, body(text("测试")), None, 0, 0)]})
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ExportError, "已取消"):
            self.read("mr_buddy_200001", cancel=lambda: True)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_null_identity_fields_never_merge_distinct_opaque_rows(self):
        self.database({"mr_buddy_200001": [
            (None, None, None, b"opaque", b"first", 0, 0),
            (None, None, None, b"opaque", b"second", 0, 0),
        ]})
        rows = self.read("mr_buddy_200001")
        self.assertNotEqual(rows[0]["msg_uuid"], rows[1]["msg_uuid"])
        self.assertEqual(rows[0]["created_time"], 0)
        self.assertEqual(rows[0]["_export"]["legacy_time_source"], "unavailable")

    def test_media_presence_marks_unavailable_and_raw_blob_is_complete(self):
        packet = body(text("图文") + tlv(6, tlv(1, b"synthetic-hash")))
        self.database({"mr_buddy_200001": [(1, 1, 100001, packet, None, 0, 0)]})
        row = self.read("mr_buddy_200001")[0]
        self.assertEqual(row["_export"]["media_status"], "missing")
        self.assertEqual(row["_export"]["local_media"], [])
        self.assertEqual(base64.b64decode(row["_desktop_raw"]["row"]["MsgContent"]["data"]), packet)


if __name__ == "__main__":
    unittest.main()
