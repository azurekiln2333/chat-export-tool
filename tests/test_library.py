"""Independent multi-source importer tests; fixtures never use personal data."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from duoshan_export.errors import ExportError
from duoshan_export.library import (discover_sources, inspect_sources, import_sources, import_history,
    _source_signature, _prepare_reader_stage, _merge_manifest, _library_lock, _run_reader)
from duoshan_export.progress import ProgressEvent


class LibraryTests(unittest.TestCase):
    def setUp(self):
        # OS temp is outside the user's no-deletion analysis workspace.
        self.temp = tempfile.TemporaryDirectory(prefix="chat-library-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def export(self, folder, app, uid, rows, media=None):
        root = self.root / folder
        root.mkdir(exist_ok=True)
        (root / "export_summary.json").write_text(json.dumps({"app": app, "timezone": "Asia/Shanghai"}))
        account = root / uid
        account.mkdir(exist_ok=True)
        (account / "statistics.json").write_text(json.dumps({"app": app, "nickname": app + " test"}))
        (account / "messages.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        for relative, value in (media or {}).items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        return root

    def message(self, uuid, uid="9100000000000001", text="first", media=None):
        return {"msg_uuid": uuid, "conversation_id": "peer", "sender": uid,
                "created_time": 1700000000000, "type": 1, "msg_server_id": 9007199254740997,
                "_export": {"text": text, "time": "2023-11-15 06:13:20", "sender_name": "本人",
                            "conversation_name": "测试", "local_media": media or []}}

    def test_discovery_splits_android_users_and_reads_archive_package(self):
        parent = self.root / "backups"
        for package, users in (("com.tencent.mm", ["user_0", "user_999"]), ("my.maya.android", ["user_0"]), ("com.xingin.xhs", ["user_0"])):
            for user in users:
                p = parent / package / user
                p.mkdir(parents=True)
                with tarfile.open(p / "user.tar", "w") as tar:
                    data = b"fixture"
                    member = tarfile.TarInfo(package + "/files/placeholder")
                    member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))
        found = discover_sources([parent, parent / "com.tencent.mm/user_0"])
        self.assertEqual([r["app"] for r in found], ["duoshan", "wechat", "wechat", "xhs"])
        standalone = self.root / "renamed.tar"
        shutil.copyfile(parent / "com.xingin.xhs/user_0/user.tar", standalone)
        self.assertEqual(discover_sources([standalone])[0]["app"], "xhs")
        self.assertEqual(hashlib.sha256(standalone.read_bytes()).digest(), hashlib.sha256((parent / "com.xingin.xhs/user_0/user.tar").read_bytes()).digest())

    def test_unknown_input_fails_and_content_hash_detects_same_size_changes(self):
        p = self.root / "unknown"
        p.mkdir()
        with self.assertRaises(ExportError):
            discover_sources([p])
        f = p / "state.db"
        f.write_bytes(b"abc")
        stat = f.stat()
        before = _source_signature(p, lambda _: None)
        f.write_bytes(b"abd")
        os.utime(f, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertNotEqual(before, _source_signature(p, lambda _: None))

    def test_signature_progress_matches_original_digest_and_excludes_apk(self):
        source = self.root / "source"
        source.mkdir()
        (source / "one.db").write_bytes(b"abc")
        (source / "two.db").write_bytes(b"12345")
        (source / "apk.tar.zst").write_bytes(b"excluded")
        digest = hashlib.sha256(b"library-source-v1\n")
        for name, data in (("one.db", b"abc"), ("two.db", b"12345")):
            digest.update(name.encode() + b"\0" + str(len(data)).encode() + b"\0")
            digest.update(hashlib.sha256(data).hexdigest().encode() + b"\n")
        logs = []
        self.assertEqual(_source_signature(source, logs.append), digest.hexdigest())
        final = logs[-1]
        self.assertEqual((final.completed, final.total, final.unit, final.state), (8, 8, "字节", "completed"))

    def test_reader_preparation_reports_exact_bytes_and_preserves_bom_and_bigints(self):
        uid = "9100000000000001"
        row = self.message("unicode", text="中文😀")
        source = self.export("source", "Duoshan", uid, [row])
        messages = source / uid / "messages.jsonl"
        raw = b"\xef\xbb\xbf" + messages.read_bytes().replace(b"\n", b"\r\n") + b"\r\n"
        messages.write_bytes(raw)
        logs = []
        _prepare_reader_stage(source, self.root / "stage", self.root / "library", "duoshan", logs.append)
        final = [event for event in logs if isinstance(event, ProgressEvent) and event.stage.startswith("准备阅读器记录")][-1]
        self.assertEqual((final.completed, final.total, final.state), (len(raw), len(raw), "completed"))
        self.assertEqual(json.loads((self.root / "stage" / uid / "messages.jsonl").read_text(encoding="utf-8")), row)
        self.assertEqual(messages.read_bytes(), raw)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for bridge integration")
    def test_reader_events_stream_before_exit_and_heartbeat_during_wait(self):
        script = self.root / "progress.mjs"
        script.write_text('''
process.stderr.write('CHAT_ARCHIVE_PROGRESS '+JSON.stringify({message:'合并记录',stage:'合并记录',completed:2,total:7,unit:'条',state:'running'})+'\\n');
setTimeout(() => process.stdout.write(JSON.stringify({status:'completed'})), 150);
''', encoding="utf-8")
        logs = []
        with patch("duoshan_export.library._node_command", return_value=(shutil.which("node"), script)), patch("duoshan_export.library.READER_PROGRESS_INTERVAL", 0.02):
            result = _run_reader("node", [], logs.append)
        self.assertEqual(result["status"], "completed")
        heartbeats = [event for event in logs if "仍在处理" in event and event.stage == "合并记录"]
        self.assertTrue(heartbeats)
        self.assertTrue(all((event.completed, event.total) == (2, 7) for event in heartbeats))
        self.assertEqual(logs[-1].state, "completed")
        self.assertFalse(any(t.name.startswith("reader-import-") for t in threading.enumerate()))

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for bridge integration")
    def test_invalid_reader_result_never_reports_merge_complete(self):
        script = self.root / "invalid.mjs"
        for output in ("invalid json", "null", "[]", '{"status":"error"}'):
            with self.subTest(output=output):
                script.write_text("process.stdout.write(" + json.dumps(output) + ");", encoding="utf-8")
                logs = []
                with patch("duoshan_export.library._node_command", return_value=(shutil.which("node"), script)):
                    with self.assertRaisesRegex(ExportError, "invalid result"):
                        _run_reader("node", [], logs.append)
                self.assertFalse(any(getattr(event, "state", None) == "completed" for event in logs))
                self.assertEqual(logs[-1].state, "failed")

    def test_selected_package_and_nested_sources_are_protected_before_discovery(self):
        package = self.root / "backup" / "com.tencent.mm"
        user = package / "user_0"
        user.mkdir(parents=True)
        archive = user / "user.tar"
        with tarfile.open(archive, "w") as tar:
            data = b"synthetic"
            member = tarfile.TarInfo("com.tencent.mm/MicroMsg/placeholder")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
        before = {p.relative_to(package).as_posix(): p.read_bytes() for p in package.rglob("*") if p.is_file()}
        for selected in (package, user, archive):
            with self.subTest(selected=selected.name):
                with self.assertRaisesRegex(ExportError, "separate"):
                    inspect_sources([selected], package / "work")
                with self.assertRaisesRegex(ExportError, "separate"):
                    import_sources([selected], package / "output", self.root / "work")
                self.assertFalse((package / "work").exists())
                self.assertFalse((package / "output").exists())
        self.assertEqual({p.relative_to(package).as_posix(): p.read_bytes() for p in package.rglob("*") if p.is_file()}, before)

    def test_invalid_manifest_and_interrupted_metadata_publish_preserve_live_files(self):
        from duoshan_export import library as module
        source = self.export("first", "Duoshan", "9100000000000001", [self.message("one")])
        library = self.root / "library"
        library.mkdir()
        original = '{"retained": "original"}'
        (library / "media_manifest.json").write_text(original)
        (library / "decoration_manifest.json").write_text("malformed json")
        with self.assertRaisesRegex(ExportError, "Invalid JSON"):
            import_sources([source], library, self.root / "work", progress=lambda _: None)
        self.assertEqual((library / "media_manifest.json").read_text(), original)
        self.assertFalse((library / "reader.sqlite").exists())
        (library / "decoration_manifest.json").write_text("{}")
        real_write = module._retain_and_write
        def fail_second(path, value, history):
            if path.name == "decoration_manifest.json":
                raise OSError("synthetic write failure")
            return real_write(path, value, history)
        with patch.object(module, "_retain_and_write", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "synthetic"):
                import_sources([source], library, self.root / "work", progress=lambda _: None)
        self.assertEqual((library / "media_manifest.json").read_text(), original)
        self.assertEqual((library / "decoration_manifest.json").read_text(), "{}")
        self.assertFalse((library / "reader.sqlite").exists())

    def test_assets_are_content_addressed_and_original_raw_fields_preserved(self):
        uid = "9100000000000001"
        row = self.message("one", media=["attachments/image/same.png"])
        source = self.export("first", "Duoshan", uid, [row], {"attachments/image/same.png": b"picture-one"})
        original = (source / uid / "messages.jsonl").read_bytes()
        library = self.root / "library"
        library.mkdir()
        stage = self.root / "stage"
        _prepare_reader_stage(source, stage, library, "duoshan", lambda _: None)
        projected = json.loads((stage / uid / "messages.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(projected["msg_server_id"], 9007199254740997)
        self.assertEqual({k: v for k,v in row.items() if k != "_export"}, {k:v for k,v in projected.items() if k != "_export"})
        rel = projected["_export"]["local_media"][0]
        self.assertEqual((library / rel).read_bytes(), b"picture-one")
        self.assertEqual((source / uid / "messages.jsonl").read_bytes(), original)
        (source / "attachments/image/same.png").write_bytes(b"picture-two")
        _prepare_reader_stage(source, self.root / "stage2", library, "duoshan", lambda _: None)
        self.assertEqual((library / rel).read_bytes(), b"picture-one")
        self.assertEqual(len(list((library / "attachments").rglob("*.png"))), 2)

    def test_unsafe_or_missing_paths_fail_and_offline_manifest_keeps_media(self):
        uid = "9100000000000001"
        source = self.export("bad", "Duoshan", uid, [self.message("bad", media=["attachments/../../secret"])])
        library = self.root / "out"
        library.mkdir()
        with self.assertRaises(ExportError):
            _prepare_reader_stage(source, self.root / "stage", library, "duoshan", lambda _: None)
        old = {"media": {"url": {"status": "ok", "file": "attachments/image.png"}}}
        self.assertEqual(_merge_manifest(old, {"media": {"url": {"status": "failed", "file": ""}}}), old)

    def test_library_lock_refuses_second_writer(self):
        with _library_lock(self.root / "library"):
            with self.assertRaises(ExportError):
                with _library_lock(self.root / "library"):
                    self.fail("A second writer acquired the library lock")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for integration")
    def test_raw_duoshan_backup_flows_through_unchanged_adapter(self):
        from tests.synthetic import build_app_source, ACCOUNT_ME, base_rows
        source = self.root / "raw"
        build_app_source(source)
        before = _source_signature(source, lambda _: None)
        output, work = self.root / "library", self.root / "work"
        result = import_sources([source], output, work, progress=lambda _: None)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(_source_signature(source, lambda _: None), before)
        db = sqlite3.connect(output / "reader.sqlite")
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE uid=?", (ACCOUNT_ME,)).fetchone()[0], len(base_rows(ACCOUNT_ME)))
            for meta, in db.execute("SELECT meta_json FROM messages"):
                value = json.loads(meta)
                for p in value.get("local_media", []):
                    self.assertTrue((output / p).is_file(), p)
        finally:
            db.close()

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for integration")
    def test_separate_repeat_and_newer_imports_preserve_edits_and_apps(self):
        uid, xuid = "9100000000000001", "a123456789abcdef12345678"
        source = self.export("duoshan", "Duoshan", uid, [self.message("one")])
        xrow = {"uuid": "xone", "local_chat_user_id": "peer@" + xuid, "local_group_chat_id": "",
                "sender_id": xuid, "create_time": 1700000000000, "content_type": 1,
                "_export": {"text": "hello", "time": "2023-11-15 06:13:20", "conversation_name": "X", "sender_name": "self"}}
        xhs = self.export("xhs", "Xiaohongshu", xuid, [xrow])
        output, work = self.root / "library", self.root / "work"
        first = import_sources([source], output, work, progress=lambda _: None)
        self.assertEqual(first["status"], "completed")
        self.assertTrue((output / ("dy_" + uid) / "messages.jsonl").is_file())
        repeat = import_sources([source], output, work, progress=lambda _: None)
        self.assertEqual(repeat["imports"][0]["status"], "unchanged")
        db = sqlite3.connect(output / "reader.sqlite")
        db.execute("INSERT INTO message_edits(uid,msg_uuid,text_override,note,hidden,revision) VALUES(?,?,?,?,?,?)",
                   (uid,"one","user edit","note",0,1))
        db.commit()
        db.close()
        import_sources([xhs], output, work, progress=lambda _: None)
        # New backup no longer contains old message. The library must keep it.
        self.export("duoshan", "Duoshan", uid, [self.message("two", text="new")])
        import_sources([source], output, work, progress=lambda _: None)
        db = sqlite3.connect(output / "reader.sqlite")
        try:
            self.assertEqual(db.execute("SELECT uid,count(*) FROM messages GROUP BY uid ORDER BY uid").fetchall(), [(uid,2),(xuid,1)])
            self.assertEqual(db.execute("SELECT text_override,note,revision FROM message_edits WHERE uid=? AND msg_uuid='one'", (uid,)).fetchone(), ("user edit","note",1))
        finally:
            db.close()
        self.assertEqual(sum(r["status"] == "completed" for r in import_history(output)),3)
        self.assertTrue((output / ("xhs_" + xuid)).is_dir())


if __name__ == "__main__":
    unittest.main()
