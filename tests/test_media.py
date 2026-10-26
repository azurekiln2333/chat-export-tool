"""Focused tests for ``duoshan_export.media``.

Owned by the media module (media.py / decoration parsing). This file is new and
does not touch ``tests/test_validation.py`` or ``tests/synthetic.py``: it only
imports the shared synthetic fixtures, so it is safe to keep alongside them.

Every test is offline. Downloads are either disabled or mocked, and the network
policy tests use fake HTTP responses, so no external host is ever contacted.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from tests import synthetic

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from duoshan_export import media  # noqa: E402
from duoshan_export.config import ExportConfig  # noqa: E402
from duoshan_export.errors import ExportError  # noqa: E402

ACCOUNT = "9000000000000001"


class MediaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="dsmedia-"))
        self.addCleanup(shutil.rmtree, self.temporary, True)

    # -- helpers
    def config(self, output: Path, **overrides) -> ExportConfig:
        values = dict(source=self.temporary / "source", output=output,
                      work=output / ".work", download=False)
        values.update(overrides)
        return ExportConfig(**values)

    def manifest(self, output: Path) -> dict:
        path = output / "media_manifest.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def decorations(self, output: Path) -> dict:
        return json.loads((output / "decoration_manifest.json").read_text(encoding="utf-8"))

    def voice_record(self, output: Path, resource_id: str) -> dict:
        """A manifest record for a real, verifiable voice file."""
        target = output / "attachments" / "voice" / (resource_id + ".m4a")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(synthetic.wav_bytes())
        return {
            "kind": "voice", "key": resource_id, "resource_id": resource_id,
            "message_count": 1, "status": "ok",
            "path": target.relative_to(output).as_posix(),
            "bytes": target.stat().st_size, "sha256": synthetic.sha256_file(target),
        }

    def write_inventory(self, output: Path, keys: list[str]) -> None:
        """A minimal exported account with one voice message per resource key."""
        folder = output / ACCOUNT
        folder.mkdir(parents=True, exist_ok=True)
        lines = []
        for index, key in enumerate(keys):
            row = synthetic.message_row(
                f"00000000-0000-4000-8000-00000000000{index}",
                msg_type=17,
                content={"duration": 1200,
                         "resource_url": {"uri": key,
                                          "url_list": ["https://example.invalid/" + key]}},
                sender=int(ACCOUNT), created=1757860962579 + index,
            )
            row["_export"] = {"account_uid": ACCOUNT,
                              "media": self.manifest(output).get("messages", {}).get(
                                  ACCOUNT + ":" + row["msg_uuid"], {})}
            lines.append(synthetic.dump(row))
        (folder / "messages.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -------------------------------------------------------------- case one
    def test_offline_rebuild_preserves_imported_media_and_decorations(self) -> None:
        """An offline rebuild keeps reused successes and imported decorations."""
        output = self.temporary / "DuoshanChatExport"
        synthetic.build_export(output)
        before_media = self.manifest(output)
        before_decorations = self.decorations(output)

        config = self.config(output)
        result = media.download_media(config)
        self.assertEqual(result["mode"], "offline")
        self.assertEqual(result["downloaded"], 0)
        self.assertGreaterEqual(result["reused"], 1)

        after_media = self.manifest(output)
        after_by_id = {item["resource_id"]: item for item in after_media["resources"]}
        reused = 0
        for record in before_media["resources"]:
            if record.get("status") != "ok":
                continue
            reused += 1
            self.assertIn(record["resource_id"], after_by_id)
            kept = after_by_id[record["resource_id"]]
            self.assertEqual(kept["status"], "ok")
            self.assertEqual(kept["path"], record["path"])
            self.assertTrue((output / kept["path"]).is_file())
        self.assertGreaterEqual(reused, 1)

        media.prepare_decorations(config, None)
        self.assertEqual(self.decorations(output), before_decorations)

    # -------------------------------------------------------------- case two
    def test_valid_ok_rejects_same_size_hash_tamper_and_unsafe_paths(self) -> None:
        """Full size match never bypasses the stored SHA256 or the path policy."""
        output = self.temporary / "DuoshanChatExport"
        output.mkdir(parents=True)
        record = self.voice_record(output, "voice_tamper")
        self.assertTrue(media._valid_ok(record, output))

        # Same byte count, wrong content hash.
        tampered = dict(record, sha256="0" * 64)
        self.assertFalse(media._valid_ok(tampered, output))

        # Same hash but a size that cannot match the file.
        self.assertFalse(media._valid_ok(dict(record, bytes=record["bytes"] + 1), output))

        # Nothing recorded means nothing can be verified.
        unverifiable = dict(record)
        unverifiable.pop("bytes")
        unverifiable.pop("sha256")
        self.assertFalse(media._valid_ok(unverifiable, output))

        # Paths that escape, or leave the export tree, are always rejected.
        payload = (output / record["path"]).read_bytes()
        outside = self.temporary / "outside.m4a"
        outside.write_bytes(payload)
        for unsafe in ("../outside.m4a", "../../outside.m4a", "/etc/passwd",
                       "C:/windows/outside.m4a", "attachments"):
            self.assertFalse(
                media._valid_ok({"status": "ok", "path": unsafe,
                                 "bytes": len(payload),
                                 "sha256": synthetic.sha256_file(outside)}, output),
                unsafe,
            )

    # ------------------------------------------------------------ case three
    def test_valid_journal_recovers_over_stale_failed_manifest(self) -> None:
        """A completed journal entry outranks a stale failed manifest record."""
        output = self.temporary / "DuoshanChatExport"
        output.mkdir(parents=True)
        resource_id = media._resource_id("voice", "voice-resume")
        record = self.voice_record(output, resource_id)
        self.write_inventory(output, ["voice-resume"])

        manifest = {
            "resources": [{"kind": "voice", "resource_id": resource_id,
                           "status": "failed", "errors": ["HTTP 403"]}],
            "messages": {},
            "statistics": {},
        }
        (output / "media_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        work = output / ".work"
        work.mkdir(parents=True)
        (work / media.JOURNAL_NAME).write_text(
            json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

        known = media._load_known(output, work)
        self.assertEqual(known[resource_id]["status"], "ok")

        # A full offline pass must therefore reuse the journal success.
        config = self.config(output)
        result = media.download_media(config)
        self.assertEqual(result["reused"], 1)
        kept = {item["resource_id"]: item for item in self.manifest(output)["resources"]}
        self.assertEqual(kept[resource_id]["status"], "ok")
        self.assertEqual(kept[resource_id]["path"], record["path"])

    # ------------------------------------------------------------- case four
    def test_incremental_journal_survives_interrupted_download(self) -> None:
        """Each finished download is flushed before a later failure aborts."""
        output = self.temporary / "DuoshanChatExport"
        output.mkdir(parents=True)
        self.write_inventory(output, ["voice-first", "voice-second"])
        config = self.config(output, download=True, workers=1)

        calls: list[str] = []

        def interrupted(_config, item, resource_id):
            calls.append(resource_id)
            if len(calls) == 1:
                return self.voice_record(Path(_config.output), resource_id)
            raise RuntimeError("simulated interruption")

        with mock.patch.object(media, "_download_item", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                media.download_media(config)

        self.assertEqual(len(calls), 2)
        journal = output / ".work" / media.JOURNAL_NAME
        self.assertTrue(journal.is_file())
        lines = [json.loads(line) for line in
                 journal.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["resource_id"], calls[0])
        self.assertEqual(lines[0]["status"], "ok")
        self.assertTrue(media._valid_ok(lines[0], output))

        # Resuming must not download the resource that already completed.
        resumed: list[str] = []

        def finish(_config, item, resource_id):
            resumed.append(resource_id)
            return self.voice_record(Path(_config.output), resource_id)

        with mock.patch.object(media, "_download_item", side_effect=finish):
            result = media.download_media(self.config(output, download=True, workers=1))

        self.assertNotIn(calls[0], resumed)
        self.assertIn(calls[1], resumed)
        self.assertEqual(result["reused"], 1)
        statuses = {item["resource_id"]: item["status"]
                    for item in self.manifest(output)["resources"]}
        self.assertEqual(statuses[calls[0]], "ok")
        self.assertEqual(statuses[calls[1]], "ok")

    # ------------------------------------------------------------- case five
    def test_https_redirect_and_host_policy(self) -> None:
        """Only allow-listed HTTPS hosts are fetched; every redirect is rechecked."""
        for url, expected in (
            ("https://p3-sign.douyinpic.com/obj/x", True),
            ("http://p3-sign.douyinpic.com/obj/x", False),
            ("https://example.invalid/x", False),
            ("https://user:pass@douyinpic.com/x", False),
            ("https://p3-sign.douyinpic.com:8443/x", False),
        ):
            self.assertEqual(media._allowed(url), expected, url)

        # A plain fetch upgrades http to https before any request is made.
        session = _FakeSession([_FakeResponse(payload=b"png")])
        with mock.patch.object(media, "_session", return_value=session):
            payload, actual, mime = media._fetch("http://p3-sign.douyinpic.com/obj/x")
        self.assertEqual(payload, b"png")
        self.assertTrue(actual.startswith("https://p3-sign.douyinpic.com/"))
        self.assertEqual(mime, "image/png")
        self.assertEqual(len(session.urls), 1)

        # A redirect to an unlisted host, or off HTTPS, is rejected untouched.
        for location in ("https://evil.example/obj/x",
                         "http://p3-sign.douyinpic.com/obj/x"):
            session = _FakeSession([_FakeResponse(redirect=location)])
            with mock.patch.object(media, "_session", return_value=session):
                with self.assertRaises(ExportError):
                    media._fetch("https://p3-sign.douyinpic.com/obj/start")
            self.assertEqual(len(session.urls), 1, location)

        # A redirect to another allow-listed host is followed and validated.
        session = _FakeSession([
            _FakeResponse(redirect="https://p3-sign.douyinpic.com/obj/final"),
            _FakeResponse(payload=b"data"),
        ])
        with mock.patch.object(media, "_session", return_value=session):
            payload, actual, _ = media._fetch("https://p3-sign.douyinpic.com/obj/start")
        self.assertEqual(payload, b"data")
        self.assertEqual(actual, "https://p3-sign.douyinpic.com/obj/final")
        self.assertEqual(len(session.urls), 2)

        # An endless redirect loop stops instead of spinning forever.
        loop = [_FakeResponse(redirect="https://p3-sign.douyinpic.com/obj/loop")
                for _ in range(media.MAX_REDIRECTS + 1)]
        session = _FakeSession(loop)
        with mock.patch.object(media, "_session", return_value=session):
            with self.assertRaises(ExportError):
                media._fetch("https://p3-sign.douyinpic.com/obj/start")


class _FakeResponse:
    """Minimal stand-in for a streamed ``requests`` response."""

    def __init__(self, *, redirect: str | None = None, payload: bytes = b"",
                 content_type: str = "image/png") -> None:
        self._redirect = redirect
        self._payload = payload
        self.headers = {"Content-Type": content_type}
        if redirect:
            self.headers["Location"] = redirect

    @property
    def is_redirect(self) -> bool:
        return self._redirect is not None

    def raise_for_status(self) -> None:  # pragma: no cover - always ok
        return None

    def iter_content(self, size: int):
        yield self._payload

    def close(self) -> None:
        return None


class _FakeSession:
    """Records requested URLs and replays a fixed queue of responses."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
