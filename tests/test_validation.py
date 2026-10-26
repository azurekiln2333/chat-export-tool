"""Tests for ``duoshan_export.validation``.

The fixtures are produced by ``tests.synthetic`` independently of the package
exporter and reader, so these tests check the validator against a realistic but
separate producer. Every test also tampers with a copy to prove that the
validator detects real breakage instead of always passing.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from tests import synthetic

ROOT = Path(__file__).resolve().parent.parent
import sys

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from duoshan_export import validation  # noqa: E402
from duoshan_export.errors import ExportError  # noqa: E402


# Media decoding needs ffmpeg for audio and Pillow for images.
def _decoders_available() -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    try:
        import PIL  # noqa: F401
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return False
    return True


requires_decoders = unittest.skipUnless(_decoders_available(),
                                        "ffmpeg on PATH and Pillow are required")


class ValidationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="dsv-"))
        self.addCleanup(shutil.rmtree, self.temporary, True)
        self.root = self.temporary / "DuoshanChatExport"

    # -- helpers
    def build(self, **kwargs) -> dict:
        return synthetic.build_export(self.root, **kwargs)

    def validate(self, **kwargs) -> dict:
        return validation.validate_export(self.root, **kwargs)

    def page(self, account: str) -> Path:
        return self.root / account / "conversations" / "conversation_001_page_001.html"

    def codes(self, report: dict) -> set[str]:
        return set(report["problem_counts"])

    # -- happy path
    def test_valid_export_passes_for_arbitrary_accounts(self) -> None:
        fixture = self.build(accounts=[synthetic.ACCOUNT_ME, synthetic.ACCOUNT_OTHER])
        report = self.validate()
        self.assertEqual(report["status"], "passed", report["problems"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["problem_count"], 0)
        self.assertEqual(len(report["accounts"]), 2)
        self.assertEqual(report["totals"]["messages"], 2 * len(fixture["rows"]))
        for account in report["accounts"]:
            self.assertTrue(account["raw_fields_match"])
            self.assertEqual(account["sqlite_integrity"], "ok")
            self.assertEqual(account["jsonl_rows"], account["sqlite_rows"])
            self.assertEqual(account["csv_rows"], account["jsonl_rows"])
            self.assertEqual(account["html_messages"], account["jsonl_rows"])
        self.assertTrue((self.root / "validation_results.json").is_file())
        self.assertTrue((self.root / "reader_media_validation_results.json").is_file())
        self.assertFalse((self.root / "media_validation_results.json").exists())

    def test_reports_are_written_and_reproducible(self) -> None:
        self.build()
        first = self.validate()
        second = self.validate()
        self.assertEqual(first["status"], second["status"])
        stored = json.loads((self.root / "validation_results.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], "passed")
        self.assertEqual(stored["totals"]["messages"], second["totals"]["messages"])

    def test_export_without_media_or_decorations_still_validates(self) -> None:
        self.build(with_media=False, with_decorations=False)
        report = self.validate()
        self.assertEqual(report["status"], "passed", report["problems"])
        self.assertFalse((self.root / "media_manifest.json").exists())
        self.assertTrue(any("media manifest" in note for note in report["notes"]))

    def test_two_independent_exports_do_not_share_state(self) -> None:
        first_root = self.temporary / "first"
        second_root = self.temporary / "second"
        synthetic.build_export(first_root, accounts=[synthetic.ACCOUNT_ME])
        synthetic.build_export(second_root, accounts=[synthetic.ACCOUNT_OTHER])

        # Break only the first export; the second must stay clean.
        self._tamper_row(first_root / synthetic.ACCOUNT_ME / "chat.sqlite",
                         "11111111-1111-4111-8111-111111111111", "tampered")
        first = validation.validate_export(first_root)
        second = validation.validate_export(second_root)
        self.assertEqual(first["status"], "failed")
        self.assertIn("raw_row_mismatch", self.codes(first))
        self.assertEqual(second["status"], "passed", second["problems"])
        self.assertEqual(second["accounts"][0]["uid"], synthetic.ACCOUNT_OTHER)

    # -- raw data equality
    def test_tampered_sqlite_row_is_detected(self) -> None:
        self.build()
        self._tamper_row(self.root / synthetic.ACCOUNT_ME / "chat.sqlite",
                         "22222222-2222-4222-8222-222222222222", "changed after export")
        report = self.validate()
        self.assertEqual(report["status"], "failed")
        self.assertIn("raw_row_mismatch", self.codes(report))
        self.assertFalse(report["accounts"][0]["raw_fields_match"])

    def test_extra_jsonl_row_is_detected(self) -> None:
        fixture = self.build()
        path = self.root / synthetic.ACCOUNT_ME / "messages.jsonl"
        row = dict(fixture["rows"]["11111111-1111-4111-8111-111111111111"])
        row["msg_uuid"] = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        row["_export"] = fixture["meta"]["11111111-1111-4111-8111-111111111111"]
        with path.open("a", encoding="utf-8") as handle:
            handle.write(synthetic.dump(row) + "\n")
        report = self.validate()
        self.assertEqual(report["status"], "failed")
        self.assertIn("raw_row_missing_in_sqlite", self.codes(report))
        self.assertIn("row_count_mismatch", self.codes(report))
        self.assertIn("csv_row_count_mismatch", self.codes(report))

    def test_missing_rendered_message_is_detected(self) -> None:
        fixture = self.build()
        page = self.page(synthetic.ACCOUNT_ME)
        text = page.read_text(encoding="utf-8")
        page.write_text(text.replace('id="msg-88888888-8888-4888-8888-888888888888"',
                                     'id="msg-replaced-anchor"'), encoding="utf-8")
        self.assertIn("88888888-8888-4888-8888-888888888888", fixture["rows"])
        report = self.validate()
        self.assertIn("page_unknown_message", self.codes(report))
        self.assertIn("message_not_rendered", self.codes(report))

    def test_unknown_message_type_is_preserved(self) -> None:
        fixture = self.build()
        row = fixture["rows"]["77777777-7777-4777-8777-777777777777"]
        self.assertEqual(row["type"], 9999)
        report = self.validate()
        self.assertEqual(report["status"], "passed", report["problems"])

    # -- CSV safety
    def test_csv_formula_and_long_id_protection_is_enforced(self) -> None:
        self.build(protect_csv=False)
        report = self.validate()
        self.assertEqual(report["status"], "failed")
        self.assertIn("csv_formula_injection", self.codes(report))
        self.assertIn("csv_long_id_unquoted", self.codes(report))

    def test_protected_csv_is_accepted(self) -> None:
        self.build(protect_csv=True)
        text = (self.root / synthetic.ACCOUNT_ME / "messages.csv").read_text(encoding="utf-8-sig")
        self.assertIn("'=SUM(A1:A2)", text)
        self.assertIn("'7549955327504959011", text)
        report = self.validate()
        self.assertEqual(report["status"], "passed", report["problems"])

    # -- HTML
    def test_html_text_is_escaped_and_offline(self) -> None:
        self.build()
        page = self.page(synthetic.ACCOUNT_ME).read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        report = self.validate()
        self.assertEqual(report["status"], "passed", report["problems"])

    def test_broken_local_link_is_detected(self) -> None:
        self.build()
        page = self.page(synthetic.ACCOUNT_ME)
        page.write_text(page.read_text(encoding="utf-8").replace(
            "../../attachments/stickers/sticker.png", "../../attachments/stickers/missing.png"),
            encoding="utf-8")
        report = self.validate()
        self.assertIn("local_link_missing", self.codes(report))
        self.assertEqual(report["status"], "failed")

    def test_remote_media_reference_is_detected(self) -> None:
        self.build()
        page = self.page(synthetic.ACCOUNT_ME)
        page.write_text(page.read_text(encoding="utf-8").replace(
            '<img class="sticker" src="../../attachments/stickers/sticker.png"',
            '<img class="sticker" src="https://example.invalid/sticker.png"'),
            encoding="utf-8")
        report = self.validate()
        self.assertIn("remote_media_reference", self.codes(report))

    def test_explicit_web_anchor_is_allowed(self) -> None:
        self.build()
        page = self.page(synthetic.ACCOUNT_ME)
        page.write_text(page.read_text(encoding="utf-8").replace(
            "</article>",
            '<details class="links"><a href="https://example.invalid/post" rel="noreferrer noopener">'
            "Link 1</a></details></article>"), encoding="utf-8")
        report = self.validate()
        self.assertEqual(report["status"], "passed", report["problems"])

    def test_link_escaping_the_export_tree_is_detected(self) -> None:
        self.build()
        page = self.page(synthetic.ACCOUNT_ME)
        page.write_text(page.read_text(encoding="utf-8").replace(
            "</main>", '<a href="../../../../etc/passwd">outside</a></main>'), encoding="utf-8")
        report = self.validate()
        self.assertIn("local_link_outside_export", self.codes(report))

    # -- media associations
    def test_missing_media_file_is_detected(self) -> None:
        self.build()
        (self.root / "attachments/stickers/sticker.png").unlink()
        report = self.validate()
        self.assertIn("media_file_missing", self.codes(report))
        self.assertEqual(report["status"], "failed")

    def test_unrendered_media_is_detected(self) -> None:
        self.build()
        page = self.page(synthetic.ACCOUNT_ME)
        text = page.read_text(encoding="utf-8")
        text = text.replace('src="../../attachments/voice/voice.wav"', 'src=""')
        page.write_text(text, encoding="utf-8")
        report = self.validate()
        self.assertIn("voice_not_playable", self.codes(report))

    def test_missing_url_media_is_not_a_failure(self) -> None:
        fixture = self.build()
        statuses = {resource.get("status") for resource in fixture["media_messages"].values()}
        self.assertIn("missing_url", statuses)
        report = self.validate()
        self.assertEqual(report["status"], "passed", report["problems"])

    def test_media_index_paths_are_checked(self) -> None:
        self.build()
        database = self.root / synthetic.ACCOUNT_ME / "chat.sqlite"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TABLE export_local_attachment (account_uid TEXT, msg_uuid TEXT,"
                " path_rel_export TEXT, exists_on_disk INTEGER)")
            connection.executemany(
                "INSERT INTO export_local_attachment VALUES (?,?,?,?)",
                [(synthetic.ACCOUNT_ME, "m1", "attachments/stickers/sticker.png", 1),
                 (synthetic.ACCOUNT_ME, "m2", "attachments/stickers/ghost.png", 0),
                 (synthetic.ACCOUNT_ME, "m3", "attachments/stickers/sticker.png", 0)])
            connection.commit()
        report = self.validate()
        self.assertIn("media_index_state_mismatch", self.codes(report))

    def test_media_index_unknown_path_is_detected(self) -> None:
        self.build()
        database = self.root / synthetic.ACCOUNT_ME / "chat.sqlite"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TABLE export_local_attachment (account_uid TEXT, msg_uuid TEXT,"
                " path_rel_export TEXT, exists_on_disk INTEGER)")
            connection.execute("INSERT INTO export_local_attachment VALUES (?,?,?,?)",
                               (synthetic.ACCOUNT_ME, "m1", "attachments/not-in-manifest.png", 1))
            connection.commit()
        (self.root / "attachments/not-in-manifest.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        report = self.validate()
        self.assertIn("media_index_unknown_path", self.codes(report))

    # -- summary / structure
    def test_unlisted_account_directory_is_detected(self) -> None:
        self.build()
        (self.root / "1234567890123456").mkdir()
        report = self.validate()
        self.assertIn("unlisted_account_directory", self.codes(report))

    def test_summary_total_mismatch_is_detected(self) -> None:
        self.build()
        path = self.root / "export_summary.json"
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary["total_message_count"] += 1
        path.write_text(json.dumps(summary), encoding="utf-8")
        report = self.validate()
        self.assertIn("summary_total_mismatch", self.codes(report))

    def test_extra_local_audio_does_not_inflate_voice_ok(self) -> None:
        """A legitimate local audio attachment must not be counted as a voice render.

        Regression: a real export had 469 manifest voice messages plus 2 local
        audio attachments. Counting every <audio> made the rendered voice_ok 471
        and produced a false reader_counter_mismatch.
        """
        fixture = self.build()
        extra = synthetic.add_extra_local_audio(
            self.root, synthetic.ACCOUNT_ME, "11111111-1111-4111-8111-111111111111")
        self.assertTrue((self.root / extra).is_file())

        report = self.validate()
        self.assertEqual(report["status"], "passed", report["problems"])
        self.assertEqual(report["reader"]["rendered_counters"]["voice_ok"], 1)
        self.assertEqual(report["reader"]["summary_counters"]["voice_ok"], 1)
        self.assertEqual(report["reader"]["audio_elements"], 2)

    def test_reader_counter_mismatch_is_detected(self) -> None:
        self.build()
        path = self.root / "export_summary.json"
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary["media_and_presentation"]["rendered"]["inline_emoji_occurrences"] += 5
        path.write_text(json.dumps(summary), encoding="utf-8")
        report = self.validate()
        self.assertIn("reader_counter_mismatch", self.codes(report))
        self.assertEqual(report["reader"]["status"], "failed")

    def test_missing_account_file_is_detected(self) -> None:
        self.build()
        (self.root / synthetic.ACCOUNT_ME / "contacts.csv").unlink()
        report = self.validate()
        self.assertIn("missing_account_file", self.codes(report))

    def test_broken_statistics_json_is_detected(self) -> None:
        self.build()
        (self.root / synthetic.ACCOUNT_ME / "statistics.json").write_text("{not json",
                                                                         encoding="utf-8")
        report = self.validate()
        self.assertIn("account_json_unreadable", self.codes(report))

    def test_reader_interface_language_does_not_affect_validation(self) -> None:
        """The validator checks structure, not UI text: a Chinese-UI page still passes.

        The default reader interface is Simplified Chinese; validation must never
        depend on a particular label language.
        """
        self.build()
        page = self.page(synthetic.ACCOUNT_ME)
        text = page.read_text(encoding="utf-8")
        text = text.replace('lang="en"', 'lang="zh-CN"')
        text = text.replace(">Conversations<", ">会话列表<").replace("Open voice file", "打开语音文件")
        page.write_text(text, encoding="utf-8")
        report = self.validate()
        self.assertEqual(report["status"], "passed", report["problems"])
        # The chat text itself is untouched by the interface language.
        self.assertIn("plain greeting", page.read_text(encoding="utf-8"))

    # -- hard failures
    def test_missing_output_directory_raises(self) -> None:
        with self.assertRaises(ExportError):
            validation.validate_export(self.temporary / "does-not-exist")

    def test_directory_without_summary_raises(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "index.html").write_text("<html></html>", encoding="utf-8")
        with self.assertRaises(ExportError):
            self.validate()

    def test_summary_without_accounts_raises(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "export_summary.json").write_text('{"app": "Duoshan"}', encoding="utf-8")
        with self.assertRaises(ExportError):
            self.validate()

    # -- tampering helper
    def _tamper_row(self, database: Path, uuid: str, value: str) -> None:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE msg SET content = ? WHERE msg_uuid = ?", (value, uuid))
            connection.commit()


# Media decoding (ffmpeg/Pillow) tests continue the case started above.
    @requires_decoders
    def test_decode_media_passes_and_writes_media_report(self) -> None:
        self.build(accounts=[synthetic.ACCOUNT_ME, synthetic.ACCOUNT_OTHER])
        report = self.validate(decode_media=True)
        self.assertEqual(report["status"], "passed", report["problems"])
        self.assertIsNotNone(report["media"])
        self.assertEqual(report["media"]["status"], "passed")
        stored = json.loads((self.root / "media_validation_results.json")
                            .read_text(encoding="utf-8"))
        self.assertGreaterEqual(stored["summary"]["voice_decoded"], 2)
        self.assertGreaterEqual(stored["summary"]["images_decoded"], 2)
        self.assertGreaterEqual(stored["summary"]["decorations_decoded"], 4)

    @requires_decoders
    def test_decode_media_detects_sha256_mismatch(self) -> None:
        self.build()
        path = self.root / "media_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["resources"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(manifest), encoding="utf-8")
        report = self.validate(decode_media=True)
        self.assertEqual(report["status"], "failed")
        self.assertIn("media_sha256_mismatch", self.codes(report))

    @requires_decoders
    def test_decode_media_detects_byte_size_mismatch(self) -> None:
        self.build()
        path = self.root / "media_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["resources"][0]["bytes"] = 1
        path.write_text(json.dumps(manifest), encoding="utf-8")
        report = self.validate(decode_media=True)
        self.assertIn("media_bytes_mismatch", self.codes(report))

    @requires_decoders
    def test_decode_media_detects_frame_count_mismatch(self) -> None:
        self.build()
        path = self.root / "media_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for resource in manifest["resources"]:
            if resource["kind"] == "emoji":
                resource["frames"] = 2  # the fixture PNG is a single frame
        path.write_text(json.dumps(manifest), encoding="utf-8")
        report = self.validate(decode_media=True)
        self.assertIn("image_frame_count_mismatch", self.codes(report))

    @requires_decoders
    def test_decode_media_detects_missing_decoration(self) -> None:
        self.build()
        (self.root / "attachments/inline_emoji/smile.png").unlink()
        report = self.validate(decode_media=True)
        self.assertIn("decoration_file_missing", self.codes(report))

    @requires_decoders
    def test_undecodable_audio_is_reported(self) -> None:
        self.build()
        voice = next((resource for resource in
                      json.loads((self.root / "media_manifest.json")
                                 .read_text(encoding="utf-8"))["resources"]
                      if resource["kind"] == "voice"), None)
        self.assertIsNotNone(voice)
        broken = b"not audio at all"
        (self.root / voice["path"]).write_bytes(broken)
        voice["bytes"] = len(broken)
        voice["sha256"] = synthetic.sha256_file(self.root / voice["path"])
        (self.root / "media_manifest.json").write_text(
            json.dumps({"resources": [voice], "messages": {}}, ensure_ascii=False),
            encoding="utf-8")
        report = self.validate(decode_media=True)
        self.assertIn("audio_decode_failed", self.codes(report))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
