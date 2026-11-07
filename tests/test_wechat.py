"""Independent Android WeChat fixtures; never reads the user's chat backups."""
from __future__ import annotations

import base64
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from duoshan_export.apps import wechat
from duoshan_export.errors import ExportError
from duoshan_export.library import import_sources
from duoshan_export.progress import ProgressEvent
from tests.synthetic import png_bytes, wav_bytes

try:
    import sqlcipher3
except ImportError:
    sqlcipher3 = None

SCHEMA = """
CREATE TABLE message (
    msgId INTEGER PRIMARY KEY, msgSvrId INTEGER, talker TEXT, isSend INTEGER,
    createTime INTEGER, content TEXT, type INTEGER, imgPath TEXT, reserved BLOB
);
CREATE TABLE rcontact (username TEXT PRIMARY KEY, conRemark TEXT, nickname TEXT);
CREATE TABLE ImgInfo2 (msglocalid INTEGER, bigImgPath TEXT, midImgPath TEXT, thumbImgPath TEXT);
CREATE TABLE videoinfo2 (msglocalid INTEGER, video_path TEXT, filename TEXT);
CREATE TABLE voiceinfo (MsgLocalId INTEGER, FileName TEXT);
CREATE TABLE appattach (msgInfoId INTEGER, fileFullPath TEXT);
"""
INSERT = "INSERT INTO message VALUES (:msgId,:msgSvrId,:talker,:isSend,:createTime,:content,:type,:imgPath,:reserved)"


def account_hash(uin):
    return hashlib.md5(("mm" + str(uin)).encode()).hexdigest()


def account_key(uin, imei="1234567890ABCDEF"):
    return hashlib.md5((imei + str(uin)).encode()).hexdigest()[:7]


def message(mid, **values):
    row = dict(msgId=mid, msgSvrId=9007199254740997 + mid, talker="peer",
               isSend=0, createTime=1700000000000 + mid, content="原始消息 中文🙂",
               type=1, imgPath="", reserved=b"\x00\xffraw")
    row.update(values)
    return row


def tree_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class WeChatTests(unittest.TestCase):
    def setUp(self):
        # The configured OS temporary directory is outside E:\analysis.
        self.temp = tempfile.TemporaryDirectory(prefix="wechat-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "backup" / "com.tencent.mm"
        self.source.mkdir(parents=True)
        self.work = self.root / "work"
        self.output = self.root / "output"

    def make_account(self, rows, *, uin=123456789, key=None, source=None):
        source = source or self.source
        uid = account_hash(uin)
        path = source / "MicroMsg" / uid / "EnMicroMsg.db"
        path.parent.mkdir(parents=True)
        db = sqlcipher3.connect(str(path)) if key else sqlite3.connect(path)
        try:
            if key:
                # Fixture settings are deliberately independent of adapter code.
                db.execute("PRAGMA key = '" + key + "'")
                db.execute("PRAGMA cipher_page_size = 1024")
                db.execute("PRAGMA kdf_iter = 4000")
                db.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1")
                db.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA1")
                db.execute("PRAGMA cipher_use_hmac = OFF")
            db.executescript(SCHEMA)
            db.executemany(INSERT, rows)
            db.executemany("INSERT INTO rcontact VALUES (?,?,?)", [
                ("peer", "好友备注", "好友昵称"), ("person", "", "群成员"),
                ("room@chatroom", "", "测试群")])
            db.commit()
        finally:
            db.close()
        return uid, path

    def rows(self, uid, output=None):
        return [json.loads(line) for line in ((output or self.output) / uid / "messages.jsonl").read_text(encoding="utf-8").splitlines()]

    def media(self, uid, relative, value, *, source=None):
        path = (source or self.source) / "MicroMsg" / uid / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def test_stage_heartbeat_has_truthful_counts_and_stops_before_final_event(self):
        events=[]; heartbeat=threading.Event()
        def record(event):
            events.append(event)
            if '进行中' in event:heartbeat.set()
        with patch.object(wechat,'PROGRESS_INTERVAL',0.01):
            with wechat._StageProgress(record,'测试等待',done=0,total=2) as stage:
                stage.update(action='等待阻塞读取')
                self.assertTrue(heartbeat.wait(2))
                self.assertEqual(events[-1].completed,0)
                stage.add(done=2)
            self.assertFalse(stage.thread.is_alive())
            self.assertEqual(events[-1].state,'completed')
            self.assertEqual((events[-1].completed,events[-1].total),(2,2))
            count=len(events)
            time.sleep(0.04)
            self.assertEqual(len(events),count)
        self.assertTrue(all(isinstance(event,ProgressEvent) for event in events))

    def test_stage_heartbeat_preserves_callback_error_cancel_and_processing_error(self):
        error=RuntimeError('synthetic callback failure'); called=threading.Event()
        def fail(event):
            if '进行中' in event:
                called.set();raise error
        with patch.object(wechat,'PROGRESS_INTERVAL',0.01):
            with self.assertRaises(RuntimeError) as raised:
                with wechat._StageProgress(fail,'callback',done=0,total=1) as stage:
                    self.assertTrue(called.wait(2))
            self.assertIs(raised.exception,error)
            self.assertFalse(stage.thread.is_alive())
        for cancelled in (False,True):
            events=[]; original=ExportError('original processing error')
            with self.assertRaises(ExportError) as raised:
                with wechat._StageProgress(events.append,'异常阶段',done=0,total=1,cancel=lambda:cancelled) as stage:
                    raise original
            self.assertIs(raised.exception,original)
            self.assertFalse(stage.thread.is_alive())
            self.assertEqual(events[-1].state,'cancelled' if cancelled else 'failed')
            self.assertNotIn('完成',events[-1])

    def test_account_preparation_heartbeat_ends_before_counted_message_export(self):
        uid,_=self.make_account([message(1)])
        observed=threading.Event();events=[];open_account=wechat.open_account
        def record(event):
            events.append(event)
            if isinstance(event,ProgressEvent) and event.stage.startswith('准备微信账号') and '进行中' in event:
                observed.set()
        @contextmanager
        def slow_open(*args,**kwargs):
            self.assertTrue(observed.wait(2),'account snapshot/decryption must report its blocking wait')
            with open_account(*args,**kwargs) as db:yield db
        with patch.object(wechat,'PROGRESS_INTERVAL',0.01),patch.object(wechat,'open_account',side_effect=slow_open):
            wechat.export_backup(self.source,self.output,self.work,progress=record)
        prepared=[e for e in events if isinstance(e,ProgressEvent) and e.stage.startswith('准备微信账号')]
        self.assertTrue(all(e.completed is None and e.total is None for e in prepared))
        self.assertIn('复制和解密数据库',next(e for e in prepared if '进行中' in e))
        self.assertEqual(prepared[-1].state,'completed')
        first_export=next(i for i,e in enumerate(events) if isinstance(e,ProgressEvent) and e.stage.startswith('导出微信消息'))
        self.assertTrue(all(not (isinstance(e,ProgressEvent) and e.stage.startswith('准备微信账号')) for e in events[first_export:]))
        self.assertEqual((events[first_export].completed,events[first_export].total),(0,1))
        self.assertEqual(self.rows(uid)[0]['_wechat_raw']['msgId'],1)

    def test_invalid_metadata_cache_reports_failed_verification_before_fallback(self):
        cache=self.root/'cache';cache.mkdir()
        data=cache/'database.db';data.write_bytes(b'corrupted')
        manifest=cache/'database-scan.json'
        manifest.write_text(json.dumps({'metadata':[{'path':'database.db','sha256':hashlib.sha256(b'original').hexdigest()}]}))
        events=[]
        self.assertIsNone(wechat._cached_metadata(cache,manifest,progress=events.append))
        self.assertEqual((events[-1].completed,events[-1].total,events[-1].state),(0,1,'failed'))
        self.assertIn('失败 1',events[-1])
        self.assertEqual(data.read_bytes(),b'corrupted')

    def test_archive_skip_heartbeat_reports_member_position_and_exact_extraction_totals(self):
        data=b'\xff\xd8\xffsynthetic'; digest=hashlib.sha256(data).hexdigest()
        item={'archive':str(self.root/'fixture.tar.zst'),'member':'MicroMsg/image2/wanted',
              'size':len(data),'sha256':digest,'category':'images','ext':'.jpg'}
        observed=threading.Event();events=[]
        def record(event):
            events.append(event)
            if event.stage.startswith('提取已引用') and '进行中' in event and '已见成员 1' in event:
                observed.set()
        class Archive:
            def __iter__(archive):
                skipped=tarfile.TarInfo('unrelated-large-member');skipped.size=8192;skipped.offset=4096
                yield skipped
                self.assertTrue(observed.wait(2),'heartbeat must run while archive skips to next wanted member')
                wanted=tarfile.TarInfo(item['member']);wanted.size=len(data);wanted.offset=12800
                yield wanted
            def extractfile(archive, member):return io.BytesIO(data)
        @contextmanager
        def open_archive(_):yield Archive()
        with patch.object(wechat,'PROGRESS_INTERVAL',0.01),patch.object(wechat,'_open_tar',side_effect=open_archive):
            wechat._write_media([item],self.output,self.work,progress=record)
        heartbeat=next(e for e in events if e.stage.startswith('提取已引用') and '进行中' in e)
        self.assertEqual((heartbeat.completed,heartbeat.total),(0,1))
        self.assertIn('最近成员起始位置 4,096 字节',heartbeat)
        last=events[-1]
        self.assertEqual((last.completed,last.total,last.state),(1,1,'completed'))
        self.assertIn(f'已处理字节 {len(data)}/{len(data)}',last)
        self.assertEqual((self.output/item['export_path']).read_bytes(),data)

    def test_existing_media_hash_wait_is_observable_and_reuse_is_verified(self):
        data=b'original media';digest=hashlib.sha256(data).hexdigest()
        item={'size':len(data),'sha256':digest,'category':'files','ext':'.bin'}
        target=self.output/f'attachments/wechat/files/{digest}.bin'
        target.parent.mkdir(parents=True);target.write_bytes(data)
        seen=threading.Event();events=[];actual_open=Path.open
        def record(event):
            events.append(event)
            if '进行中' in event and '校验已有附件' in event:seen.set()
        class SlowReader:
            def __enter__(reader):
                reader.stream=actual_open(target,'rb');reader.waited=False;return reader
            def read(reader,size):
                if not reader.waited:
                    reader.waited=True;self.assertTrue(seen.wait(2))
                return reader.stream.read(size)
            def __exit__(reader,*args):reader.stream.close()
        def open_file(path,*args,**kwargs):
            return SlowReader() if path==target and args==('rb',) else actual_open(path,*args,**kwargs)
        with patch.object(wechat,'PROGRESS_INTERVAL',0.01),patch.object(Path,'open',open_file):
            wechat._write_media([item],self.output,self.work,progress=record)
        self.assertIn('复用 1',events[-1])
        self.assertEqual((events[-1].completed,events[-1].total),(1,1))
        self.assertEqual(target.read_bytes(),data)

    def test_conversion_waits_report_real_counts_reuse_and_failure_without_changing_originals(self):
        cases=[('.wxgf','images',wechat._playable_images,png_bytes(),'等待 FFmpeg 图片转换'),
               ('.amr','audio',wechat._playable_audio,wav_bytes(),'等待 FFmpeg AMR 语音转换')]
        for ext,category,convert,playable,waiting in cases:
            original=b'original '+ext.encode();digest=hashlib.sha256(original).hexdigest()
            relative=f'attachments/wechat/{category}/{digest}{ext}'
            source=self.output/relative;source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(original)
            item={'sha256':digest,'ext':ext,'category':category,'size':len(original),'export_path':relative}
            events=[];seen=threading.Event()
            def record(event):
                events.append(event)
                if '进行中' in event and waiting in event:seen.set()
            def run(command,**kwargs):
                self.assertTrue(seen.wait(2),'decoder wait must keep progress observable')
                Path(command[-1]).write_bytes(playable)
                return subprocess.CompletedProcess(command,0)
            with patch.object(wechat,'PROGRESS_INTERVAL',0.01),patch.object(wechat.subprocess,'run',side_effect=run):
                replacement,warnings=convert([item],self.output,self.work,record)
            self.assertEqual(warnings,[])
            self.assertEqual((events[-1].completed,events[-1].total),(1,1))
            self.assertIn('新转换 1',events[-1])
            self.assertTrue(any(e.completed==0 and waiting in e for e in events))
            events=[]
            with patch.object(wechat.subprocess,'run',side_effect=AssertionError('valid cached playback must be reused')):
                again,warnings=convert([item],self.output,self.work,events.append)
            self.assertEqual(again,replacement);self.assertIn('复用 1',events[-1])
            self.assertEqual(source.read_bytes(),original)
            failed_item=dict(item,sha256=digest+'-failed')
            events=[]
            with patch.object(wechat.subprocess,'run',return_value=subprocess.CompletedProcess([],1)):
                failed,warnings=convert([failed_item],self.output,self.work,events.append)
            self.assertEqual(failed,{})
            self.assertTrue(warnings);self.assertIn('失败 1',events[-1])
            self.assertEqual((events[-1].completed,events[-1].total),(1,1))
            self.assertEqual(source.read_bytes(),original)

    def test_silk_response_wait_has_heartbeat_before_decoder_returns(self):
        original=b'#!SILK_V3synthetic';digest=hashlib.sha256(original).hexdigest()
        relative=f'attachments/wechat/audio/{digest}.silk'
        source=self.output/relative;source.parent.mkdir(parents=True);source.write_bytes(original)
        item={'sha256':digest,'ext':'.silk','category':'audio','size':len(original),'export_path':relative}
        seen=threading.Event();events=[]
        def record(event):
            events.append(event)
            if '进行中' in event and '等待 SILK 语音解码' in event:seen.set()
        process=Mock(stdin=io.StringIO(),stdout=io.StringIO(''))
        process.wait.return_value=0
        class Inbox:
            def put(inbox,value):pass
            def get(inbox,timeout):
                self.assertEqual(timeout,60)
                self.assertTrue(seen.wait(2),'SILK response wait must report current stage')
                request=json.loads(process.stdin.getvalue())
                Path(request['output']).write_bytes(wav_bytes())
                return '{"ok":true}'
        with patch.object(wechat,'PROGRESS_INTERVAL',0.01),patch.object(wechat.subprocess,'Popen',return_value=process),patch.object(wechat.queue,'Queue',return_value=Inbox()):
            playback,warnings=wechat._playable_audio([item],self.output,self.work,record)
        self.assertEqual(warnings,[]);self.assertEqual(len(playback),1)
        self.assertTrue(process.stdin.closed);self.assertTrue(process.stdout.closed)
        process.wait.assert_called_once_with(timeout=5)
        self.assertTrue(any(e.completed==0 and '等待 SILK 语音解码' in e for e in events))
        self.assertEqual((events[-1].completed,events[-1].total,events[-1].state),(1,1,'completed'))
        self.assertEqual(source.read_bytes(),original)

    def test_message_export_and_reference_rewrite_emit_real_totals_and_preserve_raw_rows(self):
        uid,_=self.make_account([message(1,type=3,imgPath='picture'),message(2)])
        image=self.media(uid,'image2/picture',b'\xff\xd8\xfforiginal')
        original=image.read_bytes();before=tree_hashes(self.source)
        events=[]
        def previews(items,output,work,progress,cancel=None):
            target=Path(output)/'attachments/wechat/images/preview.png'
            target.write_bytes(png_bytes())
            return {items[0]['export_path']:'attachments/wechat/images/preview.png'},[]
        with patch.object(wechat,'_playable_images',side_effect=previews):
            wechat.export_backup(self.source,self.output,self.work,progress=events.append)
        export=next(e for e in events if isinstance(e,ProgressEvent) and e.stage.startswith('导出微信消息') and e.state=='completed')
        rewrite=next(e for e in events if isinstance(e,ProgressEvent) and e.stage=='更新微信消息媒体引用' and e.state=='completed')
        self.assertEqual((export.completed,export.total,export.unit),(2,2,'条'))
        self.assertEqual((rewrite.completed,rewrite.total,rewrite.unit),(2,2,'条'))
        rows=self.rows(uid);self.assertEqual([r['_wechat_raw']['msgId'] for r in rows],[1,2])
        self.assertEqual((self.output/rows[0]['_export']['original_media'][0]).read_bytes(),original)
        self.assertEqual(tree_hashes(self.source),before)

    def test_cancel_during_reference_rewrite_retains_full_previous_messages_and_no_success_event(self):
        uid,_=self.make_account([message(1,type=3,imgPath='picture'),message(2)])
        self.media(uid,'image2/picture',b'\xff\xd8\xfforiginal')
        cancelled=threading.Event();events=[]
        def progress(event):
            events.append(event)
            if isinstance(event,ProgressEvent) and event.stage=='更新微信消息媒体引用' and '开始' in event:
                cancelled.set()
        def previews(items,*args,**kwargs):return {items[0]['export_path']:'attachments/wechat/images/preview.png'},[]
        with patch.object(wechat,'_playable_images',side_effect=previews):
            with self.assertRaisesRegex(ExportError,'取消'):
                wechat.export_backup(self.source,self.output,self.work,progress=progress,cancel=cancelled.is_set)
        self.assertEqual(len(self.rows(uid)),2)
        ending=events[-1]
        self.assertEqual((ending.stage,ending.state),('更新微信消息媒体引用','cancelled'))
        self.assertFalse(any(e.state=='completed' for e in events if isinstance(e,ProgressEvent) and e.stage==ending.stage))

    def test_plaintext_direction_group_raw_values_and_unknown_type(self):
        originals = [message(1, isSend=1), message(2),
                     message(3, talker="room@chatroom", content="person:\n群消息\n第二行"),
                     message(4, type=123456, content="未识别类型仍保留")]
        uid, _ = self.make_account(originals)
        before = tree_hashes(self.source)
        result = wechat.export_backup(self.source, self.output, self.work, progress=lambda _: None)
        rows = self.rows(uid)
        self.assertEqual(result["summary"]["accounts"][0]["message_count"], 4)
        self.assertEqual([r["sender"] for r in rows], [uid, "peer", "person", "peer"])
        self.assertEqual(rows[0]["_export"]["sender_name"], "我")
        self.assertEqual(rows[1]["_export"]["sender_name"], "好友备注")
        self.assertEqual(rows[2]["_export"]["text"], "群消息\n第二行")
        self.assertTrue(rows[2]["_export"]["is_group"])
        self.assertEqual(rows[2]["conversation_id"], "room@chatroom@" + uid)
        for original, row in zip(originals, rows):
            expected = dict(original)
            expected["reserved"] = {"__bytes_base64__": base64.b64encode(original["reserved"]).decode()}
            self.assertEqual(row["_wechat_raw"], expected)
            self.assertEqual(row["msg_server_id"], str(original["msgSvrId"]))
        self.assertEqual(tree_hashes(self.source), before)

    @unittest.skipIf(sqlcipher3 is None, "optional SQLCipher binding is unavailable")
    def test_sqlcipher_4000_no_hmac_key_from_shared_prefs_and_explicit_key(self):
        uin, imei = -123456789, "synthetic-device-123"
        uid, db_path = self.make_account([message(1), message(2, isSend=1)], uin=uin, key=account_key(uin, imei))
        prefs = self.source / "shared_prefs" / "system_config_prefs.xml"
        prefs.parent.mkdir()
        prefs.write_text('<map><int name="default_uin" value="-123456789"/><string name="IMEI">' + imei + '</string></map>')
        self.assertNotEqual(db_path.read_bytes()[:16], b"SQLite format 3\x00")
        before = tree_hashes(self.source)
        result = wechat.inspect_backup(self.source, self.work, progress=lambda _: None)
        self.assertEqual(result["accounts"], [{"uid": uid, "message_count": 2, "status": "readable"}])
        with wechat.open_account(db_path, self.work / "explicit", ["wrong", account_key(uin, imei)]) as db:
            self.assertEqual(db.execute("SELECT msgSvrId FROM message ORDER BY msgId").fetchall()[0][0], 9007199254740998)
        self.assertEqual(tree_hashes(self.source), before)

    @unittest.skipIf(sqlcipher3 is None, "optional SQLCipher binding is unavailable")
    def test_negative_binary_uin_mmkv_both_endian_encodings(self):
        for offset, byteorder in enumerate(("<i", ">i")):
            with self.subTest(byteorder=byteorder):
                uin = -1789234567 + offset
                source = self.root / ("binary-" + str(offset)) / "com.tencent.mm"
                uid, _ = self.make_account([message(1)], source=source, uin=uin, key=account_key(uin))
                mmkv = source / "files" / "mmkv" / "system_config_prefs"
                mmkv.parent.mkdir(parents=True)
                mmkv.write_bytes(b"\x09default_uin\x04" + struct.pack(byteorder, uin) + b"\x00suffix")
                before = tree_hashes(source)
                result = wechat.inspect_backup(source, self.work / str(offset), progress=lambda _: None)
                self.assertEqual(result["accounts"][0]["uid"], uid)
                self.assertEqual(result["accounts"][0]["message_count"], 1)
                self.assertEqual(tree_hashes(source), before)

    @unittest.skipIf(sqlcipher3 is None, "optional SQLCipher binding is unavailable")
    def test_cli_key_options_decrypt_and_do_not_echo_secrets(self):
        from duoshan_export.cli import main
        uin, imei = -1678123456, "test-secret-device"
        uid, _ = self.make_account([message(1)], uin=uin, key=account_key(uin, imei))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["scan", str(self.source), "--inspect", "--work-dir", str(self.work),
                           "--wechat-uin", str(uin), "--wechat-imei", imei, "--json"])
        self.assertEqual(status, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["sources"][0]["accounts"][0]["uid"], uid)
        self.assertNotIn(imei, out.getvalue() + err.getvalue())
        self.assertNotIn(str(uin), out.getvalue() + err.getvalue())
        self.assertNotIn(account_key(uin, imei), out.getvalue() + err.getvalue())

    def test_committed_wal_rows_are_read_from_private_snapshot(self):
        uid, path = self.make_account([message(1)])
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(INSERT, message(2, isSend=1))
            writer.commit()
            self.assertTrue(Path(str(path) + "-wal").is_file())
            before = tree_hashes(self.source)
            result = wechat.inspect_backup(self.source, self.work, progress=lambda _: None)
            self.assertEqual(result["accounts"][0]["message_count"], 2)
            wechat.export_backup(self.source, self.output, self.work, progress=lambda _: None)
            self.assertEqual(len(self.rows(uid)), 2)
            self.assertEqual(tree_hashes(self.source), before)

    def test_media_tables_msg_prefix_and_account_isolation(self):
        first, first_db = self.make_account([message(1, type=3), message(2, type=43), message(3, type=34), message(4, type=49)])
        second, _ = self.make_account([message(1, type=3, imgPath="same")], uin=987654321)
        with closing(sqlite3.connect(first_db)) as db:
            db.execute("INSERT INTO ImgInfo2 VALUES (?,?,?,?)", (1, "same", "", ""))
            db.execute("INSERT INTO videoinfo2 VALUES (?,?,?)", (2, "", "movie"))
            db.execute("INSERT INTO voiceinfo VALUES (?,?)", (3, "voice"))
            db.execute("INSERT INTO appattach VALUES (?,?)", (4, "/old/path/document.pdf"))
            db.commit()
        expected = [b"\xff\xd8\xfffirst-image", b"\x00\x00\x00\x18ftypmp42movie", b"RIFF\x00\x00\x00\x00WAVEvoice", b"%PDF-document"]
        for relative, value in zip(["image2/ab/cd/same", "video/movie.mp4", "voice2/ab/cd/msg_voice.wav", "attachment/document.pdf"], expected):
            self.media(first, relative, value)
        second_bytes = b"\x89PNGsecond-image"
        self.media(second, "image2/ab/cd/same", second_bytes)
        self.media(first, "image2/ab/cd/unreferenced", b"\xff\xd8\xffunused")
        before = tree_hashes(self.source)
        external = self.root / "library"
        result = wechat.export_backup(self.source, self.output, self.work, media_output=external, progress=lambda _: None)
        self.assertEqual(result["summary"]["media_assets"], 5)
        for row, value in zip(self.rows(first), expected):
            paths = row["_export"]["local_media"]
            self.assertEqual(len(paths), 1)
            self.assertEqual((external / paths[0]).read_bytes(), value)
            self.assertEqual(row["_export"]["media_status"], "available")
        other = self.rows(second)[0]
        self.assertEqual((external / other["_export"]["local_media"][0]).read_bytes(), second_bytes)
        self.assertNotEqual(other["conversation_id"], self.rows(first)[0]["conversation_id"])
        self.assertFalse((self.output / "attachments").exists())
        self.assertEqual(tree_hashes(self.source), before)

    def test_zstd_archive_streams_only_referenced_assets_and_skips_unsafe_members(self):
        import zstandard
        uid, _ = self.make_account([message(1, type=3, imgPath="picture")])
        expected = b"\xff\xd8\xffarchive-image"
        self.media(uid, "image2/aa/bb/picture", expected)
        self.media(uid, "image2/aa/bb/unreferenced", b"\xff\xd8\xffunused")
        packed = io.BytesIO()
        with tarfile.open(fileobj=packed, mode="w") as archive:
            for path in sorted(self.source.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname="com.tencent.mm/" + path.relative_to(self.source).as_posix())
            for name in ("../escaped/EnMicroMsg.db", "/absolute/EnMicroMsg.db", "C:/drive/EnMicroMsg.db"):
                member = tarfile.TarInfo(name)
                member.size = 3
                archive.addfile(member, io.BytesIO(b"bad"))
            symlink = tarfile.TarInfo("com.tencent.mm/MicroMsg/" + "b" * 32 + "/EnMicroMsg.db")
            symlink.type = tarfile.SYMTYPE
            symlink.linkname = "../../outside"
            archive.addfile(symlink)
        source = self.root / "user_0.tar.zst"
        source.write_bytes(zstandard.ZstdCompressor().compress(packed.getvalue()))
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        wechat.export_backup(source, self.output, self.work, progress=lambda _: None)
        row = self.rows(uid)[0]
        self.assertEqual((self.output / row["_export"]["local_media"][0]).read_bytes(), expected)
        self.assertEqual(len(list((self.output / "attachments").rglob("*.jpg"))), 1)
        self.assertFalse(list(self.work.rglob("picture")))
        self.assertFalse((self.root / "escaped").exists())
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_stable_server_key_across_snapshots_and_local_fallback(self):
        original = message(1)
        newer = dict(original, msgId=999, content="updated local representation", imgPath="added-media")
        self.assertEqual(wechat.source_key(original), wechat.source_key(newer))
        self.assertNotEqual(wechat.source_key(original), wechat.source_key(dict(original, talker="other-peer")))
        local = dict(original, msgSvrId=0)
        self.assertEqual(wechat.source_key(local), wechat.source_key(dict(local, imgPath="new-cache")))
        self.assertNotEqual(wechat.source_key(local), wechat.source_key(dict(local, content="different unsent message")))

    def test_actual_wechat_incremental_reader_import_retains_old_history(self):
        uid, _ = self.make_account([message(1, isSend=1), message(2)])
        before = tree_hashes(self.source)
        library = self.root / "merged"
        first = import_sources([self.source], library, self.work, progress=lambda _: None)
        repeated = import_sources([self.source], library, self.work, progress=lambda _: None)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(repeated["imports"][0]["status"], "unchanged")
        newer = self.root / "newer" / "com.tencent.mm"
        self.make_account([message(999, msgSvrId=9007199254740998, isSend=1), message(3)], source=newer)
        import_sources([newer], library, self.work, progress=lambda _: None)
        with closing(sqlite3.connect(library / "reader.sqlite")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE uid=?", (uid,)).fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT directory FROM library_accounts WHERE uid=?", (uid,)).fetchone()[0], "wx_" + uid)
            self.assertEqual(db.execute("SELECT count(*) FROM import_message_aliases WHERE uid=?", (uid,)).fetchone()[0], 3)
        self.assertEqual(len(self.rows("wx_" + uid, library)), 3)
        self.assertEqual(tree_hashes(self.source), before)

    def test_unknown_schema_and_source_overlap_fail_explicitly(self):
        uid, path = self.make_account([message(1)])
        with closing(sqlite3.connect(path)) as db:
            db.execute("ALTER TABLE message RENAME TO unrelated")
            db.commit()
        before = tree_hashes(self.source)
        with self.assertRaisesRegex(ExportError, "不支持"):
            wechat.inspect_backup(self.source, self.work, progress=lambda _: None)
        with self.assertRaisesRegex(ExportError, "分开"):
            wechat.export_backup(self.source, self.source / "output", self.work, progress=lambda _: None)
        with self.assertRaisesRegex(ExportError, "分开"):
            wechat.prepare_backup(self.source, self.source / "work", progress=lambda _: None)
        self.assertEqual(tree_hashes(self.source), before)

    @unittest.skipIf(sqlcipher3 is None, "optional SQLCipher binding is unavailable")
    def test_missing_or_wrong_key_fail_without_empty_success(self):
        _, path = self.make_account([message(1)], key="123abcd")
        before = tree_hashes(self.source)
        with self.assertRaisesRegex(ExportError, "密钥材料"):
            wechat.inspect_backup(self.source, self.work, progress=lambda _: None)
        with self.assertRaisesRegex(ExportError, "解密失败"):
            with wechat.open_account(path, self.work / "wrong", ["765wxyz"]):
                self.fail("wrong key accepted")
        self.assertEqual(tree_hashes(self.source), before)

    def test_existing_media_same_size_corruption_is_rejected(self):
        uid, _ = self.make_account([message(1, type=3, imgPath="picture")])
        self.media(uid, "image2/picture", b"\xff\xd8\xffvalid")
        wechat.export_backup(self.source, self.output, self.work, progress=lambda _: None)
        target = self.output / self.rows(uid)[0]["_export"]["local_media"][0]
        target.write_bytes(b"\xff\xd8\xffother")
        with self.assertRaisesRegex(ExportError, "校验不符"):
            wechat.export_backup(self.source, self.root / "second", self.work, media_output=self.output, progress=lambda _: None)

    def test_corrupted_and_partial_private_snapshots_are_recovered_without_deletion(self):
        _, path = self.make_account([message(1)])
        before = tree_hashes(self.source)
        with wechat.open_account(path, self.work, []) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM message").fetchone()[0], 1)
        cached = next(self.work.rglob("EnMicroMsg.db"))
        for corruption in (b"partial", b"x" * cached.stat().st_size):
            cached.write_bytes(corruption)
            with wechat.open_account(path, self.work, []) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM message").fetchone()[0], 1)
        retained = list(self.work.rglob("EnMicroMsg.db.invalid-*"))
        self.assertEqual(len(retained), 2)
        self.assertEqual(retained[0].parent, cached.parent)
        self.assertEqual(tree_hashes(self.source), before)

    def test_corrupt_archive_metadata_cache_is_reextracted_and_retained(self):
        _, _ = self.make_account([message(1)])
        archive = self.root / "user.tar"
        with tarfile.open(archive, "w") as packed:
            for file in self.source.rglob("*"):
                if file.is_file():
                    packed.add(file, arcname="com.tencent.mm/" + file.relative_to(self.source).as_posix())
        before = archive.read_bytes()
        info = wechat.prepare_backup(archive, self.work, progress=lambda _: None)
        cached = next(Path(info["root"]).rglob("EnMicroMsg.db"))
        cached.write_bytes(b"broken cache")
        result = wechat.inspect_backup(archive, self.work, progress=lambda _: None)
        self.assertEqual(result["accounts"][0]["message_count"], 1)
        self.assertEqual(len(list(Path(info["root"]).rglob("EnMicroMsg.db.unverified-*"))), 1)
        self.assertEqual(archive.read_bytes(), before)

    def test_media_output_overlap_is_rejected_before_creating_outputs(self):
        self.make_account([message(1)])
        before = tree_hashes(self.source)
        with self.assertRaisesRegex(ExportError, "媒体目录"):
            wechat.export_backup(self.source, self.output, self.work, media_output=self.source / "media", progress=lambda _: None)
        self.assertFalse(self.output.exists())
        self.assertEqual(tree_hashes(self.source), before)

    def test_real_silk_codec_produces_playable_wav_and_retains_original(self):
        uid, _ = self.make_account([message(1, type=34, imgPath="voice")])
        voice = self.media(uid, "voice2/ab/cd/msg_voice.amr", b"")
        code = """
import { encode } from 'silk-wasm';
import { writeFileSync } from 'node:fs';
const pcm = Buffer.alloc(24000 * 2);
for (let i = 0; i < 24000; i++) pcm.writeInt16LE(Math.round(6000 * Math.sin(i * 2 * Math.PI * 440 / 24000)), i * 2);
const encoded = await encode(pcm, 24000);
writeFileSync(process.argv[1], encoded.data);
"""
        encoded = subprocess.run(["node", "--input-type=module", "-e", code, str(voice)], cwd=ROOT / "web", capture_output=True, text=True)
        self.assertEqual(encoded.returncode, 0, encoded.stderr)
        original = voice.read_bytes()
        before = tree_hashes(self.source)
        result = wechat.export_backup(self.source, self.output, self.work, progress=lambda _: None)
        self.assertEqual(result["summary"]["warnings"], [])
        row = self.rows(uid)[0]
        saved = row["_export"]["original_media"]
        self.assertEqual(len(saved), 1)
        self.assertEqual((self.output / saved[0]).read_bytes(), original)
        playable = self.output / row["_export"]["local_media"][0]
        self.assertEqual(playable.suffix, ".wav")
        with wave.open(str(playable), "rb") as wav:
            self.assertEqual(wav.getframerate(), 24000)
            self.assertEqual(wav.getnchannels(), 1)
            self.assertGreaterEqual(wav.getnframes(), 23000)
            self.assertNotEqual(set(wav.readframes(wav.getnframes())), {0})
        self.assertEqual(tree_hashes(self.source), before)

    @unittest.skipUnless(shutil.which("ffmpeg"), "optional FFmpeg is unavailable")
    def test_wxgf_hevc_image_has_png_preview_and_preserves_original_bytes(self):
        from PIL import Image
        uid, _ = self.make_account([message(1, type=3, imgPath="picture")])
        encoded = self.root / "frame.hevc"
        process = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=c=red:s=64x64:r=1", "-frames:v", "1", "-c:v", "libx265",
            "-x265-params", "log-level=error:pools=1", "-f", "hevc", str(encoded)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        original = b"wxgf" + bytes(28) + encoded.read_bytes()
        self.media(uid, "image2/ab/cd/picture.jpg", original)
        before = tree_hashes(self.source)
        result = wechat.export_backup(self.source, self.output, self.work, progress=lambda _: None)
        self.assertEqual(result["summary"]["warnings"], [])
        meta = self.rows(uid)[0]["_export"]
        self.assertEqual(len(meta["image_previews"]), 1)
        self.assertEqual(meta["local_media"], meta["image_previews"])
        self.assertEqual((self.output / meta["original_media"][0]).read_bytes(), original)
        self.assertTrue(meta["original_media"][0].endswith(".wxgf"))
        with Image.open(self.output / meta["image_previews"][0]) as image:
            image.load()
            self.assertEqual(image.size, (64, 64))
            self.assertEqual(image.format, "PNG")
            red, green, blue = image.convert("RGB").getpixel((32, 32))
            self.assertGreater(red, 200)
            self.assertLess(max(green, blue), 50)
        self.assertEqual(tree_hashes(self.source), before)


if __name__ == "__main__":
    unittest.main()
