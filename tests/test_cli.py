"""End-to-end tests: real SQLite source -> export tree -> CLI and validation.

The fixtures in ``tests.synthetic`` build a genuine Android-style app-data
folder, a DataBackup archive and SQLite databases that use the verified Duoshan
schema (including a real ``-wal`` sidecar), so these tests exercise the same code
paths a user's backup does instead of hand-written export files.

Two groups live here:

* ``PipelineTests`` drive the library entry points (prepare -> export ->
  rerender -> validate) and always run.
* ``CliTests`` call ``duoshan_export.cli.main`` exactly as a user would. The
  ``export``/``media`` commands need the media-download modules; those tests skip
  with a clear reason while another owner's modules are still unpublished, and
  start running automatically once they exist.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

from tests import synthetic

REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY / "src") not in sys.path:
    sys.path.insert(0, str(REPOSITORY / "src"))

from duoshan_export import paths  # noqa: E402
from duoshan_export.cli import main  # noqa: E402
from duoshan_export.config import ExportConfig  # noqa: E402
from duoshan_export.errors import ExportError  # noqa: E402
from duoshan_export.exporter import export_chats, rerender_export  # noqa: E402
from duoshan_export.runtime import STATE_FILE, begin_export, load_state  # noqa: E402
from duoshan_export.source import discover_accounts, extract_archive, prepare_source  # noqa: E402
from duoshan_export.validation import validate_export  # noqa: E402


def _media_pipeline_available() -> bool:
    """True once the agreed media API exists: download_media, prepare_decorations,
    index_media. The modules are published by another owner; until the contract is
    complete these CLI tests skip instead of reporting a false failure."""
    try:
        media = importlib.import_module("duoshan_export.media")
        index = importlib.import_module("duoshan_export.media_index")
    except Exception:  # ImportError, or a module still being written
        return False
    return (callable(getattr(media, "download_media", None))
            and callable(getattr(media, "prepare_decorations", None))
            and callable(getattr(index, "index_media", None)))


requires_media = unittest.skipUnless(
    _media_pipeline_available(),
    "waiting for media.download_media/prepare_decorations and media_index.index_media; "
    "the CLI export tests resume automatically once that API lands",
)


class CaseBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="dscli-"))
        self.addCleanup(shutil.rmtree, self.temporary, True)

    # -- helpers
    def source(self, name: str = "source") -> Path:
        return self.temporary / f"src-{name}"

    def output(self, name: str = "export") -> Path:
        return self.temporary / f"out-{name}"

    def config(self, source: Path | None = None, output: Path | None = None, **kwargs) -> ExportConfig:
        source = Path(source or self.source())
        output = Path(output or self.output())
        kwargs.setdefault("work", output / ".work")
        return ExportConfig(source=source, output=output, **kwargs)

    def run_pipeline(self, config: ExportConfig) -> tuple[dict, dict]:
        begin_export(config)
        layout = prepare_source(config.source, config.work)
        summary = export_chats(config, layout)
        rerender_export(config)
        report = validate_export(config.output)
        return summary, report

    def jsonl_rows(self, output: Path, uid: str) -> list[dict]:
        path = output / uid / paths.ACCOUNT_FILES["messages_jsonl"]
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def cli(self, arguments: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(arguments)
        return code, out.getvalue(), err.getvalue()


class PipelineTests(CaseBase):
    def test_export_from_real_schema_validates_with_english_names(self) -> None:
        synthetic.build_app_source(self.source())
        summary, report = self.run_pipeline(self.config())

        self.assertEqual(report["status"], "passed", report["problems"])
        self.assertEqual(summary["total_message_count"], len(synthetic.base_rows(synthetic.ACCOUNT_ME)))
        self.assertEqual(report["totals"]["messages"], summary["total_message_count"])

        output = self.output()
        for name in ("index.html", "reader.css", paths.ROOT_FILES["summary"],
                     paths.ROOT_FILES["readme"]):
            self.assertTrue((output / name).is_file(), name)
        account = output / synthetic.ACCOUNT_ME
        for name in paths.ACCOUNT_FILES.values():
            self.assertTrue((account / name).is_file(), name)
        self.assertTrue((account / paths.DIRECTORIES["pages"]).is_dir())
        self.assertTrue((account / paths.DIRECTORIES["transcripts"]).is_dir())

        # No legacy or non-ASCII generated names anywhere in the export tree.
        legacy = set(paths.LEGACY_NAMES)
        for item in output.rglob("*"):
            if ".work" in item.parts:
                continue
            self.assertTrue(item.name.isascii(), item)
            self.assertNotIn(item.name, legacy, item)

    def test_reader_interface_is_simplified_chinese_and_names_stay_english(self) -> None:
        """The boundary: Chinese HTML interface, English generated names."""
        synthetic.build_app_source(self.source())
        self.run_pipeline(self.config())
        output = self.output()

        index = (output / "index.html").read_text(encoding="utf-8")
        self.assertIn('lang="zh-CN"', index)
        self.assertNotIn('lang="en"', index)
        self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in index),
                        "index.html has no Chinese interface text")

        page = next((output / synthetic.ACCOUNT_ME / paths.DIRECTORIES["pages"]).glob("*.html"))
        html = page.read_text(encoding="utf-8")
        self.assertIn('lang="zh-CN"', html)
        self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in html))
        # Original chat text is copied verbatim, not translated.
        self.assertIn("plain greeting", html)

        # Every generated directory and file name is still English/ASCII.
        for item in output.rglob("*"):
            if ".work" in item.parts:
                continue
            self.assertTrue(item.name.isascii(), item)

    def test_pages_use_the_canonical_english_page_names(self) -> None:
        synthetic.build_app_source(self.source())
        self.run_pipeline(self.config())
        pages = sorted(item.name for item in
                       (self.output() / synthetic.ACCOUNT_ME / paths.DIRECTORIES["pages"]).glob("*.html"))
        self.assertEqual(pages, ["conversation_001_page_001.html"])

    def test_raw_rows_and_special_text_survive_the_export(self) -> None:
        synthetic.build_app_source(self.source())
        self.run_pipeline(self.config())
        output = self.output()

        rows = {row["msg_uuid"]: row for row in self.jsonl_rows(output, synthetic.ACCOUNT_ME)}
        with contextlib.closing(sqlite3.connect(
                self.source() / "databases" / f"encrypted_{synthetic.ACCOUNT_ME}_im.db")) as db:
            db.row_factory = sqlite3.Row
            source_rows = {row["msg_uuid"]: dict(row) for row in db.execute("SELECT * FROM msg")}
        self.assertEqual(set(rows), set(source_rows))
        for uuid, source_row in source_rows.items():
            for column, value in source_row.items():
                self.assertEqual(rows[uuid][column], value, f"{uuid}.{column}")
            self.assertIn("_export", rows[uuid])

        page = next((output / synthetic.ACCOUNT_ME / paths.DIRECTORIES["pages"]).glob("*.html"))
        html = page.read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)

        csv_text = (output / synthetic.ACCOUNT_ME / paths.ACCOUNT_FILES["messages_csv"]).read_text(encoding="utf-8-sig")
        self.assertIn("'=SUM(A1:A2)", csv_text)
        self.assertIn("'7549955327504959011", csv_text)

    def test_arbitrary_account_ids_are_used_verbatim(self) -> None:
        uid = "8600000000000077"
        synthetic.build_app_source(self.source(), [uid])
        _, report = self.run_pipeline(self.config(accounts=(uid,)))
        self.assertEqual(report["status"], "passed", report["problems"])
        self.assertTrue((self.output() / uid / paths.ACCOUNT_FILES["database"]).is_file())
        self.assertEqual([item["uid"] for item in report["accounts"]], [uid])

    def test_two_sequential_exports_stay_independent(self) -> None:
        first_rows = synthetic.base_rows(synthetic.ACCOUNT_ME)
        second_rows = synthetic.base_rows(synthetic.ACCOUNT_OTHER)
        second_rows[0] = dict(second_rows[0], content=json.dumps({"text": "second backup only"}))
        synthetic.build_app_source(self.source("first"), [synthetic.ACCOUNT_ME])
        synthetic.build_app_source(self.source("second"), [synthetic.ACCOUNT_OTHER],
                                   rows_by_uid={synthetic.ACCOUNT_OTHER: second_rows})

        first_summary, first_report = self.run_pipeline(
            self.config(self.source("first"), self.output("first")))
        second_summary, second_report = self.run_pipeline(
            self.config(self.source("second"), self.output("second")))

        self.assertEqual(first_report["status"], "passed", first_report["problems"])
        self.assertEqual(second_report["status"], "passed", second_report["problems"])
        self.assertEqual([item["uid"] for item in first_report["accounts"]], [synthetic.ACCOUNT_ME])
        self.assertEqual([item["uid"] for item in second_report["accounts"]], [synthetic.ACCOUNT_OTHER])
        self.assertFalse((self.output("first") / synthetic.ACCOUNT_OTHER).exists())
        self.assertFalse((self.output("second") / synthetic.ACCOUNT_ME).exists())
        self.assertIn("second backup only",
                      (self.output("second") / synthetic.ACCOUNT_OTHER / paths.ACCOUNT_FILES["messages_jsonl"])
                      .read_text(encoding="utf-8"))
        self.assertNotEqual(first_summary["total_message_count"], 0)
        self.assertGreaterEqual(second_summary["total_message_count"], 1)

    def test_resume_reuses_the_same_output_only_for_the_same_source(self) -> None:
        synthetic.build_app_source(self.source())
        first = self.config()
        self.run_pipeline(first)
        state = load_state(self.output())
        self.assertEqual(state["status"], "in_progress")

        # Same source and same account selection: resume is allowed.
        again = self.config(resume=True)
        begin_export(again)
        layout = prepare_source(again.source, again.work)
        export_chats(again, layout)
        self.assertEqual(validate_export(again.output)["status"], "passed")

        # A different account selection must not silently rewrite the export.
        with self.assertRaises(ExportError):
            begin_export(self.config(resume=True, accounts=(synthetic.ACCOUNT_OTHER,)))

    def test_resume_rejects_a_changed_source(self) -> None:
        synthetic.build_app_source(self.source())
        self.run_pipeline(self.config())
        database = self.source() / "databases" / f"encrypted_{synthetic.ACCOUNT_ME}_im.db"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE msg SET content = ? WHERE type = 7",
                               (json.dumps({"text": "edited after the export"}),))
            connection.commit()
        with self.assertRaises(ExportError) as caught:
            begin_export(self.config(resume=True))
        self.assertIn("changed", str(caught.exception).lower())

    def test_non_empty_output_requires_resume(self) -> None:
        synthetic.build_app_source(self.source())
        occupied = self.output()
        occupied.mkdir(parents=True)
        (occupied / "stray.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(ExportError):
            begin_export(self.config())

    def test_output_inside_the_source_is_rejected(self) -> None:
        synthetic.build_app_source(self.source())
        with self.assertRaises(ExportError):
            begin_export(self.config(source=self.source(),
                                     output=self.source() / "inside"))

    def test_account_filter_exports_only_the_requested_account(self) -> None:
        synthetic.build_app_source(self.source(), [synthetic.ACCOUNT_ME, synthetic.ACCOUNT_OTHER])
        summary, report = self.run_pipeline(self.config(accounts=(synthetic.ACCOUNT_OTHER,)))
        self.assertEqual(report["status"], "passed", report["problems"])
        self.assertEqual(summary["accounts"], [synthetic.ACCOUNT_OTHER])
        self.assertTrue((self.output() / synthetic.ACCOUNT_OTHER).is_dir())
        self.assertFalse((self.output() / synthetic.ACCOUNT_ME).exists())
        stored = json.loads((self.output() / paths.ROOT_FILES["summary"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["account_count"], 1)

    def test_unknown_account_filter_is_an_actionable_error(self) -> None:
        synthetic.build_app_source(self.source())
        with self.assertRaises(ExportError) as caught:
            self.run_pipeline(self.config(accounts=("1234567890123456",)))
        self.assertIn("1234567890123456", str(caught.exception))

    def test_rows_committed_only_in_the_wal_are_exported(self) -> None:
        synthetic.build_app_source(self.source(), wal=True)
        wal = self.source() / "databases" / f"encrypted_{synthetic.ACCOUNT_ME}_im.db-wal"
        self.assertTrue(wal.is_file())
        self.assertGreater(wal.stat().st_size, 0)

        summary, report = self.run_pipeline(self.config(accounts=(synthetic.ACCOUNT_ME,)))
        self.assertEqual(report["status"], "passed", report["problems"])
        self.assertIn("committed only in the write-ahead log",
                      (self.output() / synthetic.ACCOUNT_ME / paths.ACCOUNT_FILES["messages_jsonl"])
                      .read_text(encoding="utf-8"))
        with contextlib.closing(sqlite3.connect(
                self.output() / synthetic.ACCOUNT_ME / paths.ACCOUNT_FILES["database"])) as db:
            self.assertGreaterEqual(db.execute("SELECT count(*) FROM msg").fetchone()[0],
                                    len(synthetic.base_rows(synthetic.ACCOUNT_ME)) + 1)
        self.assertEqual(summary["total_message_count"],
                         len(synthetic.base_rows(synthetic.ACCOUNT_ME)) + 1)

    def test_archive_traversal_members_are_skipped(self) -> None:
        archive = synthetic.build_backup_archive(
            self.temporary / "backup.tar.gz", [synthetic.ACCOUNT_ME],
            members=synthetic.UNSAFE_MEMBERS)
        destination = self.temporary / "extracted"
        stats = extract_archive(archive, destination)

        self.assertTrue(any("escaped.db" in name or "windows.db" in name for name in stats["skipped"]),
                        stats["skipped"])
        for escaped in self.temporary.rglob("*.db"):
            self.assertTrue(escaped.resolve().is_relative_to(destination.resolve())
                            or escaped.resolve().is_relative_to(self.output().resolve())
                            or escaped.resolve().is_relative_to((self.output() / ".work").resolve())
                            or "src-" in str(escaped), escaped)
        self.assertFalse((self.temporary / "escaped.db").exists())
        self.assertFalse((self.temporary / "windows.db").exists())
        self.assertFalse((self.temporary / "absolute").exists())

        # The legitimate databases inside the archive still extract and export.
        _, report = self.run_pipeline(self.config(source=self.temporary / "backup.tar.gz"))
        self.assertEqual(report["status"], "passed", report["problems"])
        self.assertEqual([item["uid"] for item in report["accounts"]], [synthetic.ACCOUNT_ME])

    def test_single_database_file_source(self) -> None:
        app = synthetic.build_app_source(self.source())
        database = app / "databases" / f"encrypted_{synthetic.ACCOUNT_ME}_im.db"
        _, report = self.run_pipeline(self.config(source=database))
        self.assertEqual(report["status"], "passed", report["problems"])
        self.assertEqual([item["uid"] for item in report["accounts"]], [synthetic.ACCOUNT_ME])
        # The sibling contact cache next to the selected database is discovered too.
        stored = json.loads((self.output() / paths.ROOT_FILES["summary"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["accounts"][0]["nickname"], "Me")

    def test_discovery_reports_contacts_for_each_account(self) -> None:
        synthetic.build_app_source(self.source(), [synthetic.ACCOUNT_ME, synthetic.ACCOUNT_OTHER])
        layout = prepare_source(self.source(), self.temporary / "work")
        accounts = discover_accounts(layout)
        self.assertEqual([account.uid for account in accounts],
                         sorted([synthetic.ACCOUNT_ME, synthetic.ACCOUNT_OTHER]))
        for account in accounts:
            self.assertIsNotNone(account.contacts_database)
            self.assertIn(account.uid, account.database.name)

    def test_export_state_is_written_and_resumable(self) -> None:
        synthetic.build_app_source(self.source())
        self.run_pipeline(self.config())
        state = load_state(self.output())
        self.assertEqual(state["format_version"], 1)
        self.assertEqual(state["accounts"], [])
        self.assertTrue((self.output() / STATE_FILE).is_file())


class CliTests(CaseBase):
    def test_version_is_reported(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self.cli(["--version"])
        self.assertEqual(caught.exception.code, 0)

    def test_inspect_reports_every_account_without_writing_the_source(self) -> None:
        synthetic.build_app_source(self.source(), [synthetic.ACCOUNT_ME, synthetic.ACCOUNT_OTHER])
        before = sorted(item.name for item in (self.source() / "databases").iterdir())
        code, out, _ = self.cli(["inspect", str(self.source()), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([row["uid"] for row in payload["accounts"]],
                         sorted([synthetic.ACCOUNT_ME, synthetic.ACCOUNT_OTHER]))
        self.assertTrue(all(row["message_count"] > 0 for row in payload["accounts"]))
        self.assertEqual(sorted(item.name for item in (self.source() / "databases").iterdir()), before)

    def test_inspect_rejects_a_work_dir_inside_the_source(self) -> None:
        synthetic.build_app_source(self.source())
        code, _, err = self.cli(["inspect", str(self.source()), "--work-dir",
                                 str(self.source() / "work")])
        self.assertEqual(code, 1)
        self.assertIn("separate", err)

    def test_validate_command_accepts_a_clean_export(self) -> None:
        synthetic.build_export(self.output())
        code, out, _ = self.cli(["validate", str(self.output()), "--json"])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["status"], "passed")

    def test_validate_command_fails_on_a_tampered_export(self) -> None:
        synthetic.build_export(self.output())
        (self.output() / synthetic.ACCOUNT_ME / paths.ACCOUNT_FILES["contacts_csv"]).unlink()
        code, _, err = self.cli(["validate", str(self.output())])
        self.assertEqual(code, 1)
        self.assertIn("validation failed", err.lower())

    def test_validate_command_rejects_a_missing_directory(self) -> None:
        code, _, err = self.cli(["validate", str(self.temporary / "absent")])
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)

    def test_doctor_reports_json_for_every_dependency(self) -> None:
        code, out, _ = self.cli(["doctor", "--json"])
        payload = json.loads(out)
        self.assertIn(code, (0, 1))
        self.assertIn("core_ready", payload)
        for name in ("Pillow", "requests", "zstandard", "tzdata"):
            self.assertIn(name, payload["dependencies"])
            self.assertIn("available", payload["dependencies"][name])
        self.assertIn("ffmpeg", payload["programs"])

    def test_unknown_command_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self.cli(["reticulate"])
        self.assertNotEqual(caught.exception.code, 0)

    @requires_media
    def test_export_command_end_to_end(self) -> None:
        synthetic.build_app_source(self.source())
        code, out, err = self.cli(["export", str(self.source()), "-o", str(self.output())])
        self.assertEqual(code, 0, err)
        self.assertTrue((self.output() / "index.html").is_file())
        stored = json.loads((self.output() / paths.ROOT_FILES["summary"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["account_count"], 1)
        self.assertTrue((self.output() / paths.ROOT_FILES["data_validation"]).is_file())

    @requires_media
    def test_export_command_honours_account_filter_and_english_output(self) -> None:
        synthetic.build_app_source(self.source(), [synthetic.ACCOUNT_ME, synthetic.ACCOUNT_OTHER])
        code, _, err = self.cli(["export", str(self.source()), "-o", str(self.output()),
                                 "--account", synthetic.ACCOUNT_OTHER])
        self.assertEqual(code, 0, err)
        self.assertFalse((self.output() / synthetic.ACCOUNT_ME).exists())
        self.assertTrue((self.output() / synthetic.ACCOUNT_OTHER).is_dir())

    @requires_media
    def test_export_command_then_resume(self) -> None:
        synthetic.build_app_source(self.source())
        code, _, err = self.cli(["export", str(self.source()), "-o", str(self.output())])
        self.assertEqual(code, 0, err)
        code, _, err = self.cli(["export", str(self.source()), "-o", str(self.output()), "--resume"])
        self.assertEqual(code, 0, err)
        code, _, err = self.cli(["export", str(self.source()), "-o", str(self.output())])
        self.assertEqual(code, 1)
        self.assertIn("resume", err.lower())

    @requires_media
    def test_media_command_refreshes_an_existing_export(self) -> None:
        synthetic.build_app_source(self.source())
        code, _, err = self.cli(["export", str(self.source()), "-o", str(self.output())])
        self.assertEqual(code, 0, err)
        code, _, err = self.cli(["media", str(self.output())])
        self.assertEqual(code, 0, err)
        self.assertTrue((self.output() / "index.html").is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
