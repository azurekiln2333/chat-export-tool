"""Real local native-codec tests; synthetic rows and a public fixture key only."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from duoshan_export.apps import desktop_crypto, desktop_legacy as legacy
from duoshan_export.errors import ExportError

KEY = "00112233445566778899aabbccddeeff"


@unittest.skipUnless(os.name == "nt" and (legacy.component_directory() / "legacy-qq-helper.exe").is_file(),
                     "optional local Tencent component bundle is not installed")
class LegacyNativeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="legacy-native-"))

    def fixture(self, name="Msg3.0.db", wrapped=True):
        path = self.root / name
        result = legacy._helper("fixture", {"output": str(path), "wrapped": wrapped}, progress=lambda _: None)
        self.assertTrue(result["ok"])
        return path

    def test_probe_and_roundtrip_wrapped_and_unwrapped_exact_values(self):
        self.assertEqual(legacy.probe_legacy_components(progress=lambda _: None)["architecture"], "x86")
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                source = self.fixture(str(wrapped) + ".db", wrapped)
                digest = hashlib.sha256(source.read_bytes()).digest()
                output = self.root / (str(wrapped) + "-plain.db")
                logs = []
                desktop_crypto.decrypt_snapshot(source, output, app="qq_legacy", keys={"test-account": {"legacy_key": KEY}}, account="test-account", progress=logs.append)
                with closing(sqlite3.connect(output)) as db:
                    self.assertEqual(db.execute("SELECT * FROM fixture ORDER BY id").fetchall(),
                                     [(1, "synthetic Unicode 测试", b"\0\x01\xfe\xff"), (2, "second row", None)])
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), digest)
                self.assertNotIn(KEY, "\n".join(logs))
                self.assertTrue(any(getattr(event, "total", None) for event in logs))

    def test_wrong_key_and_corruption_never_publish(self):
        source = self.fixture()
        original = source.read_bytes()
        with self.assertRaises(ExportError):
            legacy.decrypt_legacy_snapshot(source, self.root / "wrong.db", keys="11" * 16, progress=lambda _: None)
        self.assertFalse((self.root / "wrong.db").exists())
        self.assertEqual(source.read_bytes(), original)
        damaged = self.root / "damaged.db"
        damaged.write_bytes(original[:2048] + bytes(len(original) - 2048))
        with self.assertRaises(ExportError):
            legacy.decrypt_legacy_snapshot(damaged, self.root / "corrupt.db", keys=KEY, progress=lambda _: None)
        self.assertFalse((self.root / "corrupt.db").exists())

    def test_cancel_running_component_retains_source_and_partial(self):
        source = self.fixture()
        original = source.read_bytes()
        cancel = [False]
        def progress(event):
            if "复制旧版 QQ 解密工作文件" in event:
                cancel[0] = True
        with self.assertRaisesRegex(ExportError, "取消"):
            legacy.decrypt_legacy_snapshot(source, self.root / "cancelled.db", keys=KEY, progress=progress, cancel=lambda: cancel[0])
        self.assertFalse((self.root / "cancelled.db").exists())
        self.assertEqual(source.read_bytes(), original)

    def test_encrypted_classic_and_nt_partial_then_complete_reader_import(self):
        from duoshan_export.apps import desktop
        from duoshan_export.library import import_sources
        source = self.root / "Tencent Files" / "123456789"
        path = source / "nt_qq/nt_db/nt_msg.db"
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
                CREATE TABLE c2c_msg_table("40001" INTEGER,"40011" INTEGER,
                    "40013" INTEGER,"40021" TEXT,"40033" TEXT,"40050" INTEGER,"40800" TEXT);
                INSERT INTO c2c_msg_table VALUES(101,2,1,'987654321','123456789',1700000001,'NT fixture');
            ''')
        classic = source / "Msg3.0.db"
        self.assertTrue(legacy._helper("fixture", {"output": str(classic), "wrapped": True, "messages": True}, progress=lambda _: None)["ok"])
        digests = {p: hashlib.sha256(p.read_bytes()).digest() for p in (path, classic)}
        output, work = self.root / "library", self.root / "work"
        with patch.object(desktop, "recover_qq_keys", return_value={}):
            partial = import_sources([source], output, work, progress=lambda _: None)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["imports"][0]["failed_databases"][0]["database"], "Msg3.0.db")
        result = import_sources([source], output, work, desktop_keys={"123456789": {"Msg3.0.db": {"legacy_key": KEY}}}, progress=lambda _: None)
        self.assertEqual(result["status"], "completed")
        with closing(sqlite3.connect(output / "reader.sqlite")) as db:
            self.assertEqual(db.execute("SELECT text_original FROM messages ORDER BY created_time").fetchall(), [("你😀<&",), ("NT fixture",)])
            uid = db.execute("SELECT uid FROM accounts").fetchone()[0]
            self.assertEqual(db.execute("SELECT DISTINCT sender FROM messages").fetchall(), [(uid,)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts").fetchone(), (1,))
        self.assertEqual({p: hashlib.sha256(p.read_bytes()).digest() for p in digests}, digests)
        self.assertNotIn(KEY, (output / "import_history.jsonl").read_text(encoding="utf-8"))
        repeat = import_sources([source], output, work, progress=lambda _: None)
        self.assertEqual(repeat["imports"][0]["status"], "unchanged")

    def test_classic_key_only_keeps_missing_nt_key_explicit_without_process_scan(self):
        from duoshan_export.apps import desktop
        from duoshan_export.library import import_sources
        source = self.root / "Tencent Files" / "123456789"
        path = source / "nt_qq/nt_db/nt_msg.db"
        path.parent.mkdir(parents=True)
        header = bytearray(1024)
        header[:16] = b"SQLite header 3\0"
        header[32:40] = b"QQ_NT DB"
        path.write_bytes(header + bytes(4096))
        classic = source / "Msg3.0.db"
        self.assertTrue(legacy._helper("fixture", {"output": str(classic), "wrapped": True, "messages": True}, progress=lambda _: None)["ok"])
        with patch.object(desktop, "recover_qq_keys", side_effect=AssertionError("Explicit keys must not trigger a process scan")):
            result = import_sources([source], self.root / "library", self.root / "work", desktop_keys={"123456789": {"Msg3.0.db": {"legacy_key": KEY}}}, progress=lambda _: None)
        self.assertEqual(result["status"], "partial")
        failure = result["imports"][0]["failed_databases"][0]
        self.assertEqual(failure["format"], "qqnt")
        self.assertIn("缺少", failure["error"])
        with closing(sqlite3.connect(self.root / "library/reader.sqlite")) as db:
            self.assertEqual(db.execute("SELECT text_original FROM messages").fetchall(), [("你😀<&",)])
