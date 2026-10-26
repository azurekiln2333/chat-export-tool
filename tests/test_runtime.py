"""Focused tests for runtime safety: locations, output ownership, reuse integrity.

These are self-contained synthetic fixtures (no real backup data, no network,
no SQLCipher and no FFmpeg) so they run in every environment. They cover the
behaviour that protects a user's files:

* configuration validation and timezone errors
* source/output/work overlap rejection
* output ownership and ``--resume`` source-identity checks
* full-content source signatures
* media reuse hash/size verification and overlap refusal
* media indexing that only adds ``export_`` objects and never changes ``msg``
* CLI failures return a nonzero code with a clean message (no traceback)
* ``open_database`` closes its connection on exit
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY / "src") not in sys.path:
    sys.path.insert(0, str(REPOSITORY / "src"))

from duoshan_export import cli, media_index, reuse  # noqa: E402
from duoshan_export.config import ExportConfig  # noqa: E402
from duoshan_export.database import open_database  # noqa: E402
from duoshan_export.errors import ExportError  # noqa: E402
from duoshan_export.runtime import (  # noqa: E402
    STATE_FILE,
    begin_export,
    check_locations,
    load_state,
    source_signature,
)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def make_source(root: Path, payload: bytes = b"first-version") -> Path:
    """A minimal app-data-shaped source with one database file."""
    (root / "databases").mkdir(parents=True, exist_ok=True)
    (root / "databases" / "encrypted_111111_im.db").write_bytes(payload)
    return root


def make_message(uuid: str, content: str, sender: str = "1") -> dict:
    return {"msg_uuid": uuid, "type": 1, "content": content, "sender": sender, "conversation_id": "c1"}


def make_media_export(root: Path, uid: str, *, content: str = "hello",
                      media: bytes = b"voice-bytes") -> tuple[Path, dict]:
    """An export-shaped folder with one message and one reusable media asset."""
    account = root / uid
    write_jsonl(account / "messages.jsonl", [make_message("u1", content)])
    asset = account / "attachments" / "voice" / "clip.amr"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(media)
    relative = f"{uid}/attachments/voice/clip.amr"
    record = {"kind": "voice", "status": "ok", "resource_id": "r1", "path": relative,
              "bytes": len(media), "sha256": sha256_bytes(media), "duration": 1.0, "message_count": 1}
    manifest = {
        "resources": [dict(record, frames=1)],
        "messages": {f"{uid}:u1": dict(record)},
        "statistics": {},
    }
    write_json(root / "media_manifest.json", manifest)
    write_json(root / "decoration_manifest.json", {"bubbles": {}, "inline_emoji": {}})
    return root, manifest


class RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="duoshan-runtime-")
        self.tmp = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def config(self, source: Path, output: Path, work: Path | None = None, **kwargs) -> ExportConfig:
        return ExportConfig(source=source, output=output, work=work or output / ".work", **kwargs)


class ConfigTests(RuntimeCase):
    def test_paths_are_resolved_and_defaults_apply(self) -> None:
        config = self.config(self.tmp / "src", self.tmp / "out")
        self.assertTrue(config.source.is_absolute())
        self.assertEqual(config.work, (self.tmp / "out" / ".work").resolve())
        self.assertEqual(config.timezone, "Asia/Shanghai")
        self.assertEqual(config.accounts, ())

    def test_account_ids_must_be_ascii_digits(self) -> None:
        for bad in (("abc",), ("12 3",), ("1", "١")):
            with self.assertRaises(ExportError):
                self.config(self.tmp / "src", self.tmp / "out", accounts=bad)

    def test_numeric_bounds_are_enforced(self) -> None:
        with self.assertRaises(ExportError):
            self.config(self.tmp / "src", self.tmp / "a", page_size=0)
        with self.assertRaises(ExportError):
            self.config(self.tmp / "src", self.tmp / "b", page_size=5001)
        with self.assertRaises(ExportError):
            self.config(self.tmp / "src", self.tmp / "c", workers=0)
        with self.assertRaises(ExportError):
            self.config(self.tmp / "src", self.tmp / "d", workers=33)

    def test_unknown_timezone_is_actionable(self) -> None:
        with self.assertRaises(ExportError) as caught:
            self.config(self.tmp / "src", self.tmp / "out", timezone="Not/AZone")
        self.assertIn("timezone", str(caught.exception).lower())


class LocationTests(RuntimeCase):
    def test_default_work_inside_output_is_allowed(self) -> None:
        check_locations(self.config(self.tmp / "src", self.tmp / "out"))

    def test_separate_work_is_allowed(self) -> None:
        check_locations(self.config(self.tmp / "src", self.tmp / "out", self.tmp / "work"))

    def test_output_inside_source_is_rejected(self) -> None:
        src = self.tmp / "src"
        src.mkdir()
        with self.assertRaises(ExportError):
            check_locations(self.config(src, src / "out"))

    def test_work_inside_source_is_rejected(self) -> None:
        src = self.tmp / "src"
        src.mkdir()
        with self.assertRaises(ExportError):
            check_locations(self.config(src, self.tmp / "out", src / "work"))

    def test_source_inside_output_is_rejected(self) -> None:
        out = self.tmp / "out"
        out.mkdir()
        with self.assertRaises(ExportError):
            check_locations(self.config(out / "src", out))

    def test_work_cannot_contain_output_or_equal_it(self) -> None:
        work = self.tmp / "work"
        with self.assertRaises(ExportError):
            check_locations(self.config(self.tmp / "src", work / "out", work))
        with self.assertRaises(ExportError):
            check_locations(self.config(self.tmp / "src", self.tmp / "same", self.tmp / "same"))


class StateTests(RuntimeCase):
    def test_fresh_output_writes_state_without_touching_source(self) -> None:
        src = make_source(self.tmp / "src")
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in src.rglob("*") if p.is_file()}
        state = begin_export(self.config(src, self.tmp / "out"))
        self.assertEqual(state["status"], "in_progress")
        self.assertTrue((self.tmp / "out" / STATE_FILE).is_file())
        after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in src.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "begin_export must not modify the source")

    def test_non_empty_output_requires_resume(self) -> None:
        src = make_source(self.tmp / "src")
        out = self.tmp / "out"
        out.mkdir()
        (out / "stray.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(ExportError) as caught:
            begin_export(self.config(src, out))
        self.assertIn("empty", str(caught.exception).lower())

    def test_output_that_is_a_file_is_rejected(self) -> None:
        src = make_source(self.tmp / "src")
        out = self.tmp / "out"
        out.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ExportError):
            begin_export(self.config(src, out))

    def test_missing_source_is_rejected(self) -> None:
        with self.assertRaises(ExportError):
            begin_export(self.config(self.tmp / "missing", self.tmp / "out"))

    def test_resume_with_same_source_and_accounts_is_allowed(self) -> None:
        src = make_source(self.tmp / "src")
        out = self.tmp / "out"
        begin_export(self.config(src, out, accounts=("111111",)))
        state = begin_export(self.config(src, out, accounts=("111111",), resume=True))
        self.assertEqual(state["accounts"], ["111111"])
        self.assertEqual(load_state(out)["source_signature"], state["source_signature"])

    def test_resume_rejects_a_changed_source(self) -> None:
        src = make_source(self.tmp / "src")
        out = self.tmp / "out"
        begin_export(self.config(src, out))
        database = src / "databases" / "encrypted_111111_im.db"
        stat = database.stat()
        database.write_bytes(b"second-ersion")  # same length, restored mtime below
        os.utime(database, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaises(ExportError) as caught:
            begin_export(self.config(src, out, resume=True))
        self.assertIn("changed", str(caught.exception).lower())

    def test_resume_rejects_a_different_account_selection(self) -> None:
        src = make_source(self.tmp / "src")
        out = self.tmp / "out"
        begin_export(self.config(src, out, accounts=("111111",)))
        with self.assertRaises(ExportError):
            begin_export(self.config(src, out, accounts=("222222",), resume=True))

    def test_source_signature_tracks_full_content(self) -> None:
        src = make_source(self.tmp / "src")
        first = source_signature(src)
        self.assertEqual(first, source_signature(src), "signature must be stable")

        database = src / "databases" / "encrypted_111111_im.db"
        stat = database.stat()
        database.write_bytes(b"first-versioN")  # one byte differs, same length
        os.utime(database, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        second = source_signature(src)
        self.assertNotEqual(first, second, "same size+mtime but different content must differ")

        (src / "databases" / "extra.db").write_bytes(b"new")
        self.assertNotEqual(second, source_signature(src), "a new database file must change the signature")


class ReuseTests(RuntimeCase):
    def test_copies_verified_media_and_rewrites_paths(self) -> None:
        old, manifest = make_media_export(self.tmp / "old", "111111")
        new, _ = make_media_export(self.tmp / "new", "111111")
        result = reuse.reuse_media(old, new)
        self.assertEqual(result["copied_files"], 1)
        self.assertEqual(result["matching_messages"], 1)
        copied = new / "111111" / "attachments" / "voice" / "clip.amr"
        self.assertTrue(copied.is_file())
        self.assertEqual(sha256_bytes(copied.read_bytes()), sha256_bytes(b"voice-bytes"))
        written = json.loads((new / "media_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(written["messages"]["111111:u1"]["path"], "111111/attachments/voice/clip.amr")

    def test_rejects_media_whose_content_does_not_match_the_manifest(self) -> None:
        old, _ = make_media_export(self.tmp / "old", "111111")
        new, _ = make_media_export(self.tmp / "new", "111111")
        (old / "111111" / "attachments" / "voice" / "clip.amr").write_bytes(b"tampered")
        with self.assertRaises(ExportError) as caught:
            reuse.reuse_media(old, new)
        self.assertIn("does not match", str(caught.exception))

    def test_refuses_overlapping_export_directories(self) -> None:
        old, _ = make_media_export(self.tmp / "old", "111111")
        with self.assertRaises(ExportError):
            reuse.reuse_media(old, old)
        nested = old / "nested"
        nested.mkdir()
        with self.assertRaises(ExportError):
            reuse.reuse_media(old, nested)

    def test_only_matches_messages_that_are_identical_in_both_exports(self) -> None:
        old, _ = make_media_export(self.tmp / "old", "111111", content="hello")
        new, _ = make_media_export(self.tmp / "new", "111111", content="different")
        (new / "111111" / "attachments" / "voice" / "clip.amr").unlink()
        result = reuse.reuse_media(old, new)
        self.assertEqual(result["matching_messages"], 0)
        self.assertEqual(result["copied_files"], 0)
        self.assertFalse((new / "111111" / "attachments" / "voice" / "clip.amr").exists())

    def test_missing_manifest_is_tolerated(self) -> None:
        old, _ = make_media_export(self.tmp / "old", "111111")
        new, _ = make_media_export(self.tmp / "new", "111111")
        (old / "media_manifest.json").unlink()
        result = reuse.reuse_media(old, new)
        self.assertEqual(result["copied_files"], 0)

    def test_legacy_manifest_and_jsonl_names_are_found(self) -> None:
        old, _ = make_media_export(self.tmp / "old", "111111")
        new, _ = make_media_export(self.tmp / "new", "111111")
        (old / "media_manifest.json").rename(old / "媒体清单.json")
        (old / "111111" / "messages.jsonl").rename(old / "111111" / "聊天记录.jsonl")
        result = reuse.reuse_media(old, new)
        self.assertEqual(result["copied_files"], 1)
        self.assertEqual(result["matching_messages"], 1)


class MediaIndexTests(RuntimeCase):
    def _build_export(self) -> tuple[Path, str]:
        uid = "111111"
        output = self.tmp / "export"
        account = output / uid
        account.mkdir(parents=True)
        asset = account / "attachments" / "voice" / "clip.amr"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"voice-bytes")
        connection = sqlite3.connect(str(account / "chat.sqlite"))
        try:
            connection.execute(
                "CREATE TABLE msg (msg_uuid TEXT PRIMARY KEY, conversation_id TEXT,"
                " conversation_short_id TEXT, conversation_type INTEGER, type INTEGER,"
                " sub_type INTEGER, created_time INTEGER, sender TEXT, deleted INTEGER,"
                " status INTEGER, content TEXT)")
            connection.execute(
                "INSERT INTO msg VALUES ('u1', 'c1', 's1', 1, 1, 0, 1700000000000, '1', 0, 0, 'hello')")
            connection.commit()
        finally:
            connection.close()
        record = {"kind": "voice", "status": "ok", "resource_id": "r1",
                  "path": f"{uid}/attachments/voice/clip.amr", "bytes": 11,
                  "sha256": sha256_bytes(b"voice-bytes"), "duration": 1.0, "message_count": 1, "frames": 1}
        write_json(output / "media_manifest.json",
                   {"resources": [dict(record)], "messages": {f"{uid}:u1": dict(record)}, "statistics": {}})
        write_json(output / "decoration_manifest.json", {"bubbles": {}, "inline_emoji": {}})
        write_json(output / "export_summary.json", {"accounts": [{"uid": uid}]})
        return output, uid

    def test_index_adds_only_export_objects_and_preserves_msg(self) -> None:
        output, uid = self._build_export()
        config = self.config(self.tmp / "src", output)
        report = media_index.index_media(config)
        account = report["accounts"][0]
        self.assertTrue(account["msg_rows_unchanged"])
        self.assertTrue(account["index_matches_manifest"])
        self.assertEqual(account["integrity_check"], ["ok"])
        self.assertTrue(all(name.startswith("export_") for name in account["added_tables"]))
        self.assertEqual(account["indexed_attachment_rows"], 1)
        self.assertTrue((output / "media_index_results.json").is_file())

        connection = sqlite3.connect(str(output / uid / "chat.sqlite"))
        try:
            self.assertEqual(connection.execute("SELECT content FROM msg").fetchall(), [("hello",)])
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            views = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='view'")}
        finally:
            connection.close()
        self.assertTrue({"export_local_attachment", "export_bubble_resource"} <= tables)
        self.assertIn("export_chat_with_media", views)

        second = media_index.index_media(config)["accounts"][0]
        self.assertEqual(second["added_tables"], [])
        self.assertEqual(second["indexed_attachment_rows"], 1)

    def test_local_path_rejects_escapes_and_non_ascii(self) -> None:
        output, _ = self._build_export()
        with self.assertRaises(ExportError):
            media_index._local_path("../outside.bin", output)
        with self.assertRaises(ExportError):
            media_index._local_path("conversations/外.html", output)
        self.assertTrue(media_index._local_path("111111/chat.sqlite", output).is_file())


class ConnectionClosureTests(RuntimeCase):
    def test_open_database_closes_connection_after_the_block(self) -> None:
        source = self.tmp / "plain.db"
        connection = sqlite3.connect(str(source))
        try:
            connection.execute("CREATE TABLE msg (msg_uuid TEXT)")
            connection.commit()
        finally:
            connection.close()
        with open_database(source, "111111", self.tmp / "work") as opened:
            self.assertEqual(opened.execute("SELECT count(*) FROM msg").fetchone(), (0,))
        with self.assertRaises(sqlite3.ProgrammingError):
            opened.execute("SELECT count(*) FROM msg")


class CliErrorTests(RuntimeCase):
    def run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_inspect_missing_source_returns_nonzero_and_clean_message(self) -> None:
        code, _, stderr = self.run_main(["inspect", str(self.tmp / "missing")])
        self.assertEqual(code, 1)
        self.assertIn("Error:", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_json_errors_are_structured_on_stdout(self) -> None:
        code, stdout, _ = self.run_main(["inspect", str(self.tmp / "missing"), "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "error")
        self.assertIn("does not exist", payload["error"].lower())

    def test_json_preserves_unicode_on_legacy_windows_output(self) -> None:
        payload = {"original_text": "\ufff4\U0001f600\u4e2d\u6587"}
        for flags in ([], ["--json"]):
            with self.subTest(flags=flags):
                buffer = io.BytesIO()
                with io.TextIOWrapper(buffer, encoding="gbk", errors="strict") as stdout:
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        with patch.object(cli, "_dispatch", return_value=payload):
                            code = cli.main(["inspect", str(self.tmp)] + flags)
                    stdout.flush()
                    self.assertEqual(code, 0, stderr.getvalue())
                    self.assertEqual(json.loads(buffer.getvalue().decode("gbk")), payload)

    def test_json_errors_preserve_unicode_on_legacy_windows_output(self) -> None:
        message = "Missing resource \ufff4\U0001f600"
        buffer = io.BytesIO()
        with io.TextIOWrapper(buffer, encoding="gbk", errors="strict") as stdout:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                with patch.object(cli, "_dispatch", side_effect=ExportError(message)):
                    code = cli.main(["inspect", str(self.tmp), "--json"])
            stdout.flush()
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(buffer.getvalue().decode("gbk")),
                             {"status": "error", "error": message})

    def test_validate_missing_directory_returns_nonzero(self) -> None:
        code, _, stderr = self.run_main(["validate", str(self.tmp / "missing")])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", stderr)

    def test_corrupt_archive_is_a_clean_error_not_a_traceback(self) -> None:
        source = self.tmp / "source"
        source.mkdir()
        (source / "user.tar").write_bytes(b"this is not a tar archive" * 40)
        code, _, stderr = self.run_main(["export", str(source), "-o", str(self.tmp / "out")])
        self.assertEqual(code, 1)
        self.assertIn("Error:", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_output_inside_source_is_rejected_by_the_cli(self) -> None:
        source = make_source(self.tmp / "source")
        code, _, stderr = self.run_main(["export", str(source), "-o", str(source / "out")])
        self.assertEqual(code, 1)
        self.assertIn("outside the source", stderr.lower())

    def test_doctor_reports_dependencies_as_json(self) -> None:
        code, stdout, _ = self.run_main(["doctor", "--json"])
        self.assertIn(code, (0, 1))
        payload = json.loads(stdout)
        self.assertIn("core_ready", payload)
        self.assertIn("sqlcipher3-wheels", payload["dependencies"])


class CliConfigTests(RuntimeCase):
    def test_media_config_reuses_the_stored_work_directory(self) -> None:
        stored_work = self.tmp / "custom-work"
        state = {"work": str(stored_work), "accounts": ["111111"], "timezone": "UTC",
                 "page_size": 100, "source": str(self.tmp / "src")}
        args = cli.build_parser().parse_args(["media", str(self.tmp / "out")])
        config = cli._config(args, source=self.tmp / "src", state=state)
        self.assertEqual(config.work, stored_work.resolve())
        self.assertEqual(config.accounts, ("111111",))
        self.assertEqual(config.timezone, "UTC")
        self.assertEqual(config.page_size, 100)

    def test_explicit_work_dir_overrides_the_stored_one(self) -> None:
        explicit = self.tmp / "explicit-work"
        state = {"work": str(self.tmp / "custom-work"), "accounts": ()}
        args = cli.build_parser().parse_args(["media", str(self.tmp / "out"), "--work-dir", str(explicit)])
        config = cli._config(args, source=self.tmp / "src", state=state)
        self.assertEqual(config.work, explicit.resolve())

    def test_export_config_deduplicates_selected_accounts(self) -> None:
        args = cli.build_parser().parse_args(
            ["export", str(self.tmp / "src"), "-o", str(self.tmp / "out"),
             "--account", "111111", "--account", "111111"])
        config = cli._config(args)
        self.assertEqual(config.accounts, ("111111",))


if __name__ == "__main__":
    unittest.main()
