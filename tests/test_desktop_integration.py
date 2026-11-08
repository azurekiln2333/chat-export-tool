"""PC adapters through the real transactional reader, using synthetic accounts."""
from __future__ import annotations

from contextlib import closing, redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from duoshan_export.library import discover_sources, import_sources, import_history, _completed_source_ids
from duoshan_export import library
from duoshan_export.apps import desktop
from duoshan_export.cli import main
from duoshan_export.errors import ExportError


def _varint(value):
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def _field(number, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return _varint((number << 3) | 2) + _varint(len(value)) + value
    return _varint(number << 3) + _varint(value)


@unittest.skipUnless(shutil.which("node"), "Node.js is required for reader integration")
class DesktopLibraryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="desktop-integration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "library"
        self.work = self.root / "work"

    def wx(self, parent, account="wxid_synthetic_1a2b", rows=None):
        source = parent / "xwechat_files" / account
        path = source / "db_storage/message/message_0.db"
        path.parent.mkdir(parents=True)
        table = "Msg_" + hashlib.md5(b"wxid_peer").hexdigest()
        with closing(sqlite3.connect(path)) as db:
            db.executescript(f"""
                CREATE TABLE Name2Id(user_name TEXT);
                INSERT INTO Name2Id VALUES ('wxid_synthetic'), ('wxid_peer');
                CREATE TABLE {table}(local_id INTEGER, server_id INTEGER, local_type INTEGER,
                    create_time INTEGER, real_sender_id INTEGER, message_content TEXT,
                    WCDB_CT_message_content INTEGER);
            """)
            db.executemany(f"INSERT INTO {table} VALUES (?,?,1,1700000000,?,?,0)",
                           rows or [(1, 9007199254741001, 1, "outgoing"),
                                    (2, 9007199254741002, 2, "incoming")])
            db.commit()
        return source

    def qq(self, parent):
        source = parent / "Tencent Files" / "123456789"
        path = source / "nt_qq/nt_db/nt_msg.db"
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
                CREATE TABLE c2c_msg_table("40001" INTEGER,"40011" INTEGER,
                    "40013" INTEGER,"40021" TEXT,"40033" TEXT,"40050" INTEGER,"40800" TEXT);
                INSERT INTO c2c_msg_table VALUES(101,2,1,'987654321','123456789',1700000000,'QQ outgoing');
                INSERT INTO c2c_msg_table VALUES(102,2,0,'987654321','987654321',1700000001,'QQ incoming');
            ''')
            db.commit()
        return source

    def legacy(self, source):
        def text_body(value, timestamp):
            content = value.encode("utf-16-le")
            field = b"\x01" + len(content).to_bytes(2, "little") + content
            header = bytes(8) + struct.pack("<III", timestamp, 7, 0) + bytes(8)
            return header + b"\x01" + len(field).to_bytes(2, "little") + field
        path = source / "Msg3.0.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE mr_buddy_987654321(Time INTEGER,Rand INTEGER,SenderUin INTEGER,MsgContent BLOB,Info BLOB)")
            db.executemany("INSERT INTO mr_buddy_987654321 VALUES (?,?,?,?,?)", [
                (1600000000, 7, 123456789, text_body("旧QQ发出", 1600000000), b"\x00\xff"),
                (1600000001, 8, 987654321, text_body("旧QQ回复", 1600000001), None),
            ])
            db.commit()
        return path

    def interchange(self, app, uid):
        source = self.root / "mixed" / app
        account = source / uid
        account.mkdir(parents=True)
        (source / "export_summary.json").write_text(json.dumps({"app": app}), encoding="utf-8")
        (account / "statistics.json").write_text(json.dumps({"app": app}), encoding="utf-8")
        row = {"msg_uuid": app + "-original", "sender": uid, "conversation_id": "peer",
               "created_time": 1700000000000, "type": 1,
               "_export": {"text": app + " retained", "local_media": [], "time": "2023-11-15 06:13:20"}}
        (account / "messages.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        return source

    def query(self, sql, params=()):
        with closing(sqlite3.connect(self.output / "reader.sqlite")) as db:
            return db.execute(sql, params).fetchall()

    def run_import(self, paths, **kwargs):
        return import_sources(paths, self.output, self.work, progress=lambda _: None, **kwargs)

    def test_mixed_qq_plaintext_databases_share_account_and_preserve_both_histories(self):
        source = self.qq(self.root / "mixed-qq")
        self.legacy(source)
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.rglob("*.db")}
        result = self.run_import([source])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.query("SELECT COUNT(*) FROM accounts"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(4,)])
        uid = self.query("SELECT uid FROM accounts")[0][0]
        rows = self.query("SELECT sender,text_original,raw_json,created_time,msg_uuid FROM messages ORDER BY created_time")
        self.assertEqual([row[1] for row in rows], ["旧QQ发出", "旧QQ回复", "QQ outgoing", "QQ incoming"])
        self.assertEqual([row[0] for row in rows], [uid, "987654321", uid, "987654321"])
        self.assertEqual(json.loads(rows[0][2])["_desktop_raw"]["row"]["Info"], {"encoding": "base64", "data": "AP8="})
        self.assertEqual((rows[0][3], rows[-1][3]), (1600000000000, 1700000001000))
        self.assertTrue(all(row[4].startswith("qq_pc:legacy:") for row in rows[:2]))
        self.assertTrue(all(not row[4].startswith("qq_pc:legacy:") for row in rows[2:]))
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})
        self.assertEqual(self.run_import([source])["imports"][0]["status"], "unchanged")

    def test_partial_qq_import_keeps_nt_visible_and_retries_same_source_until_key_recovers_legacy(self):
        source = self.qq(self.root / "partial-qq")
        legacy = self.legacy(source)
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.rglob("*.db")}
        original = desktop.decrypt_snapshot
        secret = "synthetic-only-key"
        copied_paths = []
        def require_legacy_key(snapshot, destination, **kwargs):
            if Path(snapshot).name == legacy.name:
                # Exercise real discovery/snapshot/parser/Node paths, changing
                # only the unavailable cipher condition on the private copy.
                self.assertTrue(Path(snapshot).is_relative_to(self.work))
                self.assertFalse(Path(snapshot).is_relative_to(source))
                copied_paths.append(Path(snapshot))
                if kwargs.get("keys") != {"legacy": secret}:
                    raise ExportError("合成旧QQ密钥尚未提供")
            return original(snapshot, destination, **kwargs)
        with patch.object(desktop, "decrypt_snapshot", side_effect=require_legacy_key):
            first = self.run_import([source])
            item = first["imports"][0]
            source_id = item["source_id"]
            self.assertEqual((first["status"], item["status"]), ("partial", "partial"))
            self.assertTrue(item["retry_required"])
            self.assertEqual(item["failed_databases"][0]["database"], "Msg3.0.db")
            self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(2,)])
            self.assertEqual({row[0] for row in self.query("SELECT text_original FROM messages")}, {"QQ outgoing", "QQ incoming"})
            self.assertNotIn(source_id, _completed_source_ids(self.output))
            self.assertEqual(self.query("SELECT source_id,projection_status FROM import_batches"), [("partial:" + source_id, "completed")])
            second = self.run_import([source])
            self.assertEqual(second["status"], "partial")
            self.assertEqual(second["imports"][0]["source_id"], source_id)
            self.assertTrue(second["imports"][0]["result"]["repeated"])
            self.assertEqual(len(copied_paths), 2)
            self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(2,)])
            third = self.run_import([source], desktop_keys={"legacy": secret})
        self.assertEqual(third["status"], "completed")
        self.assertEqual(third["imports"][0]["source_id"], source_id)
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(4,)])
        self.assertIn(source_id, _completed_source_ids(self.output))
        self.assertEqual(self.run_import([source])["imports"][0]["status"], "unchanged")
        terminal = [entry["status"] for entry in import_history(self.output) if entry["status"] != "started"]
        self.assertEqual(terminal, ["partial", "partial", "completed"])
        self.assertNotIn(secret, (self.output / "import_history.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})

    def test_new_qq_cache_reimports_unchanged_database_and_publishes_media(self):
        from PIL import Image
        source = self.qq(self.root / "cache-qq")
        picture = io.BytesIO()
        Image.new("RGB", (5, 7), "blue").save(picture, format="PNG")
        image = picture.getvalue()
        md5 = hashlib.md5(image).hexdigest()
        blob = _field(40800, _field(45002, 2) + _field(45424, bytes.fromhex(md5)))
        database = source / "nt_qq/nt_db/nt_msg.db"
        with closing(sqlite3.connect(database)) as db:
            db.execute('INSERT INTO c2c_msg_table VALUES(103,2,0,?,?,?,?)',
                       ("987654321", "987654321", 1700000002, blob))
            db.commit()
        db_hash = hashlib.sha256(database.read_bytes()).hexdigest()
        first = self.run_import([source])
        row = json.loads(self.query("SELECT meta_json FROM messages WHERE msg_uuid='qq_pc:server:peer:987654321:103'")[0][0])
        self.assertEqual(row["local_media"], [])
        cache = source / "nt_qq/nt_data/Pic/2025-01/Ori" / (md5 + ".png")
        cache.parent.mkdir(parents=True)
        cache.write_bytes(image)
        second = self.run_import([source])
        self.assertNotEqual(first["imports"][0]["source_id"], second["imports"][0]["source_id"])
        self.assertEqual(second["imports"][0]["status"], "completed")
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(3,)])
        row = json.loads(self.query("SELECT meta_json FROM messages WHERE msg_uuid='qq_pc:server:peer:987654321:103'")[0][0])
        self.assertEqual(row["media_status"], "ok")
        self.assertEqual((self.output / row["local_media"][0]).read_bytes(), image)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), db_hash)
        self.assertEqual(cache.read_bytes(), image)
        self.assertEqual(self.run_import([source])["imports"][0]["status"], "unchanged")
        # Full cache bytes participate even if no message references the file.
        # Same size/mtime or unchanged adapter output must not hide that change.
        unrelated = source / "FileRecv/unreferenced.bin"
        unrelated.parent.mkdir()
        unrelated.write_bytes(b"same-size-A")
        third = self.run_import([source])
        stamp = unrelated.stat().st_mtime_ns
        unrelated.write_bytes(b"same-size-B")
        os.utime(unrelated, ns=(stamp, stamp))
        fourth = self.run_import([source])
        self.assertNotEqual(third["imports"][0]["source_id"], fourth["imports"][0]["source_id"])
        self.assertIn(fourth["imports"][0]["source_id"], _completed_source_ids(self.output))
        self.assertEqual(self.run_import([source])["imports"][0]["status"], "unchanged")
        ignored = source / "UnrelatedSettings"
        ignored.mkdir()
        (ignored / "ignored.bin").write_bytes(b"not an account cache")
        self.assertEqual(self.run_import([source])["imports"][0]["status"], "unchanged")

    def test_partial_batch_committed_before_reader_error_keeps_metadata_and_remains_retryable(self):
        source = self.qq(self.root / "partial-commit")
        self.legacy(source)
        decrypt = desktop.decrypt_snapshot
        reader = library._run_reader
        def missing_legacy(snapshot, destination, **kwargs):
            if Path(snapshot).name == "Msg3.0.db":
                raise ExportError("合成旧库缺少密钥")
            return decrypt(snapshot, destination, **kwargs)
        def after_commit(*args, **kwargs):
            reader(*args, **kwargs)
            raise ExportError("合并提交后的合成输出失败")
        with patch.object(desktop, "decrypt_snapshot", side_effect=missing_legacy):
            with patch.object(library, "_run_reader", side_effect=after_commit):
                with self.assertRaisesRegex(ExportError, "提交后的"):
                    self.run_import([source])
            self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(2,)])
            self.assertEqual(_completed_source_ids(self.output), set())
            self.assertTrue((self.output / "export_summary.json").is_file())
            self.assertTrue((self.output / "media_manifest.json").is_file())
            retried = self.run_import([source])
        self.assertEqual(retried["status"], "partial")
        self.assertTrue(retried["imports"][0]["result"]["repeated"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(2,)])

    def test_cli_partial_inspection_and_import_return_nonzero_with_readable_results(self):
        source = self.qq(self.root / "partial-cli")
        self.legacy(source)
        decrypt = desktop.decrypt_snapshot
        def missing_legacy(snapshot, destination, **kwargs):
            if Path(snapshot).name == "Msg3.0.db":
                raise ExportError("合成旧库缺少密钥")
            return decrypt(snapshot, destination, **kwargs)
        commands = [
            ["inspect", str(source), "--work-dir", str(self.work)],
            ["scan", str(source), "--inspect", "--work-dir", str(self.work)],
            ["import", str(source), "-o", str(self.output), "--work-dir", str(self.work)],
        ]
        with patch.object(desktop, "decrypt_snapshot", side_effect=missing_legacy):
            for command in commands:
                with self.subTest(command=command[0]):
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        status = main([*command, "--json"])
                    result = json.loads(out.getvalue())
                    self.assertEqual(status, 1)
                    self.assertEqual(result["status"], "partial")
                    items = result["imports"] if command[0] == "import" else result["sources"]
                    self.assertEqual(items[0]["failed_databases"][0]["database"], "Msg3.0.db")
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages"), [(2,)])

    def test_mixed_sources_keep_android_rows_and_pc_updates_keep_history(self):
        android_uids = {"wechat": "1" * 32, "duoshan": "9111222233334444", "xhs": "2" * 24}
        for app, uid in android_uids.items():
            self.run_import([self.interchange(app, uid)])
        android_before = self.query("SELECT * FROM messages ORDER BY uid, msg_uuid")
        wx = self.wx(self.root / "mixed")
        qq = self.qq(self.root / "mixed")
        found = discover_sources([self.root / "mixed"])
        self.assertEqual(sorted(s["app"] for s in found), ["duoshan", "qq_pc", "wechat", "wechat_pc", "xhs"])
        uid = next(s["uid"] for s in found if s["app"] == "wechat_pc")
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for source in (wx, qq) for p in source.rglob("*.db")}
        self.run_import([wx, qq])
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})
        placeholders = ",".join("?" for _ in android_uids)
        self.assertEqual(self.query(f"SELECT * FROM messages WHERE uid IN ({placeholders}) ORDER BY uid, msg_uuid",
                                    tuple(android_uids.values())), android_before)
        self.assertEqual(dict(self.query("SELECT uid,app FROM library_accounts"))[uid], "wechat_pc")
        directions = self.query("SELECT sender,text_original FROM messages WHERE uid=? ORDER BY text_original", (uid,))
        self.assertEqual(directions, [("wxid_peer", "incoming"), (uid, "outgoing")])
        self.assertTrue((self.output / ("wxpc_" + uid) / "messages.jsonl").is_file())
        self.assertTrue(any(p.name.startswith("qqpc_") for p in self.output.iterdir()))
        repeated = self.run_import([wx, qq])
        self.assertEqual([r["status"] for r in repeated["imports"]], ["unchanged", "unchanged"])
        # A later snapshot of the same account has a different installation suffix,
        # different local row numbers, one repeated server ID and one new message.
        later = self.wx(self.root / "later", "wxid_synthetic_cafe", [
            (301, 9007199254741001, 1, "outgoing updated"),
            (303, 9007199254741003, 2, "new incoming")])
        self.run_import([later])
        self.assertEqual(self.query("SELECT COUNT(*) FROM accounts")[0][0], 5)
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages WHERE uid=?", (uid,))[0][0], 3)
        self.assertEqual({r[0] for r in self.query("SELECT text_original FROM messages WHERE uid=?", (uid,))},
                         {"incoming", "outgoing updated", "new incoming"})

    def test_identical_database_bytes_do_not_skip_different_pc_accounts(self):
        first = self.wx(self.root / "first")
        second = self.root / "second/xwechat_files/wxid_another_babe"
        shutil.copytree(first, second)
        result = self.run_import([first, second])
        self.assertEqual([r["status"] for r in result["imports"]], ["completed", "completed"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM accounts")[0][0], 2)
        self.assertEqual(self.query("SELECT COUNT(*) FROM messages")[0][0], 4)
        self.assertNotEqual(result["imports"][0]["source_id"], result["imports"][1]["source_id"])

    def test_pc_nested_database_selections_deduplicate_in_both_orders(self):
        account = self.qq(self.root / "nested")
        database = account / "nt_qq/nt_db/nt_msg.db"
        for paths in ([account, database], [database, account]):
            with self.subTest(paths=paths):
                found = discover_sources(paths)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["source"], str(account.resolve()))

    def test_cli_local_scan_and_private_key_errors(self):
        source = self.wx(self.root / "Documents")
        before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        out, err = io.StringIO(), io.StringIO()
        with patch("duoshan_export.desktop_sources.documents_directory", return_value=self.root / "Documents"), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(["scan", "--local", "--json"]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "detected")
        self.assertEqual([s["app"] for s in result["sources"]], ["wechat_pc"])
        self.assertEqual(before, {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()})
        self.assertFalse(self.work.exists())
        secret = "SYNTHETIC-PRIVATE-VALUE-DO-NOT-LOG"
        key_file = self.root / "keys.json"
        key_file.write_text('{"broken":"' + secret, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(["inspect", str(source), "--work-dir", str(self.work),
                                   "--desktop-keys", str(key_file), "--json"]), 1)
        self.assertNotIn(secret, out.getvalue() + err.getvalue())
        self.assertIn("JSON", out.getvalue() + err.getvalue())
        self.assertFalse(self.work.exists())


if __name__ == "__main__":
    unittest.main()
