"""Synthetic library media retrieval; every HTTP request is intercepted."""
from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from duoshan_export import library_media as media
from duoshan_export.errors import ExportError
from duoshan_export.library import import_sources
from tests.synthetic import png_bytes, wav_bytes

ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, data=b"", status=200, headers=None, chunks=None):
        self.data, self.status_code = data, status
        self.headers = headers or {"Content-Length": str(len(data))}
        self.chunks = chunks
        self.closed = False
    def iter_content(self, size):
        yield from self.chunks if self.chunks is not None else [self.data]
    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.lock = threading.Lock()
    def get(self, url, **options):
        with self.lock:
            self.calls.append((url, options))
            if not self.responses:
                raise AssertionError("Unexpected network request")
            return self.responses.pop(0)


class LibraryMediaTests(unittest.TestCase):
    def test_download_reports_numeric_validation_download_and_patch_stages(self):
        from duoshan_export.progress import ProgressEvent
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": ["https://p.douyinpic.com/progress"]}}},
                             {"type": 30, "content": {}}])
        events = []
        with patch.object(media, "_session", return_value=Session([Response(png_bytes())])):
            result = media.download_library_media(self.library, self.work / "media", workers=1,
                download_media=True, progress=events.append)
        self.assertEqual(result["totals"], dict(downloaded=1, existing=0, failed=0, unsupported=1))
        structured = [event for event in events if isinstance(event, ProgressEvent)]
        scanning = [event for event in structured if event.stage == "扫描资料库媒体"]
        self.assertIsNone(scanning[0].total)
        self.assertEqual((scanning[1].completed, scanning[1].total), (0, 2))
        self.assertEqual((scanning[-1].completed, scanning[-1].total, scanning[-1].state), (2, 2, "completed"))
        self.assertTrue(all(event.unit == "条" for event in scanning))
        validation = [event for event in structured if event.stage == "校验已有媒体"]
        self.assertEqual((validation[0].completed, validation[0].total, validation[0].state), (0, 2, "running"))
        self.assertEqual((validation[-1].completed, validation[-1].total, validation[-1].state), (2, 2, "completed"))
        download = [event for event in structured if event.stage == "补全在线媒体"]
        self.assertEqual((download[0].completed, download[0].total), (1, 2))
        self.assertEqual((download[-1].completed, download[-1].total, download[-1].state), (2, 2, "completed"))
        self.assertEqual(structured[-1].stage, "写入媒体索引")
        self.assertEqual(structured[-1].state, "completed")
        self.assertTrue(any(str(event).startswith("[✓]https://") for event in events))

    def test_inventory_counts_only_requested_accounts_in_same_snapshot(self):
        from duoshan_export.progress import ProgressEvent
        wanted = self.add("duoshan", [{"type": 7, "content": "text"}, {"type": 30, "content": {}}])
        self.add("xhs", [{"content_type": 1, "content": "another account"}])
        events = []
        inventory = media._inventory(self.library, [wanted], events.append, None)
        self.assertEqual(len(inventory), 1)
        self.assertTrue(all(isinstance(event, ProgressEvent) for event in events))
        self.assertEqual((events[-1].completed, events[-1].total), (2, 2))
        self.assertEqual(events[-1].state, "completed")

    def test_patch_reports_heartbeat_without_fabricated_counts_and_preserves_errors(self):
        from duoshan_export import progress as progress_module
        for updates in (None, self.root / "updates.jsonl"):
            events = []
            heartbeat = threading.Event()
            stage = "恢复媒体索引" if updates is None else "写入媒体索引"

            def report(event):
                events.append(event)
                if len(events) >= 2:
                    heartbeat.set()

            def run(*args, **kwargs):
                self.assertTrue(heartbeat.wait(2), "Long patch did not report a heartbeat")
                return subprocess.CompletedProcess(args[0], 0, '{"status":"patched"}', '')

            with patch.object(progress_module, "PROGRESS_INTERVAL", 0.01), patch.object(media.subprocess, "run", side_effect=run):
                self.assertEqual(media._patch(self.library, updates, "node", report), {"status": "patched"})
            self.assertGreaterEqual(len(events), 3)
            self.assertTrue(all(event.stage == stage and event.total is None and event.completed is None for event in events))
            self.assertEqual(events[0].state, "running")
            self.assertEqual(events[-1].state, "completed")
        failed = []
        with patch.object(media.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, '', 'fixture patch failure')):
            with self.assertRaisesRegex(ExportError, "fixture patch failure"):
                media._patch(self.library, None, "node", failed.append)
        self.assertEqual(failed[-1].state, "failed")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="library-media-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library, self.work = self.root / "library", self.root / "work"

    def add(self, app, rows, uid=None):
        uid = uid or {"duoshan": "910000000000001", "xhs": "a" * 24, "wechat": "b" * 32}[app]
        folder = self.root / ("export-" + app + "-" + str(len(list(self.root.iterdir()))))
        account = folder / uid
        account.mkdir(parents=True)
        (folder / "export_summary.json").write_text(json.dumps({"app": {"duoshan": "Duoshan", "xhs": "Xiaohongshu", "wechat": "WeChat"}[app], "timezone": "Asia/Shanghai"}))
        (account / "statistics.json").write_text(json.dumps({"app": app, "nickname": "synthetic"}))
        (account / "messages.jsonl").write_text("".join(json.dumps({"msg_uuid": str(i), "conversation_id": "peer", "sender": uid,
            "created_time": 1700000000000 + i, "msg_server_id": 9007199254740997 + i,
            **row, "_export": {"app": app, "text": "synthetic", "time": "2023-11-15 06:13:20", "conversation_name": "fixture", "local_media": []}}) + "\n" for i, row in enumerate(rows)), encoding="utf-8")
        import_sources([folder], self.library, self.work / "import", progress=lambda _: None)
        return uid

    def run_download(self, session, **kwargs):
        progress = kwargs.pop("progress", lambda _: None)
        with patch.object(media, "_session", return_value=session):
            return media.download_library_media(self.library, self.work / "media", workers=1, progress=progress, download_media=True, **kwargs)

    def test_live_url_log_preserves_signed_queries_and_fallback_outcomes_without_replaying_cached_attempts(self):
        original = "http://p.douyinpic.com/original?signature=" + "a" * 3000 + "%2Bexact&x=<literal>"
        fallback = "https://p.douyinpic.com/fallback?signature=full%2Ffallback"
        unused = "https://p.douyinpic.com/unused?signature=never-requested"
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": [original, fallback, unused]}}}])
        logs = []
        result = self.run_download(Session([Response(status=403), Response(png_bytes())]), progress=logs.append)
        self.assertEqual(result["totals"]["downloaded"], 1)
        url_lines = [line for line in logs if line.startswith("[")]
        self.assertEqual(len(url_lines), 2)
        self.assertTrue(url_lines[0].startswith("[x]https://" + original[7:] + " | 失败："))
        self.assertIn("HTTP 403", url_lines[0])
        self.assertEqual(url_lines[1], "[✓]" + fallback + " | 下载成功")
        self.assertNotIn(unused, "\n".join(logs))
        reused_logs = []
        again = self.run_download(Session([]), progress=reused_logs.append)
        self.assertEqual(again["totals"]["existing"], 1)
        reused_lines = [line for line in reused_logs if line.startswith("[")]
        self.assertEqual(reused_lines, ["[=]" + url + " | 已有本地文件（记录候选，未发起新请求）" for url in (original, fallback, unused)])
        self.assertNotIn("HTTP 403", "\n".join(reused_logs))
        self.assertFalse(any(line.startswith("[✓]") for line in reused_lines))

    def test_live_url_log_marks_rejected_redirect_missing_url_and_sanitized_exception(self):
        source = "https://p.douyinpic.com/redirect?sig=source%2Bfull"
        destination = "https://unapproved.example.test/image?sig=redirect%2Ffull"
        unavailable = "https://p.douyinpic.com/error?sig=known-full"
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": [source]}}},
                             {"type": 30, "content": {}},
                             {"type": 27, "content": {"resource_url": {"url_list": [unavailable]}}}])
        logs = []
        session = Session([Response(status=302, headers={"Location": destination})])
        get = session.get
        def request(url, **options):
            if url == unavailable:
                raise OSError("secret-password from exception https://not-recorded.example.test/private")
            return get(url, **options)
        with patch.object(session, "get", side_effect=request):
            result = self.run_download(session, progress=logs.append)
        self.assertEqual(result["totals"], dict(downloaded=0, existing=0, failed=1, unsupported=2))
        self.assertIn("[→]" + source + " | HTTP 302 重定向至 " + destination, logs)
        denied = next(line for line in logs if line.startswith("[!]" + destination))
        self.assertIn("不支持", denied)
        self.assertIn("未发起请求", denied)
        self.assertTrue(any(line.startswith("[!]无 URL | 不支持：") and "视频下载网址" in line for line in logs))
        failed = next(line for line in logs if line.startswith("[x]" + unavailable))
        self.assertIn("OSError", failed)
        self.assertNotIn("secret-password", "\n".join(logs))
        self.assertNotIn("not-recorded.example.test", "\n".join(logs))

    def test_default_progress_with_strict_gbk_does_not_interrupt_download_or_change_signed_url(self):
        url = "https://p.douyinpic.com/photo?signature=full%2Bexact%2Fvalue&x=1"
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": [url]}}}])
        buffer = io.BytesIO()
        with io.TextIOWrapper(buffer, encoding="gbk", errors="strict") as stream:
            with redirect_stdout(stream), patch.object(media, "_session", return_value=Session([Response(png_bytes())])):
                result = media.download_library_media(self.library, self.work / "media", workers=1, download_media=True)
            stream.flush()
            text = buffer.getvalue().decode("gbk")
        self.assertEqual(result["totals"]["downloaded"], 1)
        self.assertEqual(result["patch_result"]["patched_messages"], 1)
        self.assertIn("[+]" + url + " | 下载成功", text)
        unicode_output = io.StringIO()
        with redirect_stdout(unicode_output):
            media._print_progress("[✓]" + url + " | 下载成功")
        self.assertEqual(unicode_output.getvalue(), "[✓]" + url + " | 下载成功\n")

    def test_custom_progress_encoding_exception_is_not_suppressed(self):
        self.add("duoshan", [{"type": 30, "content": {}}])
        error = UnicodeEncodeError("gbk", "✓", 0, 1, "synthetic callback error")
        def progress(_):
            raise error
        with self.assertRaises(UnicodeEncodeError) as raised:
            media.download_library_media(self.library, self.work / "media", progress=progress, download_media=True)
        self.assertIs(raised.exception, error)

    def test_app_specific_extraction_preserves_signed_queries_and_ignores_chat_links(self):
        ds = media._extract_resources("duoshan", {"type": 27, "content": json.dumps({"resource_url": {"origin_url_list": ["https://p.douyinpic.com/a?sig=exact"], "thumb_url_list": ["https://p.douyinpic.com/thumb"]}})}, {}, 27)
        self.assertEqual(ds[0]["urls"], ["https://p.douyinpic.com/a?sig=exact", "https://p.douyinpic.com/thumb"])
        xhs = media._extract_resources("xhs", {"content_type": 2, "content": json.dumps({"content": json.dumps({"link": "https://msg-img.xhscdn.com/small", "imageDataMap": {"IM_PRV": {"url": "https://msg-img.xhscdn.com/preview"}, "IM_ORG": {"url": "https://msg-img.xhscdn.com/original?token=keep"}}, "livePhotoVideo": {"videoLink": "https://msg-video.xhscdn.com/live"}})})}, {}, 2)
        self.assertEqual([item["kind"] for item in xhs], ["image", "video"])
        self.assertEqual(xhs[0]["urls"][0], "https://msg-img.xhscdn.com/original?token=keep")
        self.assertEqual(media._extract_resources("duoshan", {"type": 7, "content": "https://p.douyinpic.com/user-pasted-url"}, {}, 7), [])
        encrypted = media._extract_resources("wechat", {"_wechat_raw": {"type": 3, "content": '<msg><img aeskey="secret" cdnmidimgurl="https://m.qpic.cn/image"/></msg>'}}, {}, 3)
        self.assertEqual(encrypted[0]["reason"], "encrypted_wechat_resource")
        group = media._extract_resources("wechat", {"_wechat_raw": {"type": 47, "content": 'person:\n<msg><emoji cdnurl="https://emoji.qpic.cn/plain"/></msg>'}}, {}, 47)
        self.assertEqual(group[0]["urls"], ["https://emoji.qpic.cn/plain"])
        video = media._extract_resources("duoshan", {"type": 30, "content": {"poster": {"origin_url_list": ["https://p.byteimg.com/poster"]}}}, {}, 30)
        self.assertEqual([(item["kind"], item["reason"]) for item in video], [("video", "video_url_not_recorded"), ("image", None)])

    def test_offline_report_uses_latest_bounded_journal_and_current_inventory_without_mutation(self):
        url = "https://p.douyinpic.com/photo?sig=full%2Bvalue&x=<tag>"
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": [url]}}},
                             {"type": 30, "content": {}},
                             {"type": 27, "content": {"resource_url": {"url_list": ["https://p.douyinpic.com/pending"]}}}])
        inventory = media._inventory(self.library, None, lambda _: None, None)
        item = next(r for r in inventory.values() if url in r["urls"])
        media._journal(self.library, {"resource_id": item["resource_id"], "app": "duoshan", "kind": "image", "status": "unsupported", "reason": "unapproved_media_host"})
        media._journal(self.library, {"resource_id": item["resource_id"], "app": "duoshan", "kind": "image", "status": "failed", "reason": "expired_or_unavailable_http_403"})
        with (self.library / media.MANIFEST).open("ab") as stream:
            stream.write(b'{"resource_id":"torn')
        before = (self.library / "reader.sqlite").read_bytes()
        with patch.object(media, "_session", side_effect=AssertionError("report must stay offline")), \
             patch.object(media, "_patch", side_effect=AssertionError("report must not mutate reader")), \
             patch.object(media, "_library_lock", side_effect=AssertionError("report must not lock downloader")):
            result = media.inspect_library_media(self.library, active_download=True, progress=lambda _: None)
            again = media.inspect_library_media(self.library, progress=lambda _: None)
        self.assertEqual(before, (self.library / "reader.sqlite").read_bytes())
        self.assertEqual(result["data_status"], "active_snapshot")
        self.assertEqual(result["totals"], dict(ok=0, existing=0, failed=1, unsupported=1, pending=1, orphaned=0))
        self.assertTrue(all(not row["attempts_known"] for row in result["resources"]))
        row = next(r for r in result["resources"] if r["resource_id"] == item["resource_id"])
        self.assertEqual(row["recorded_urls"], [url])
        self.assertEqual(row["status"], "failed")
        self.assertIn("403", row["reason_text"])
        self.assertFalse(row["attempts_known"])
        self.assertEqual(row["attempted_urls"], [])
        self.assertTrue(row["messages"][0]["account_directory"].startswith("dy_"))
        self.assertTrue(result["journal_snapshot"]["torn_final_line"])
        self.assertEqual(result["journal_snapshot"]["parsed_records"], 2)
        self.assertNotEqual(result["report_json"], again["report_json"])
        self.assertIn(url, Path(result["report_text"]).read_text(encoding="utf-8"))
        html_report = Path(result["report_html"]).read_text(encoding="utf-8")
        self.assertNotIn("<tag>", html_report)
        self.assertIn("&lt;tag&gt;", html_report)
        self.assertNotIn('<a href="https:', html_report)
        self.assertEqual(json.loads(Path(result["report_json"]).read_text(encoding="utf-8"))["resources"], result["resources"])

    def test_recorded_and_attempted_urls_include_rejected_redirect_and_chinese_causes(self):
        urls = ["https://p.douyinpic.com/forbidden?sig=full%2Btoken&x=1", "https://unapproved.example.test/image?private=recorded",
                "https://p.douyinpic.com/redirect?sig=old", "https://p.douyinpic.com/bad-image?sig=bad"]
        redirect = "https://127.0.0.1/blocked?sig=redirect%2Btoken"
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": [url]}}} for url in urls] + [{"type": 30, "content": {}}])
        session = Session([Response(status=403), Response(status=302, headers={"Location": redirect}), Response(b"not an image")])
        result = self.run_download(session)
        self.assertEqual(result["totals"], dict(downloaded=0, existing=0, failed=2, unsupported=3))
        rows = {item["recorded_urls"][0]: item for item in result["failures"] if item["recorded_urls"]}
        self.assertEqual(rows[urls[0]]["attempted_urls"], [urls[0]])
        self.assertEqual(rows[urls[0]]["attempts"][0]["requests"][0]["http_status"], 403)
        self.assertIn("拒绝访问", rows[urls[0]]["reason_text"])
        self.assertEqual(rows[urls[1]]["attempted_urls"], [])
        self.assertFalse(rows[urls[1]]["attempts"][0]["requests"][0]["requested"])
        self.assertIn("不代表文件不存在", rows[urls[1]]["reason_text"])
        redirected = rows[urls[2]]
        self.assertEqual(redirected["attempted_urls"], [urls[2]])
        self.assertEqual(redirected["attempts"][0]["requests"][0]["redirect_to"], redirect)
        self.assertEqual(redirected["attempts"][0]["requests"][1]["url"], redirect)
        self.assertFalse(redirected["attempts"][0]["requests"][1]["requested"])
        self.assertEqual(rows[urls[3]]["reason"], "invalid_or_encrypted_image")
        self.assertEqual(rows[urls[3]]["attempts"][0]["requests"][0]["status"], "received")
        self.assertEqual(rows[urls[3]]["attempts"][0]["status"], "failed")
        missing = next(row for row in result["failures"] if not row["recorded_urls"])
        self.assertEqual(missing["reason"], "video_url_not_recorded")
        self.assertTrue(missing["attempts_known"])
        self.assertEqual(missing["attempts"], [])
        self.assertEqual(len(session.calls), 3)
        report = media.inspect_library_media(self.library, progress=lambda _: None)
        report_rows = {row["recorded_urls"][0]: row for row in report["resources"] if row["recorded_urls"]}
        self.assertEqual(report_rows[urls[2]]["attempts"], redirected["attempts"])
        self.assertIn(redirect, Path(report["report_text"]).read_text(encoding="utf-8"))

    def test_success_url_diagnostics_stay_out_of_reader_metadata_and_patch_payload(self):
        url = "https://p.douyinpic.com/photo?sig=private%2Ffull&expiry=123"
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": [url]}}}])
        result = self.run_download(Session([Response(png_bytes())]))
        self.assertEqual(result["totals"]["downloaded"], 1)
        record = next(iter(media._load_manifest(self.library).values()))
        self.assertEqual(record["recorded_urls"], [url])
        self.assertEqual(record["attempted_urls"], [url])
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            meta = json.loads(db.execute("SELECT meta_json FROM messages").fetchone()[0])
        self.assertLessEqual(set(meta["media"]), media.MEDIA_FIELDS)
        for field in ("attempts", "recorded_urls", "attempted_urls", "diagnostic", "reason_text", "messages"):
            self.assertNotIn(field, meta["media"])
        updates = (Path(result["work"]) / "media-updates.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(url, updates)
        self.assertNotIn("conversation_name", updates)
        self.assertNotIn("account_directory", updates)

    def test_report_does_not_reassociate_historical_or_deleted_message_references(self):
        uid = self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": ["https://p.douyinpic.com/photo"]}}}])
        self.run_download(Session([Response(png_bytes())]))
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            db.execute("DELETE FROM messages WHERE uid=?", (uid,)); db.commit()
        result = media.inspect_library_media(self.library, progress=lambda _: None)
        self.assertEqual(result["totals"]["orphaned"], 1)
        row = result["resources"][0]
        self.assertFalse(row["current"])
        self.assertEqual(row["messages"], [])
        self.assertEqual(row["historical_messages"][0]["uid"], uid)

    def test_manifest_snapshot_ignores_records_appended_after_open_boundary(self):
        self.add("duoshan", [{"type": 7, "content": "text"}])
        media._journal(self.library, {"resource_id": "first", "status": "failed"})
        appended = []
        def append_during_read():
            if not appended:
                appended.append(True)
                media._journal(self.library, {"resource_id": "later", "status": "ok"})
            return False
        records, snapshot = media._manifest_snapshot(self.library, append_during_read)
        self.assertEqual(set(records), {"first"})
        self.assertLess(snapshot["bytes"], (self.library / media.MANIFEST).stat().st_size)

    def test_report_can_run_while_library_mutation_lock_is_held(self):
        self.add("duoshan", [{"type": 30, "content": {}}])
        with media._library_lock(self.library):
            result = media.inspect_library_media(self.library, progress=lambda _: None, active_download=True)
        self.assertEqual(result["totals"]["unsupported"], 1)

    def test_cli_report_is_offline_and_returns_readable_report_paths(self):
        from duoshan_export.cli import main
        self.add("duoshan", [{"type": 30, "content": {}}])
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch.object(media, "_session", side_effect=AssertionError("offline")):
            code = main(["library-media-report", str(self.library), "--active-download", "--json"])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["data_status"], "active_snapshot")
        self.assertTrue(Path(result["report_html"]).is_file())
        self.assertEqual(result["resources"][0]["recorded_urls"], [])

    def test_cancelled_attempt_is_reported_without_claiming_it_never_requested(self):
        url = "https://p.douyinpic.com/cancel?sig=full"
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": [url]}}}])
        cancelled = [False]
        class CancellingResponse(Response):
            def iter_content(response, size):
                yield b"partial"
                cancelled[0] = True
                yield b"remaining"
        logs = []
        result = self.run_download(Session([CancellingResponse(headers={})]), cancel=lambda: cancelled[0], progress=logs.append)
        self.assertEqual(result["status"], "cancelled")
        report = media.inspect_library_media(self.library, progress=lambda _: None)
        row = report["resources"][0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["journal_status"], "cancelled")
        self.assertEqual(row["reason"], "cancelled")
        self.assertTrue(row["attempts_known"])
        self.assertEqual(row["attempted_urls"], [url])
        self.assertEqual(row["attempts"][0]["status"], "cancelled")
        self.assertFalse(result["patch_result"])
        self.assertTrue(any(line.startswith("[!]" + url + " | 已取消：") for line in logs))

    def test_opt_in_required_before_any_artifact_or_network(self):
        session = Session([])
        with patch.object(media, "_session", return_value=session):
            with self.assertRaisesRegex(ExportError, "explicit"):
                media.download_library_media(self.library, self.work, download_media=False)
        self.assertFalse(self.library.exists())
        self.assertFalse(self.work.exists())
        self.assertEqual(session.calls, [])

    def test_package_work_path_is_rejected_without_modifying_original_tree(self):
        self.add("duoshan", [{"type": 7, "content": "text"}])
        package = self.root / "com.tencent.mm"
        package.mkdir()
        original = package / "original.bin"
        original.write_bytes(b"unchanged")
        with self.assertRaisesRegex(ExportError, "original Android"):
            media.download_library_media(self.library, package / "work", download_media=True)
        self.assertEqual(list(package.iterdir()), [original])
        self.assertEqual(original.read_bytes(), b"unchanged")

    def test_fetch_checks_every_redirect_and_closes_all_responses(self):
        run = self.root / "fetch"
        run.mkdir()
        first = Response(status=302, headers={"Location": "https://127.0.0.1/private"})
        session = Session([first])
        with patch.object(media, "_session", return_value=session):
            with self.assertRaisesRegex(media._ResourceError, "unapproved"):
                media._fetch("duoshan", "https://p.douyinpic.com/start", "image", run, None)
        self.assertTrue(first.closed)
        self.assertEqual(len(session.calls), 1)
        for url in ("https://douyinpic.com.evil.test/file", "https://evil-douyinpic.com/file", "https://user:secret@p.douyinpic.com/file", "https://p.douyinpic.com:8080/file", "https://p.douyinpic.com:invalid/file"):
            self.assertFalse(media._allowed("duoshan", url))
        self.assertFalse(media._allowed("wechat", "https://msg-img.xhscdn.com/file"))
        self.assertTrue(media._allowed("xhs", "https://image-url-2-feature-1251524319.cos.ap-shanghai.myqcloud.com/file"))
        self.assertFalse(media._allowed("xhs", "https://other.cos.ap-shanghai.myqcloud.com/file"))

    def test_stream_limits_expiry_and_torn_response_retain_partials(self):
        run = self.root / "fetch"
        run.mkdir()
        oversized = Response(headers={"Content-Length": "100"})
        stream = Response(headers={}, chunks=[b"12", b"34567"])
        expired = Response(status=403)
        truncated = Response(b"123", headers={"Content-Length": "4"})
        for response, reason in ((oversized, "size_limit"), (stream, "size_limit"), (expired, "expired"), (truncated, "incomplete")):
            with self.subTest(reason=reason), patch.object(media, "_session", return_value=Session([response])), patch.dict(media.MAX_BYTES, {"image": 5}):
                with self.assertRaisesRegex(media._ResourceError, reason):
                    media._fetch("xhs", "https://msg-img.xhscdn.com/a", "image", run, None)
                self.assertTrue(response.closed)
        self.assertEqual(len(list(run.glob("*.partial"))), 2)

    def test_stream_cancellation_retains_received_bytes_and_closes_response(self):
        run = self.root / "fetch"
        run.mkdir()
        response = Response(headers={}, chunks=[b"received", b"not-written"])
        checks = [0]
        def cancel():
            checks[0] += 1
            return checks[0] >= 3
        with patch.object(media, "_session", return_value=Session([response])):
            with self.assertRaises(media._Cancelled):
                media._fetch("xhs", "https://msg-img.xhscdn.com/image", "image", run, cancel)
        self.assertTrue(response.closed)
        self.assertEqual(next(run.glob("*.partial")).read_bytes(), b"received")

    def test_downloads_live_three_app_rows_preserving_raw_edits_and_deleted_rows(self):
        uid = self.add("duoshan", [{"type": 17, "content": {"resource_url": {"url_list": ["https://a.douyinstatic.com/voice"]}}},
                                  {"type": 5, "content": {"url": {"url_list": ["https://p.douyinpic.com/sticker"]}}}])
        self.add("xhs", [{"content_type": 2, "content": {"content": {"imageDataMap": {"IM_ORG": {"url": "https://msg-img.xhscdn.com/photo?signature=keep"}}}}}])
        self.add("wechat", [{"type": 47, "_wechat_raw": {"type": 47, "content": '<msg><emoji cdnurl="https://emoji.qpic.cn/sticker"/></msg>'}}])
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            before = db.execute("SELECT uid,msg_uuid,raw_json FROM messages ORDER BY uid,msg_uuid").fetchall()
            db.execute("INSERT INTO message_edits VALUES (?,?,?, ?,1,4,?)", (uid, "0", "edited", "note", "today"))
            db.execute("INSERT INTO deletions VALUES ('message','synthetic-deletion','today')")
            db.commit()
        # A stale export line must never be imported by the media operation.
        (self.library / ("dy_" + uid) / "messages.jsonl").write_text('{"msg_uuid":"stale-deleted","type":27,"content":{"resource_url":{"url_list":["https://p.douyinpic.com/deleted"]}}}\n')
        session = Session([Response(wav_bytes()), Response(png_bytes()), Response(png_bytes(color=(90, 80, 70))), Response(png_bytes(color=(60, 50, 40)))])
        result = self.run_download(session)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["totals"], dict(downloaded=4, existing=0, failed=0, unsupported=0))
        self.assertEqual(len(session.calls), 4)
        self.assertTrue(any(url.endswith("?signature=keep") for url, _ in session.calls))
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            self.assertEqual(db.execute("SELECT uid,msg_uuid,raw_json FROM messages ORDER BY uid,msg_uuid").fetchall(), before)
            self.assertEqual(db.execute("SELECT text_override,note,hidden,revision FROM message_edits").fetchone(), ("edited", "note", 1, 4))
            self.assertEqual(db.execute("SELECT count(*) FROM deletions").fetchone()[0], 1)
            for (meta_text,) in db.execute("SELECT meta_json FROM messages"):
                meta = json.loads(meta_text)
                self.assertEqual(meta["media"]["status"], "ok")
                self.assertTrue((self.library / meta["media"]["path"]).is_file())
        repeated = self.run_download(Session([]))
        self.assertEqual(repeated["totals"], dict(downloaded=0, existing=4, failed=0, unsupported=0))

    def test_manifest_recovers_patch_failure_without_another_download(self):
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": ["https://p.douyinpic.com/image"]}}}])
        actual_patch = media._patch
        def fail_publish(output, updates, node, progress):
            if updates is not None:
                raise ExportError("synthetic reader failure")
            return actual_patch(output, updates, node, progress)
        with patch.object(media, "_patch", side_effect=fail_publish):
            with self.assertRaisesRegex(ExportError, "synthetic"):
                self.run_download(Session([Response(png_bytes())]))
        self.assertTrue((self.library / media.MANIFEST).is_file())
        result = self.run_download(Session([]))
        self.assertEqual(result["totals"]["existing"], 1)
        self.assertEqual(result["patch_result"]["patched_messages"], 1)

    def test_expired_recorded_original_uses_recorded_fallback_only(self):
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"origin_url_list": ["http://p.douyinpic.com/original?signature=unchanged"], "large_url_list": ["https://p.douyinpic.com/recorded-fallback"]}}}])
        rejected, accepted = Response(status=403), Response(png_bytes())
        session = Session([rejected, accepted])
        result = self.run_download(session)
        self.assertEqual(result["totals"]["downloaded"], 1)
        self.assertEqual([url for url, _ in session.calls], ["https://p.douyinpic.com/original?signature=unchanged", "https://p.douyinpic.com/recorded-fallback"])
        self.assertTrue(rejected.closed and accepted.closed)
        manifest = (self.library / media.MANIFEST).read_text()
        self.assertIn("signature=unchanged", manifest)
        record = next(iter(media._load_manifest(self.library).values()))
        self.assertEqual(record["attempts"][0]["status"], "failed")
        self.assertEqual(record["attempts"][1]["status"], "ok")
        self.assertEqual(record["attempts"][0]["requests"][0]["url"], "https://p.douyinpic.com/original?signature=unchanged")

    def test_encrypted_and_missing_media_are_reported_without_network(self):
        self.add("wechat", [{"type": 3, "_wechat_raw": {"type": 3, "content": '<msg><img aeskey="secret" cdnmidimgurl="https://m.qpic.cn/encrypted"/></msg>'}}, {"type": 34}])
        session = Session([])
        result = self.run_download(session)
        self.assertEqual(result["totals"]["unsupported"], 2)
        self.assertEqual({r["reason"] for r in result["failures"]}, {"encrypted_wechat_resource", "wechat_url_not_recorded"})
        self.assertEqual(session.calls, [])
        self.assertNotIn("secret", json.dumps(result))

    def test_deleted_during_download_is_not_recreated_or_patched(self):
        uid = self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": ["https://p.douyinpic.com/photo"]}}}])
        class DeletedResponse(Response):
            def iter_content(response, size):
                with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
                    db.execute("DELETE FROM messages WHERE uid=?", (uid,));db.commit()
                yield png_bytes()
        result = self.run_download(Session([DeletedResponse(headers={})]))
        self.assertEqual(result["totals"]["downloaded"], 1)
        self.assertEqual(result["patch_result"]["skipped_messages"], 1)
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_no_url_media_never_cross_links_different_wechat_messages(self):
        uid = self.add("wechat", [{"type": 3}, {"type": 3}])
        local = self.library / "attachments/wechat/existing.png"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(png_bytes())
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            text = db.execute("SELECT meta_json FROM messages WHERE uid=? AND msg_uuid='0'", (uid,)).fetchone()[0]
            meta = json.loads(text);meta["local_media"] = ["attachments/wechat/existing.png"]
            db.execute("UPDATE messages SET meta_json=? WHERE uid=? AND msg_uuid='0'", (json.dumps(meta), uid));db.commit()
        result = self.run_download(Session([]))
        self.assertEqual(result["totals"], dict(downloaded=0, existing=1, failed=0, unsupported=1))
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            other = json.loads(db.execute("SELECT meta_json FROM messages WHERE uid=? AND msg_uuid='1'", (uid,)).fetchone()[0])
            self.assertEqual(other["local_media"], [])

    def test_corrupt_same_size_download_retries_and_never_overwrites_bad_file(self):
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": ["https://p.douyinpic.com/image"]}}}])
        self.run_download(Session([Response(png_bytes())]))
        asset = next((self.library / "attachments").rglob("*.png"))
        asset.write_bytes(b"x" * asset.stat().st_size)
        second = self.run_download(Session([Response(png_bytes())]))
        self.assertEqual(second["totals"]["failed"], 1)
        self.assertEqual(asset.read_bytes(), b"x" * asset.stat().st_size)
        self.assertIn("hash_mismatch", second["failures"][0]["reason"])

    def test_cancellation_commits_finished_work_and_later_resumes(self):
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": ["https://p.douyinpic.com/first"]}}},
                                  {"type": 27, "content": {"resource_url": {"url_list": ["https://p.douyinpic.com/second"]}}}])
        cancelled = [False]
        actual = media._download
        count = [0]
        def download(*args):
            count[0] += 1
            if count[0] == 2:
                cancelled[0] = True
                raise media._Cancelled()
            return actual(*args)
        with patch.object(media, "_download", side_effect=download):
            result = self.run_download(Session([Response(png_bytes())]), cancel=lambda: cancelled[0])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["totals"]["downloaded"], 1)
        self.assertEqual(result["patch_result"]["patched_messages"], 1)
        resumed = self.run_download(Session([Response(png_bytes(color=(80, 10, 50)))]))
        self.assertEqual(resumed["totals"]["existing"], 1)
        self.assertEqual(resumed["totals"]["downloaded"], 1)

    def test_cli_requires_explicit_online_flag(self):
        from duoshan_export.cli import main
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["library-media", str(self.library), "--json"])
        self.assertEqual(code, 1)
        self.assertIn("explicit", json.loads(stdout.getvalue())["error"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "optional FFmpeg unavailable")
    def test_real_video_bytes_pass_probe_and_are_preserved(self):
        video = self.root / "generated.mp4"
        generated = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=32x32:r=1", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], capture_output=True)
        self.assertEqual(generated.returncode, 0)
        self.add("xhs", [{"content_type": 9, "content": {"link": "https://msg-video.xhscdn.com/clip"}}])
        result = self.run_download(Session([Response(video.read_bytes())]))
        self.assertEqual(result["totals"]["downloaded"], 1)
        stored = next((self.library / "attachments").rglob("*.mp4"))
        self.assertEqual(stored.read_bytes(), video.read_bytes())
        self.assertFalse(list((self.work / "media").rglob("*.mp4")))
        self.assertFalse([p for p in (self.library / "attachments").rglob("*.partial") if p.is_file()])

    @unittest.skipUnless(shutil.which("ffmpeg"), "optional FFmpeg unavailable")
    def test_xhs_live_photo_keeps_existing_photo_and_downloads_only_video(self):
        video = self.root / "live.mp4"
        result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x32:r=1", "-t", "1", "-c:v", "libx264", str(video)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        uid = self.add("xhs", [{"content_type": 2, "content": {"content": {"imageDataMap": {"IM_ORG": {"url": "https://msg-img.xhscdn.com/image"}}, "livePhotoVideo": {"videoLink": "https://msg-video.xhscdn.com/live"}}}}])
        local = self.library / "attachments/xhs/photo.png"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(png_bytes())
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            meta = json.loads(db.execute("SELECT meta_json FROM messages WHERE uid=?", (uid,)).fetchone()[0])
            meta["local_media"] = ["attachments/xhs/photo.png"]
            db.execute("UPDATE messages SET meta_json=? WHERE uid=?", (json.dumps(meta), uid));db.commit()
        session = Session([Response(video.read_bytes())])
        result = self.run_download(session)
        self.assertEqual(result["totals"], dict(downloaded=1, existing=1, failed=0, unsupported=0))
        self.assertEqual(session.calls[0][0], "https://msg-video.xhscdn.com/live")
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            meta = json.loads(db.execute("SELECT meta_json FROM messages WHERE uid=?", (uid,)).fetchone()[0])
            self.assertEqual(len(meta["local_media"]), 2)
            self.assertIn("attachments/xhs/photo.png", meta["local_media"])
        again = self.run_download(Session([]))
        self.assertEqual(again["totals"]["existing"], 2)
        self.assertIsNone(again["patch_result"])

    def test_existing_validation_is_cached_and_does_not_republish_local_rows(self):
        uid = self.add("wechat", [{"type": 3}, {"type": 3}])
        local = self.library / "attachments/wechat/same.png"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(png_bytes())
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            for uuid, text in db.execute("SELECT msg_uuid,meta_json FROM messages WHERE uid=?", (uid,)).fetchall():
                meta = json.loads(text);meta["local_media"] = ["attachments/wechat/same.png"]
                db.execute("UPDATE messages SET meta_json=? WHERE uid=? AND msg_uuid=?", (json.dumps(meta), uid, uuid))
            db.commit()
        actual = media.file_sha256
        hashes = []
        def count_hash(path):
            hashes.append(str(path))
            return actual(path)
        with patch.object(media, "file_sha256", side_effect=count_hash):
            result = self.run_download(Session([]))
        self.assertEqual(result["totals"]["existing"], 2)
        self.assertEqual(hashes.count(str(local)), 1)
        self.assertIsNone(result["patch_result"])
        self.assertEqual(result["recovery_result"]["status"], "completed")

    def test_six_workers_publish_into_fresh_directories_and_journal_other_successes_after_failure(self):
        rows = [{"type": 27, "content": {"resource_url": {"url_list": ["https://p.douyinpic.com/" + str(i)]}}} for i in range(12)]
        self.add("duoshan", rows)
        actual = media._download
        def one_worker_fails(item, *args):
            # Directory preflight must complete before any worker starts.
            self.assertTrue(Path(item["incoming"]).is_dir())
            if item["urls"][0].endswith("/3"):
                raise OSError("synthetic worker failure with private URL")
            return actual(item, *args)
        session = Session([Response(png_bytes(color=(20 + i, 30, 40))) for i in range(11)])
        coordinator = threading.get_ident()
        callbacks = []
        with patch.object(media, "_session", return_value=session), patch.object(media, "_download", side_effect=one_worker_fails):
            result = media.download_library_media(self.library, self.work / "media", workers=6,
                progress=lambda line: callbacks.append((threading.get_ident(), line)), download_media=True)
        self.assertEqual(result["totals"], dict(downloaded=11, existing=0, failed=1, unsupported=0))
        self.assertEqual(result["patch_result"]["patched_messages"], 11)
        self.assertEqual(len([r for r in media._load_manifest(self.library).values() if r["status"] == "ok"]), 11)
        self.assertEqual(result["failures"][0]["reason"], "worker_OSError")
        self.assertTrue(all(thread == coordinator for thread, _ in callbacks))
        self.assertEqual(sum(line.startswith("[✓]") for _, line in callbacks), 11)
        self.assertNotIn("synthetic worker failure with private URL", "\n".join(line for _, line in callbacks))
        self.assertFalse(list((self.library / "attachments").rglob("*.partial")))
        resumed = self.run_download(Session([Response(png_bytes(color=(90, 50, 10)))]))
        self.assertEqual(resumed["totals"], dict(downloaded=1, existing=11, failed=0, unsupported=0))

    def test_matching_content_hash_avoids_reprobing_existing_voice(self):
        uid = self.add("wechat", [{"type": 34}])
        content = wav_bytes()
        digest = hashlib.sha256(content).hexdigest()
        relative = "attachments/wechat/" + digest + ".wav"
        local = self.library / relative
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(content)
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            meta = json.loads(db.execute("SELECT meta_json FROM messages WHERE uid=?", (uid,)).fetchone()[0])
            meta["local_media"] = [relative]
            db.execute("UPDATE messages SET meta_json=? WHERE uid=?", (json.dumps(meta), uid));db.commit()
        with patch.object(media, "_process", side_effect=AssertionError("existing voice should not be decoded again")):
            result = self.run_download(Session([]))
        self.assertEqual(result["totals"]["existing"], 1)

    def test_directory_rejection_reports_local_diagnostics_and_other_app_completes(self):
        self.add("duoshan", [{"type": 27, "content": {"resource_url": {"url_list": ["https://p.douyinpic.com/a?token=private"]}}}])
        self.add("xhs", [{"content_type": 2, "content": {"link": "https://msg-img.xhscdn.com/a"}}])
        actual = media._contained_file
        def reject_duoshan_staging(output, relative):
            if "attachments/duoshan/online/.incoming/" in relative:
                raise ExportError("synthetic path rejection with private details")
            return actual(output, relative)
        logs = []
        session = Session([Response(png_bytes())])
        with patch.object(media, "_contained_file", side_effect=reject_duoshan_staging), patch.object(media, "_session", return_value=session):
            result = media.download_library_media(self.library, self.work / "media", workers=6, progress=logs.append, download_media=True)
        self.assertEqual(result["totals"], dict(downloaded=1, existing=0, failed=1, unsupported=0))
        self.assertEqual(result["patch_result"]["patched_messages"], 1)
        diagnostic = result["failures"][0]["diagnostic"]
        self.assertEqual(diagnostic["error_type"], "ExportError")
        self.assertEqual(diagnostic["root"], str(self.library))
        self.assertEqual(diagnostic["resolved_root"], str(self.library.resolve()))
        self.assertIn(".incoming", diagnostic["resolved_path"])
        self.assertIn(diagnostic["message"], logs)
        self.assertIn("媒体路径校验失败", diagnostic["message"])
        self.assertNotIn("private details", json.dumps(result) + "".join(logs))
        self.assertTrue(any(line.startswith("[x]https://p.douyinpic.com/a?token=private |")
                            and "未发起请求" in line for line in logs))
        journal_failure = next(r for r in media._load_manifest(self.library).values() if r["status"] == "failed")
        self.assertEqual(journal_failure["diagnostic"], diagnostic)

    def test_silk_decoder_wav_is_published_without_second_conversion_or_staged_copy(self):
        original = b"#!SILK_V3" + b"synthetic encoded voice"
        decoded = wav_bytes()
        self.add("duoshan", [{"type": 17, "content": {"resource_url": {"url_list": ["https://v.douyinstatic.com/voice"]}}}])
        calls = []
        def decode(command, *, input_text=None, timeout=120):
            calls.append(command)
            if Path(command[-1]).name == "decode-voice.mjs":
                request = json.loads(input_text)
                self.assertEqual(Path(request["source"]).read_bytes(), original)
                Path(request["output"]).write_bytes(decoded)
                return subprocess.CompletedProcess(command, 0, stdout='{"ok":true}')
            self.assertEqual(command[0], "ffprobe", "decoded WAV must not be converted again")
            self.assertEqual(Path(command[-1]).read_bytes(), decoded)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"streams": [{"codec_type": "audio"}], "format": {"duration": "0.2"}}))
        with patch.object(media, "_process", side_effect=decode):
            result = self.run_download(Session([Response(original)]))
        self.assertEqual(result["totals"]["downloaded"], 1)
        self.assertEqual(len(calls), 2)
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            meta = json.loads(db.execute("SELECT meta_json FROM messages").fetchone()[0])
        self.assertEqual((self.library / meta["media"]["path"]).read_bytes(), decoded)
        self.assertEqual((self.library / meta["original_media"][0]).read_bytes(), original)
        self.assertEqual(len([p for p in (self.library / "attachments").rglob("*") if p.is_file()]), 2)
        self.assertFalse(list((self.work / "media").rglob("*.wav")))

    def test_image_conversion_closes_source_before_atomic_publication(self):
        from PIL import Image
        staging = self.library / "attachments/duoshan/online/.incoming/test"
        staging.mkdir(parents=True)
        source = staging / "download.partial"
        with Image.new("RGB", (4, 4), (20, 40, 60)) as fixture:
            fixture.save(source, format="BMP")
        original = source.read_bytes()
        active = []
        actual_open, actual_publish = Image.open, media._publish
        class OpenImage:
            def __init__(self, *args, **kwargs):
                self.image = actual_open(*args, **kwargs)
            def __enter__(self):
                active.append(self)
                return self.image.__enter__()
            def __exit__(self, *args):
                try:
                    return self.image.__exit__(*args)
                finally:
                    active.remove(self)
        def publish(*args):
            self.assertEqual(active, [], "Pillow source contexts must close before rename")
            return actual_publish(*args)
        with patch.object(Image, "open", side_effect=OpenImage), patch.object(media, "_publish", side_effect=publish):
            result = media._prepare_asset(source, "image/bmp", "image", self.library, "duoshan", staging, "node", None)
        self.assertEqual(result["format"], "PNG")
        self.assertEqual((self.library / result["originals"][0]["path"]).read_bytes(), original)
        self.assertTrue((self.library / result["path"]).read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(list(staging.iterdir()), [])
        self.assertEqual(len([p for p in (self.library / "attachments").rglob("*") if p.is_file()]), 2)

    @unittest.skipUnless(shutil.which("ffmpeg"), "optional FFmpeg unavailable")
    def test_voice_mislabeled_video_mime_is_converted_and_original_retained_once(self):
        original = self.root / "original.wav"
        original.write_bytes(wav_bytes())
        encoded = self.root / "encoded.m4a"
        process = subprocess.run(["ffmpeg", "-v", "error", "-i", str(original), "-c:a", "aac", str(encoded)], capture_output=True)
        self.assertEqual(process.returncode, 0)
        self.add("duoshan", [{"type": 17, "content": {"resource_url": {"url_list": ["https://v.douyinstatic.com/voice"]}}}])
        result = self.run_download(Session([Response(encoded.read_bytes(), headers={"Content-Type": "video/mpeg"})]))
        self.assertEqual(result["totals"]["downloaded"], 1)
        with closing(sqlite3.connect(self.library / "reader.sqlite")) as db:
            meta = json.loads(db.execute("SELECT meta_json FROM messages").fetchone()[0])
        self.assertEqual(meta["media"]["kind"], "voice")
        self.assertTrue(meta["media"]["path"].endswith(".wav"))
        self.assertEqual(len(meta["original_media"]), 1)
        self.assertEqual((self.library / meta["original_media"][0]).read_bytes(), encoded.read_bytes())
        files = [p for p in (self.library / "attachments").rglob("*") if p.is_file()]
        self.assertEqual(len(files), 2)
        self.assertFalse(list((self.work / "media").rglob("*.wav")))


if __name__ == "__main__":
    unittest.main()
