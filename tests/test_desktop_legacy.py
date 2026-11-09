"""Publication and source-retention boundaries for classic QQ imports."""
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


class DesktopLegacyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="desktop-legacy-"))
        self.source = self.root / "Msg3.0.db"
        self.output = self.root / "plain.db"
        self.source.write_bytes(b"SQLite header 3\0" + bytes(4096))
        self.digest = hashlib.sha256(self.source.read_bytes()).digest()

    def plain(self, path):
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("CREATE TABLE fixture(id INTEGER, body TEXT, raw BLOB)")
            db.execute("INSERT INTO fixture VALUES(1,?,?)", ("中文正文", b"\x00\xfe\xff"))
            db.commit()

    def test_wrapped_plaintext_needs_no_key_or_component(self):
        fixture = self.root / "fixture.db"
        self.plain(fixture)
        self.source.write_bytes(b"SQLite header 3\0" + bytes(1008) + fixture.read_bytes())
        digest = hashlib.sha256(self.source.read_bytes()).digest()
        result = desktop_crypto.decrypt_snapshot(self.source, self.output, app="qq_legacy", progress=lambda _: None)
        with closing(sqlite3.connect(result)) as db:
            self.assertEqual(db.execute("SELECT * FROM fixture").fetchall(), [(1, "中文正文", b"\x00\xfe\xff")])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), digest)

    def test_no_nt_key_truncation_or_empty_success(self):
        with patch.object(legacy, "probe_legacy_components") as probe:
            with self.assertRaisesRegex(ExportError, "16 字节"):
                legacy.decrypt_legacy_snapshot(self.source, self.output, keys=KEY * 2, progress=lambda _: None)
            probe.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_wrong_key_and_crash_do_not_publish(self):
        with patch.object(legacy, "probe_legacy_components"), patch.object(legacy, "_helper", return_value={"ok": False, "error": "sql_26"}):
            with self.assertRaisesRegex(ExportError, "密钥未通过"):
                legacy.decrypt_legacy_snapshot(self.source, self.output, keys=KEY, progress=lambda _: None)
        self.assertFalse(self.output.exists())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), self.digest)

    def test_native_success_requires_independent_sqlite_integrity(self):
        def fake(command, request, **kwargs):
            Path(request["output"]).write_bytes(legacy.SQLITE_HEADER + bytes(1008))
            return {"ok": True}
        with patch.object(legacy, "probe_legacy_components"), patch.object(legacy, "_helper", side_effect=fake):
            with self.assertRaises(ExportError):
                legacy.decrypt_legacy_snapshot(self.source, self.output, keys=KEY, progress=lambda _: None)
        self.assertFalse(self.output.exists())

    def test_changed_snapshot_is_not_published(self):
        def fake(command, request, **kwargs):
            self.plain(Path(request["output"]))
            self.source.write_bytes(self.source.read_bytes() + b"changed")
            return {"ok": True}
        with patch.object(legacy, "probe_legacy_components"), patch.object(legacy, "_helper", side_effect=fake):
            with self.assertRaisesRegex(ExportError, "期间变化"):
                legacy.decrypt_legacy_snapshot(self.source, self.output, keys=KEY, progress=lambda _: None)
        self.assertFalse(self.output.exists())

    def test_cancel_existing_output_and_sidecars(self):
        with self.assertRaisesRegex(ExportError, "取消"):
            legacy.decrypt_legacy_snapshot(self.source, self.output, keys=KEY, cancel=lambda: True)
        self.output.write_bytes(b"retained")
        with self.assertRaisesRegex(ExportError, "覆盖"):
            legacy.decrypt_legacy_snapshot(self.source, self.output, keys=KEY)
        self.assertEqual(self.output.read_bytes(), b"retained")
        for suffix in ("-wal", "-journal"):
            self.source.with_name(self.source.name + suffix).write_bytes(b"unmerged")
        with self.assertRaisesRegex(ExportError, "WAL/journal"):
            legacy.decrypt_legacy_snapshot(self.source, self.root / "new.db", keys=KEY)

    def test_bundle_missing_or_wrong_version(self):
        with self.assertRaisesRegex(ExportError, "组件包"):
            legacy._bundle(self.root)
        (self.root / "legacy-qq-helper.exe").write_bytes(b"not executable")
        (self.root / "KernelUtil.dll").write_bytes(b"unverified")
        with self.assertRaisesRegex(ExportError, "版本尚未验证"):
            legacy._bundle(self.root)

    def test_multiple_candidates_keep_failures_and_redact_secrets(self):
        requests, logs = [], []
        def fake(command, request, **kwargs):
            requests.append(request)
            if len(requests) == 1:
                Path(request["output"]).write_bytes(b"retained failure")
                return {"ok": False, "error": "sql_26"}
            self.plain(Path(request["output"]))
            return {"ok": True}
        with patch.object(legacy, "probe_legacy_components"), patch.object(legacy, "_helper", side_effect=fake):
            legacy.decrypt_legacy_snapshot(self.source, self.output, keys=["11" * 16, KEY], progress=logs.append)
        self.assertTrue(Path(requests[0]["output"]).is_file())
        self.assertTrue(self.output.is_file())
        self.assertNotIn(KEY, "\n".join(logs))
        self.assertNotIn("11" * 16, "\n".join(logs))
