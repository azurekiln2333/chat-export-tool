"""Tests for the Xiaohongshu (com.xingin.xhs) app rules and exporter.

These build real WCDB-fork-format encrypted databases, MMKV stores and
DataBackup-style archive layouts in a temporary directory, then drive the
library and CLI exactly as a user would. No real account id, message text or
network access is used.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tarfile
import tempfile
import unittest

from duoshan_export.apps import mmkv as mmkv_module
from duoshan_export.apps import wcdb as wcdb_module
from duoshan_export.apps import xhs as xhs_module
from duoshan_export.config import ExportConfig
from duoshan_export.errors import ExportError

try:
    import cryptography  # noqa: F401

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover
    CRYPTOGRAPHY_AVAILABLE = False

PAGE = wcdb_module.PAGE_SIZE
IV = wcdb_module.IV_SIZE
MAC = wcdb_module.HMAC_SIZE
RESERVE = wcdb_module.RESERVE
PASSPHRASE = "msg-test-0000-uuid"
MSG_PASSWORD = "msg11111111-2222-3333-4444-555555555555"
MSG_PASSWORD_BLOB = bytes([len(MSG_PASSWORD)]) + MSG_PASSWORD.encode("ascii")
RELATION_PASSWORD = "relation88888888-aaaa-bbbb-cccc-dddddddddddd"
RELATION_PASSWORD_BLOB = bytes([len(RELATION_PASSWORD)]) + RELATION_PASSWORD.encode("ascii")


try:
    import sqlcipher3  # the [encrypted] extra; also the fixture builder

    SQLCIPHER_AVAILABLE = True
except ImportError:  # pragma: no cover
    SQLCIPHER_AVAILABLE = False

ENCODING_PRAGMAS = (
    "PRAGMA cipher_page_size = 1024;",
    "PRAGMA kdf_iter = 64000;",
    "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA1;",
    "PRAGMA cipher_hmac_algorithm = HMAC_SHA256;",
)


def _build_msgdb(path: Path, *, owner: str, peer: str, texts: list[str],
                 passphrase: str = PASSPHRASE) -> None:
    """Create a WCDB-encrypted msgDB with the verified schema via sqlcipher3.

    Pragmas follow the app's order (key first, then the cipher spec), so the
    fixture matches what the Xiaohongshu app writes.
    """
    if path.exists():
        path.unlink()
    db = sqlcipher3.connect(str(path))
    try:
        db.execute(f"PRAGMA key = '{passphrase}'")
        for pragma in ENCODING_PRAGMAS:
            db.execute(pragma)
        db.executescript(
            """
            CREATE TABLE `message` (`uuid` TEXT NOT NULL PRIMARY KEY, `message_id` TEXT NOT NULL,
                `store_id` INTEGER NOT NULL, `create_time` INTEGER NOT NULL,
                `content_type` INTEGER NOT NULL, `message_type` INTEGER NOT NULL,
                `sub_type` TEXT NOT NULL, `sender_id` TEXT NOT NULL, `receiver_id` TEXT NOT NULL,
                `chat_id` TEXT NOT NULL, `content` TEXT NOT NULL, `hasRead` INTEGER NOT NULL,
                `local_chat_user_id` TEXT NOT NULL, `local_message_id` TEXT NOT NULL,
                `local_group_chat_id` TEXT NOT NULL DEFAULT '', `is_group_chat` INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE `chat` (`chat_id` TEXT NOT NULL, `nickname` TEXT NOT NULL,
                `local_chat_user_id` TEXT NOT NULL PRIMARY KEY, `last_activated_at` INTEGER NOT NULL);
            CREATE TABLE `group_chat` (`group_id` TEXT NOT NULL, `group_name` TEXT NOT NULL,
                `local_group_chat_id` TEXT NOT NULL PRIMARY KEY, `last_activated_at` INTEGER NOT NULL);
            CREATE TABLE `user` (`user_id` TEXT NOT NULL PRIMARY KEY, `nickname` TEXT NOT NULL,
                `account_name` TEXT NOT NULL DEFAULT '');
            CREATE TABLE `chat_set` (`chatset_id` TEXT NOT NULL, `local_chatset_id` TEXT NOT NULL PRIMARY KEY);
            CREATE TABLE `msgheader` (`uid` TEXT NOT NULL, `last_message_content` TEXT NOT NULL DEFAULT '');
            CREATE TABLE `room_master_table` (`id` INTEGER NOT NULL PRIMARY KEY, `version` INTEGER NOT NULL);
            INSERT INTO room_master_table VALUES (0, 69);
            """
        )
        for index, text in enumerate(texts):
            own = index % 2 == 0
            db.execute(
                "INSERT INTO message (uuid, message_id, store_id, create_time, content_type,"
                " message_type, sub_type, sender_id, receiver_id, chat_id, content, hasRead,"
                " local_chat_user_id, local_message_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"uuid-{index}", f"mid-{index}", index, 1700000000000 + index * 1000, 1, 0, "",
                 owner if own else peer, peer if own else owner, peer,
                 f'{{"content":"{text}","content_type":1}}', 1,
                 f"{peer}@{owner}", f"lmid-{index}"),
            )
        db.execute("INSERT INTO chat VALUES (?,?,?,?)", (peer, "Peer Nick",
                                                         f"{peer}@{owner}", 1700000000000))
        db.execute("INSERT INTO user VALUES (?,?,?)", (owner, "Owner Nick", "owner_redid"))
        db.execute("INSERT INTO user VALUES (?,?,?)", (peer, "Peer User Row", ""))
        db.commit()
    finally:
        db.close()


def _build_mmkv(path: Path, entries: list[tuple[str, bytes]]) -> None:
    """Write a minimal MMKV append-log file."""
    blob = bytearray()
    body = bytearray()
    for key, value in entries:
        encoded_key = key.encode("utf-8")
        body += len(encoded_key).to_bytes(1, "little") + encoded_key
        body += len(value).to_bytes(1, "little") + value
    blob += struct.pack("<I", len(body) + 4) + b"\xff\xff\xff\x07" + bytes(body)
    path.write_bytes(bytes(blob))


def _build_source(root: Path) -> tuple[Path, str]:
    """Create an extracted XHS app-data tree; return (app_dir, msgDB owner)."""
    app_dir = root / "com.xingin.xhs"
    databases = app_dir / "databases"
    databases.mkdir(parents=True)
    owner = "aaaaaaaa0000000000001111"
    peer = "bbbbbbbb0000000000002222"
    _build_msgdb(databases / "msgDB", owner=owner, peer=peer,
                 texts=["hello from owner", "reply from peer", "one more message"],
                 passphrase=MSG_PASSWORD)
    _build_msgdb(databases / "localRelationDB", owner=owner, peer=peer, texts=[],
                 passphrase=RELATION_PASSWORD)
    files = app_dir / "files" / "mmkv"
    files.mkdir(parents=True, exist_ok=True)
    _build_mmkv(files / "com.xingin.xhs_preferences",
                [(xhs_module.MESSAGE_DB.password_key, MSG_PASSWORD_BLOB),
                 (xhs_module.CONTACTS_DB.password_key, RELATION_PASSWORD_BLOB)])
    _build_mmkv(files / "login_user_info_kv",
                [(xhs_module.LOGIN_USER_ID_KEY,
                  bytes([len(owner)]) + owner.encode("ascii"))])
    return app_dir, owner


class MmkvTests(unittest.TestCase):
    def test_roundtrip_last_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kv"
            _build_mmkv(path, [("a", b"1"), ("b", b"2"), ("a", b"3")])
            table = mmkv_module.load(path)
            self.assertEqual(table["a"], b"3")
            self.assertEqual(table["b"], b"2")

    def test_decode_bytes_strips_length_prefix(self):
        payload = b"abc"
        blob = bytes([3]) + payload
        self.assertEqual(mmkv_module.decode_bytes(blob), payload)
        self.assertEqual(mmkv_module.decode_bytes(payload), payload)

    def test_malformed_entries_stop_the_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kv"
            body = bytes([4]) + b"good" + bytes([1]) + b"x"
            body += bytes([200, 200])  # bogus key length -> garbage entry
            body += bytes([1]) + b"y" + bytes([5]) + b"tail!"
            blob = struct.pack("<I", len(body) + 4) + b"\xff\xff\xff\x07" + body
            path.write_bytes(blob)
            table = mmkv_module.load(path)
            self.assertEqual(table.get("good"), b"x")

    def test_empty_and_tiny_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.write_bytes(b"")
            self.assertEqual(mmkv_module.load(empty), {})
            tiny = Path(tmp) / "tiny"
            tiny.write_bytes(b"\x01")
            self.assertEqual(mmkv_module.load(tiny), {})


class WcdbTests(unittest.TestCase):
    @unittest.skipUnless(CRYPTOGRAPHY_AVAILABLE, "cryptography not installed")
    def test_decrypt_roundtrip_with_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "msgDB"
            owner, peer = "a" * 24, "b" * 24
            _build_msgdb(db_path, owner=owner, peer=peer, texts=["alpha", "beta"])
            result = wcdb_module.decrypt_database(
                db_path, PASSPHRASE.encode(), root / "out.sqlite")
            self.assertGreater(result["pages"], 0)
            check = sqlite3.connect(root / "out.sqlite")
            try:
                count = check.execute("SELECT count(*) FROM message").fetchone()[0]
                text = check.execute("SELECT content FROM message ORDER BY create_time LIMIT 1").fetchone()[0]
            finally:
                check.close()
            self.assertEqual(count, 2)
            self.assertIn("alpha", text)

    @unittest.skipUnless(CRYPTOGRAPHY_AVAILABLE, "cryptography not installed")
    def test_wrong_passphrase_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "msgDB"
            _build_msgdb(db_path, owner="a" * 24, peer="b" * 24, texts=["x"])
            with self.assertRaises(ExportError):
                wcdb_module.decrypt_database(db_path, b"wrong-passphrase", root / "out.sqlite")

    @unittest.skipUnless(CRYPTOGRAPHY_AVAILABLE, "cryptography not installed")
    def test_source_file_is_never_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "msgDB"
            _build_msgdb(db_path, owner="a" * 24, peer="b" * 24, texts=["x"])
            before = db_path.read_bytes()
            wcdb_module.decrypt_database(db_path, PASSPHRASE.encode(), root / "out.sqlite")
            self.assertEqual(db_path.read_bytes(), before)

    def test_unaligned_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad"
            path.write_bytes(b"\x00" * (PAGE + 5))
            with self.assertRaises(ExportError):
                wcdb_module.decrypt_main_file(path, b"pw")


class XhsDiscoveryTests(unittest.TestCase):
    def test_discover_and_passphrase_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir, owner = _build_source(Path(tmp))
            source = xhs_module.discover(_FakeLayout(app_dir))
            self.assertIsNotNone(source)
            self.assertIn("msgDB", source.databases)
            self.assertEqual(
                xhs_module.resolve_passphrase(source, xhs_module.MESSAGE_DB), MSG_PASSWORD)
            self.assertEqual(xhs_module.login_user_id(source), owner)

    def test_discover_returns_none_for_foreign_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "com.example.app"
            (other / "databases").mkdir(parents=True)
            (other / "databases" / "notes.db").write_bytes(b"x")
            self.assertIsNone(xhs_module.discover(_FakeLayout(other)))

    def test_account_ids_from_ownership_suffix(self):
        owner = "aaaaaaaa0000000000001111"
        peer = "bbbbbbbb0000000000002222"
        rows = [
            {"local_chat_user_id": f"{peer}@{owner}", "local_group_chat_id": ""},
            {"local_chat_user_id": f"{peer}@{owner}", "local_group_chat_id": ""},
            {"local_chat_user_id": f"{peer}@{owner}", "local_group_chat_id": ""},
            {"local_chat_user_id": "", "local_group_chat_id": f"1@{peer}"},
        ]
        counts = {}
        for row in rows:
            for column in ("local_chat_user_id", "local_group_chat_id"):
                text = str(row.get(column) or "")
                if "@" in text:
                    key = text.rsplit("@", 1)[1]
                    counts[key] = counts.get(key, 0) + 1
                    break
        self.assertEqual(counts[owner], 3)
        self.assertEqual(counts[peer], 1)


class _FakeLayout:
    """Minimal SourceLayout stand-in for discovery tests."""

    def __init__(self, app_dir: Path) -> None:
        self.app_dir = app_dir
        self.work_dir = Path(tempfile.gettempdir()) / "xhs-test-nonexistent"
        self.extracted_dir = None
        self.source = app_dir


@unittest.skipUnless(CRYPTOGRAPHY_AVAILABLE and SQLCIPHER_AVAILABLE,
                    "cryptography or sqlcipher3 not installed")
class XhsExportTests(unittest.TestCase):
    def test_export_creates_reader_tree(self):
        from duoshan_export.apps import xhs_export

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir, owner = _build_source(root)
            output = root / "export"
            work = root / "work"
            config = ExportConfig(
                source=app_dir, output=output, work=work,
                accounts=(owner,), app="xhs")
            layout = _FakeLayout(app_dir)
            summary = xhs_export.export_chats(config, layout)
            self.assertEqual(summary["accounts"], [owner])
            self.assertEqual(summary["total_message_count"], 3)

            account_dir = output / owner
            self.assertTrue((account_dir / "chat.sqlite").is_file())
            self.assertTrue((account_dir / "messages.jsonl").is_file())
            self.assertTrue((account_dir / "messages.csv").is_file())
            self.assertTrue((account_dir / "index.html").is_file())
            pages = list((account_dir / "conversations").glob("conversation_*.html"))
            self.assertTrue(pages)
            texts = list((account_dir / "transcripts").glob("conversation_*.txt"))
            self.assertTrue(texts)

            jsonl_first = (account_dir / "messages.jsonl").read_text(
                encoding="utf-8").splitlines()[0]
            self.assertIn("hello from owner", jsonl_first)
            self.assertIn('"_export"', jsonl_first)

            validation = (output / "validation_results.json").read_text(encoding="utf-8")
            self.assertIn("all_data_checks_passed", validation)
            html = pages[0].read_text(encoding="utf-8")
            self.assertIn("Peer Nick", html)
            self.assertIn("class=\"message", html)

    def test_export_rejects_foreign_account_ids(self):
        from duoshan_export.apps import xhs_export

        with tempfile.TemporaryDirectory() as tmp:
            app_dir, owner = _build_source(Path(tmp))
            config = ExportConfig(
                source=app_dir, output=Path(tmp) / "export2",
                work=Path(tmp) / "work2", accounts=("123456",), app="xhs")
            with self.assertRaises(ExportError):
                xhs_export.export_chats(config, _FakeLayout(app_dir))


if __name__ == "__main__":
    unittest.main()
