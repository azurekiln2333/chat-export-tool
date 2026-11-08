"""Independent account-cache media recovery fixtures; no personal data."""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import wave

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from duoshan_export.apps.desktop_media import DesktopMediaIndex, MediaReference, desktop_media_files, extract_qqnt_media, reference_id
from duoshan_export.errors import ExportError


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return (varint((number << 3) | 2) + varint(len(value)) + value) if isinstance(value, bytes) else varint(number << 3) + varint(value)


class DesktopMediaTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="desktop-media-"))
        self.source = self.root / "Tencent Files/123456789"
        database = self.source / "nt_qq/nt_db/nt_msg.db"
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as db:
            db.execute('CREATE TABLE c2c_msg_table("40800" BLOB)')
        self.output, self.work = self.root / "library", self.root / "work"
        self.logs = []

    def picture(self, color="red", size=(8, 9), format="PNG"):
        buffer = io.BytesIO()
        Image.new("RGB", size, color).save(buffer, format=format)
        return buffer.getvalue()

    def put(self, relative, data):
        target = self.source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def index(self):
        return DesktopMediaIndex(self.source, self.output, self.work, progress=self.logs.append)

    def record(self, blob=None):
        return {"msg_uuid": "fixture", "_desktop_raw": {"row": {"40800": {"encoding": "base64", "data": base64.b64encode(blob or b"").decode()}}},
                "_export": {"text": "[图片]", "local_media": []}}

    def test_extract_only_media_elements_keeps_md5_paths_and_filename(self):
        md5 = "123456789abcdef0123456789abcdef0"
        media = field(45002, 2) + field(45424, bytes.fromhex(md5)) + field(45402, "example.png") + field(45812, r"C:\Old\Tencent Files\123456789\nt_qq\nt_data\Pic\2025-01\Ori\example.png")
        text = field(45002, 1) + field(45101, "do not open C:/private/picture.png")
        refs = extract_qqnt_media(field(40800, media) + field(40800, text))
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].kind, "image")
        self.assertEqual(refs[0].md5, (md5,))
        self.assertEqual(refs[0].filenames, ("example.png",))
        self.assertEqual(len(refs[0].paths), 1)

    def test_binary_nt_md5_field_and_standard_face_are_distinguished(self):
        digest = bytes.fromhex("123456789abcdef0123456789abcdef0")
        image = field(40800, field(45002, 2) + field(45406, digest))
        standard_face = field(40800, field(45002, 6) + field(47601, 14))
        references = extract_qqnt_media(image + standard_face)
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0].md5, (digest.hex(),))

    def test_quoted_media_is_not_attached_to_outer_message(self):
        quoted_digest, outer_digest = bytes.fromhex("1" * 32), bytes.fromhex("2" * 32)
        quoted_image = field(40800, field(45002, 2) + field(45406, quoted_digest))
        quote = field(47423, quoted_image)
        text_element = field(40800, field(45002, 1) + field(45101, "reply") + quote)
        image_element = field(40800, field(45002, 2) + field(45406, outer_digest) + quote)
        self.assertEqual(extract_qqnt_media(text_element), [])
        references = extract_qqnt_media(image_element)
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0].md5, (outer_digest.hex(),))

    def test_full_image_content_verified_before_library_publication(self):
        data = self.picture()
        md5 = hashlib.md5(data).hexdigest()
        source = self.put(f"nt_qq/nt_data/Pic/2025-01/Ori/{md5}.jpg", data)
        before = source.read_bytes()
        blob = field(40800, field(45002, 2) + field(45424, md5) + field(45402, md5 + ".jpg"))
        row = self.record(blob)
        raw_before = json.dumps(row["_desktop_raw"])
        with self.index() as index:
            meta = index.attach(row)
        self.assertEqual(meta["media_status"], "ok")
        resource = meta["desktop_media"][0]
        self.assertEqual(resource["variant"], "original")
        self.assertEqual(resource["mime"], "image/png")
        self.assertTrue(resource["path"].endswith(".png"))
        self.assertEqual((self.output / resource["path"]).read_bytes(), data)
        self.assertEqual(meta["original_media"], [resource["path"]])
        self.assertEqual(meta["media"]["reference_id"], resource["reference_id"])
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(json.dumps(row["_desktop_raw"]), raw_before)

    def test_missing_original_recovers_thumbnail_without_claiming_original(self):
        original_md5 = "1" * 32
        small = self.picture(size=(4, 4))
        large = self.picture(size=(16, 16))
        self.put(f"nt_qq/nt_data/Pic/2025-01/Thumb/{original_md5}_0.png", small)
        self.put(f"nt_qq/nt_data/Pic/2025-01/Thumb/{original_md5}_720.png", large)
        row = self.record()
        with self.index() as index:
            meta = index.attach(row, [MediaReference("image", md5=(original_md5,))])
        self.assertEqual(meta["media_status"], "thumbnail")
        self.assertEqual(meta["original_media"], [])
        self.assertEqual(meta["desktop_media"][0]["variant"], "thumbnail")
        self.assertEqual(meta["desktop_media"][0]["width"], 16)
        self.assertEqual(meta["text"], "[图片]")

    def test_original_with_matching_name_but_wrong_md5_is_never_published(self):
        wrong = self.picture()
        md5 = "2" * 32
        self.put(f"nt_qq/nt_data/Pic/2025-01/Ori/{md5}.png", wrong)
        with self.index() as index:
            meta = index.attach(self.record(), [MediaReference("image", md5=(md5,))])
        self.assertEqual(meta["media_status"], "missing")
        self.assertEqual(meta["local_media"], [])
        self.assertEqual(list((self.output / "attachments/qq_pc").glob("*/*.png")), [])

    def test_legacy_video_poster_is_marked_thumbnail_when_video_is_missing(self):
        md5 = "3" * 32
        self.put(f"Video/{md5}.png", self.picture())
        with self.index() as index:
            meta = index.attach(self.record(), [MediaReference("video", md5=(md5,))])
        self.assertEqual(meta["media_status"], "thumbnail")
        self.assertEqual(meta["desktop_media"][0]["kind"], "image")
        self.assertEqual(meta["desktop_media"][0]["variant"], "thumbnail")

    def test_content_md5_recovers_renamed_cache_without_official_index(self):
        data = self.picture("blue")
        md5 = hashlib.md5(data).hexdigest()
        self.put("Image/C2C/lost-index-opaque-cache.dat", data)
        with self.index() as index:
            resource = index.resolve(MediaReference("image", md5=(md5,)))
            again = index.resolve(MediaReference("image", md5=(md5,)))
        self.assertEqual(resource["match"], "content_md5")
        self.assertEqual(again["path"], resource["path"])
        self.assertEqual((self.output / resource["path"]).read_bytes(), data)

    def test_recorded_old_root_path_maps_only_same_account_allowed_media(self):
        data = self.picture("green")
        self.put("Image/C2C/known.png", data)
        self.put("db_storage/private.png", data)
        sibling = self.root / "Tencent Files/987654321/Image/C2C/known.png"
        sibling.parent.mkdir(parents=True)
        sibling.write_bytes(data)
        with self.index() as index:
            hit = index.resolve(MediaReference("image", paths=(r"C:\old\Tencent Files\123456789\Image\C2C\known.png",)))
            self.assertIsNotNone(hit)
            for path in (str(sibling), r"Image\..\db_storage\private.png", "db_storage/private.png", "https://host/image.png"):
                self.assertIsNone(index.resolve(MediaReference("image", paths=(path,))))

    def test_ambiguous_basename_does_not_guess_between_different_files(self):
        self.put("Image/C2C/first/image.png", self.picture())
        self.put("Image/C2C/second/image.png", self.picture("blue"))
        with self.index() as index:
            self.assertIsNone(index.resolve(MediaReference("image", filenames=("image.png",))))

    def test_unique_generic_name_without_identifier_does_not_guess(self):
        self.put("Image/C2C/image.png", self.picture())
        with self.index() as index:
            self.assertIsNone(index.resolve(MediaReference("image", filenames=("image.png",))))

    def test_legacy_refs_attach_without_touching_original_elements(self):
        data = self.picture()
        md5 = hashlib.md5(data).hexdigest()
        self.put(f"Image/C2C/{md5}.png", data)
        row = self.record()
        refs = [{"kind": "图片", "element_type": 3, "hash": md5}]
        row["_export"]["legacy_media_refs"] = refs
        with self.index() as index:
            meta = index.attach(row)
        self.assertEqual(meta["media_status"], "ok")
        self.assertEqual(meta["legacy_media_refs"], refs)

    def test_image_in_file_cache_and_multiple_message_resources(self):
        first, second = self.picture(), self.picture("blue")
        self.put("FileRecv/first.png", first)
        self.put("Image/C2C/second.png", second)
        with self.index() as index:
            meta = index.attach(self.record(), [MediaReference("image", paths=("FileRecv/first.png",)),
                                                MediaReference("image", paths=("Image/C2C/second.png",)),
                                                MediaReference("image", md5=("e" * 32,))])
        self.assertEqual(meta["media_status"], "partial")
        self.assertEqual(len(meta["local_media"]), 2)
        self.assertEqual(meta["desktop_media_summary"], {"unresolved": 1, "requested": 3})

    def test_enumeration_excludes_siblings_avatars_databases_and_source_overlap(self):
        image = self.put("Image/C2C/cache.png", self.picture())
        self.put("nt_qq/nt_data/avatar/private.png", self.picture())
        self.put("OtherCache/unrelated.png", self.picture())
        self.assertEqual(list(desktop_media_files(self.source)), [image])
        with self.assertRaisesRegex(ExportError, "重叠"):
            DesktopMediaIndex(self.source, self.source / "output", self.work, progress=lambda _: None)
        self.assertFalse((self.source / "output").exists())

    def test_image_format_validation_rejects_truncated_bytes(self):
        truncated = self.picture()[:25]
        md5 = hashlib.md5(truncated).hexdigest()
        self.put(f"Image/{md5}.png", truncated)
        with self.index() as index:
            self.assertIsNone(index.resolve(MediaReference("image", md5=(md5,))))
        self.assertTrue(any((self.output / "attachments/qq_pc/.incoming").rglob("*.partial")))

    def test_cancel_stops_indexing_without_source_changes(self):
        path = self.put("Image/cache.png", self.picture())
        before = path.read_bytes()
        with self.assertRaisesRegex(ExportError, "取消"):
            DesktopMediaIndex(self.source, self.output, self.work, cancel=lambda: True, progress=lambda _: None)
        self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(shutil.which("node") and (ROOT / "web/node_modules/silk-wasm").is_dir(), "Local SILK encoder required")
    def test_silk_voice_keeps_original_and_adds_verified_playback_wav(self):
        fixture = self.root / "generated.silk"
        script = """
import fs from 'node:fs';
import { encode } from 'silk-wasm';
const pcm = Buffer.alloc(24000);
for(let i=0;i<12000;i++) pcm.writeInt16LE(Math.round(Math.sin(i*2*Math.PI*440/24000)*4000),i*2);
const silk = await encode(pcm,24000);
fs.writeFileSync(process.argv[1],silk.data,{flag:'wx'});
"""
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run(["node", "--input-type=module", "-e", script, str(fixture)], cwd=ROOT / "web",
                                env={**os.environ, "TEMP": str(self.root), "TMP": str(self.root)},
                                capture_output=True, timeout=60, **options)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        raw = fixture.read_bytes()
        digest = hashlib.md5(raw).hexdigest()
        original = self.put(f"nt_qq/nt_data/Ptt/2025-01/Ori/{digest}.amr", raw)
        with self.index() as index:
            meta = index.attach(self.record(), [MediaReference("voice", md5=(digest,))])
            self.assertEqual(index.counts["voice_playback_files"], 1)
        self.assertEqual(meta["desktop_media"][0]["variant"], "playback")
        self.assertEqual(meta["media"]["mime"], "audio/wav")
        self.assertNotEqual(meta["local_media"], meta["original_media"])
        self.assertEqual((self.output / meta["original_media"][0]).read_bytes(), raw)
        with wave.open(str(self.output / meta["local_media"][0]), "rb") as decoded:
            self.assertGreater(decoded.getnframes(), 10000)
            self.assertEqual(decoded.getframerate(), 24000)
        self.assertEqual(original.read_bytes(), raw)

    def test_undecodable_voice_remains_downloadable_and_keeps_original(self):
        for broken_frames in (b"", b"\x01", b"\x05\x00abc", b"\xff\x7fabc", b"\xff\xfftrailing"):
            with self.subTest(frames=broken_frames.hex()):
                raw = b"\x02#!SILK_V3" + broken_frames
                digest = hashlib.md5(raw).hexdigest()
                self.put(f"Audio/{digest}.amr", raw)
                with self.index() as index:
                    meta = index.attach(self.record(), [MediaReference("voice", md5=(digest,))])
                    self.assertIsNone(index.voice_process)
                    self.assertEqual(index.counts["voice_playback_failed"], 1)
                self.assertFalse(meta["media"]["playable"])
                self.assertEqual(meta["media"]["kind"], "file")
                self.assertEqual(meta["media"]["original_kind"], "voice")
                self.assertEqual((self.output / meta["local_media"][0]).read_bytes(), raw)

    def test_reference_identity_is_stable_across_cache_path_and_thumbnail_changes(self):
        digest = "a" * 32
        original = MediaReference("image", md5=(digest,), paths=(r"C:\old\original.jpg",))
        thumbnail = MediaReference("image", identifiers=(digest.upper(),), paths=(r"D:\new\thumb.jpg",))
        self.assertEqual(reference_id(original), reference_id(thumbnail))
        self.assertNotEqual(reference_id(original), reference_id(MediaReference("video", md5=(digest,))))


if __name__ == "__main__":
    unittest.main()
