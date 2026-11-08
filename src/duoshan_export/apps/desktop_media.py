"""Recover QQ media from an account's bounded local cache, without networking.

The original client indexes are optional. Recorded paths, content MD5 and QQ's
hash-named cache variants supply independent matches. Files are decoded only
after verified copying into the destination volume. No source file is changed.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PureWindowsPath
import queue
import re
import subprocess
import threading
import time
from uuid import uuid4
import wave

from ..errors import ExportError
from ..progress import StageReporter

MEDIA_ROOTS = {
    "nt_qq/nt_data/Pic": "image", "nt_qq/nt_data/Emoji": "emoji",
    "nt_qq/nt_data/Ptt": "voice", "nt_qq/nt_data/Video": "video", "nt_qq/nt_data/File": "file",
    "nt_data/Pic": "image", "nt_data/Emoji": "emoji", "nt_data/Ptt": "voice",
    "nt_data/Video": "video", "nt_data/File": "file",
    "Image": "image", "Image2": "image", "Audio": "voice", "Video": "video", "FileRecv": "file",
}
HASH_TOKEN = re.compile(r"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])", re.I)
KINDS = {2: "image", 3: "file", 4: "voice", 5: "video", 6: "emoji"}


def _check(cancel):
    if cancel and cancel():
        raise ExportError("QQ 媒体恢复已取消；已复制资源与工作文件保留。")


def _linked(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _root(source):
    from ..desktop_sources import discover_desktop_sources
    root = Path(source["source"] if isinstance(source, dict) else source).expanduser().resolve()
    found = discover_desktop_sources([root])
    selected = [item for item in found if item["app"] == "qq_pc"]
    if len(selected) != 1:
        return None
    return Path(selected[0]["source"])


def _media_roots(root):
    for name, kind in MEDIA_ROOTS.items():
        folder = root / name
        if folder.is_dir() and not _linked(folder) and folder.resolve().is_relative_to(root):
            yield folder, kind


def _walk(folder, root, cancel=None):
    pending = [folder]
    while pending:
        _check(cancel)
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                _check(cancel)
                path = Path(entry.path)
                if _linked(path) or not path.resolve().is_relative_to(root):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path


def desktop_media_files(source, *, cancel=None):
    """Yield only this QQ account's explicit media roots, excluding junctions."""
    root = _root(source)
    if root is not None:
        for folder, _ in _media_roots(root):
            yield from _walk(folder, root, cancel)


@dataclass(frozen=True)
class MediaReference:
    kind: str
    md5: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    filenames: tuple[str, ...] = ()
    size: int | None = None


def reference_id(reference):
    if isinstance(reference, dict):
        reference = MediaReference(**reference)
    hashes = reference.md5 or reference.identifiers
    evidence = sorted(h.lower() for h in hashes) if hashes else sorted(p.replace("\\", "/").casefold() for p in reference.paths or reference.filenames)
    encoded = json.dumps([reference.kind, "hash" if hashes else "path", evidence], ensure_ascii=False, separators=(",", ":"))
    return "qq_media_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _leaves(blob, depth=0):
    from .desktop import protobuf_fields
    if depth > 7:
        return
    for field, wire, value in protobuf_fields(blob):
        if field == 47423:
            continue  # Quoted messages are evidence, not this message's media.
        yield field, wire, value
        if wire == 2 and isinstance(value, bytes):
            yield from _leaves(value, depth + 1)


def extract_qqnt_media(blob):
    """Read media elements, not arbitrary URLs or filenames mentioned in text.

    QQ NT: 40800 repeated elements, 45002 kind, 45402 filename, 45406
    binary MD5 / 45424 textual MD5. Other fields carry version-dependent paths
    (including 45403 / 45812). Their values are used only inside media elements.
    """
    from .desktop import protobuf_fields
    fields = protobuf_fields(blob)
    elements = [v for f, w, v in fields if f == 40800 and w == 2]
    if not elements and any(f == 45002 for f, _, _ in fields):
        elements = [blob]
    result = []
    for element in elements:
        leaves = list(_leaves(element))
        kind = next((KINDS[v] for f, w, v in leaves if f == 45002 and w == 0 and v in KINDS), None)
        if kind is None:
            continue
        md5, identifiers, paths, names = [], [], [], []
        for field, wire, value in leaves:
            if wire != 2 or not isinstance(value, bytes):
                continue
            if field in {45406, 45424} and len(value) == 16:
                md5.append(value.hex())
                identifiers.append(value.hex())
            try:
                text = value.decode("utf-8").strip("\x00")
            except UnicodeDecodeError:
                continue
            if not text or len(text) > 4096 or any(ord(ch) < 32 for ch in text):
                continue
            hashes = HASH_TOKEN.findall(text)
            identifiers.extend(h.lower() for h in hashes)
            if field in {45406, 45424} and re.fullmatch("[a-fA-F0-9]{32}", text):
                md5.append(text.lower())
            if "://" in text:
                continue  # Recorded CDN hashes may help; no network access.
            if "/" in text or "\\" in text:
                paths.append(text)
            elif field == 45402:
                names.append(text)
        unique = lambda values: tuple(dict.fromkeys(values))
        if kind == "emoji" and not (md5 or identifiers or paths or names):
            continue  # Standard numbered QQ faces have no local media resource.
        result.append(MediaReference(kind, unique(md5), unique(identifiers), unique(paths), unique(names)))
    return result


def extract_legacy_media(references):
    """Translate the independently decoded Msg3.0 element evidence."""
    kinds = {"图片": "image", "语音": "voice", "视频": "video", "表情": "emoji", "文件": "file"}
    result = []
    for value in references if isinstance(references, list) else ():
        if not isinstance(value, dict):
            continue
        kind = kinds.get(value.get("kind"), value.get("kind"))
        if kind not in KINDS.values():
            continue
        digest = str(value.get("hash") or "").lower()
        hashes = (digest,) if re.fullmatch(r"[a-f0-9]{32}", digest) else ()
        path = str(value.get("recorded_path") or "")
        # Legacy hash fields can be media identifiers rather than MD5. A
        # non-thumbnail original still must match its content hash unless its
        # exact recorded local path supplies independent evidence.
        result.append(MediaReference(kind, identifiers=hashes, paths=(path,) if path else ()))
    return result


@dataclass(frozen=True)
class _Asset:
    path: Path
    relative: str
    kind: str
    size: int
    mtime: int
    thumbnail: bool
    identifiers: tuple[str, ...]


def _kind_matches(wanted, actual):
    return wanted == "file" or wanted == actual or {wanted, actual} <= {"image", "emoji"}


def _cache_matches(wanted, actual):
    # FileRecv/File can contain downloaded pictures, audio and videos too.
    return actual == "file" or _kind_matches(wanted, actual)


def _hashes(path, cancel=None, stage=None):
    md5, sha = hashlib.md5(), hashlib.sha256()
    processed = 0
    with path.open("rb") as stream:
        while True:
            _check(cancel)
            data = stream.read(4 * 1024 * 1024)
            if not data:
                break
            md5.update(data)
            sha.update(data)
            processed += len(data)
            if stage:
                stage.update(completed=processed)
    return md5.hexdigest(), sha.hexdigest()


def _validate_silk_for_decoder(path, cancel=None):
    """Reject incomplete packets before the WASM decoder's unchecked reads.

    silk-wasm's libSilkCodec decoder preloads two packets for its FEC buffer
    and permits five 250-byte frames per packet. Validate that envelope in
    the copied file; a successful PCM output alone is not evidence that a
    truncated source contained audio.
    """
    with path.open("rb") as stream:
        if stream.read(10) != b"\x02#!SILK_V3":
            raise ExportError("QQ SILK 文件头不受播放转换支持，原始文件保留。")
        packets = 0
        while True:
            _check(cancel)
            header = stream.read(2)
            if not header:
                break
            if len(header) != 2:
                raise ExportError("QQ SILK 语音帧头不完整，原始文件保留。")
            size = int.from_bytes(header, "little", signed=True)
            if size == -1 and not stream.read(1):
                break
            if not 0 < size <= 1250 or len(stream.read(size)) != size:
                raise ExportError("QQ SILK 语音帧不完整，原始文件保留。")
            packets += 1
        if packets < 2:
            raise ExportError("QQ SILK 语音缺少可验证的完整帧，原始文件保留。")


def _inspect_copy(path, expected):
    """Inspect destination bytes only; never make an invalid image look usable."""
    from PIL import Image
    with path.open("rb") as stream:
        head = stream.read(64)
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            fmt = image.format
            width, height = image.size
        extensions = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp",
                      "BMP": ".bmp", "TIFF": ".tiff", "AVIF": ".avif", "HEIF": ".heic"}
        return {"kind": "image", "mime": Image.MIME.get(fmt, "image/" + fmt.lower()),
                "extension": extensions.get(fmt, "." + fmt.lower()), "width": width, "height": height}
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        if expected in {"image", "emoji"}:
            raise ExportError("本地图片校验失败，保留已复制文件。") from None
    if b"SILK_V3" in head[:20]:
        return {"kind": "voice", "mime": "audio/silk", "extension": ".silk", "playable": False}
    if head.startswith((b"#!AMR\n", b"#!AMR-WB\n")):
        return {"kind": "voice", "mime": "audio/amr", "extension": ".amr", "playable": False}
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return {"kind": "voice", "mime": "audio/wav", "extension": ".wav", "playable": True}
    if head[4:8] == b"ftyp":
        return {"kind": "video", "mime": "video/mp4", "extension": ".mp4"}
    if expected == "file":
        return {"kind": "file", "mime": "application/octet-stream", "extension": ".bin"}
    raise ExportError("本地媒体格式尚不能验证；原始文件保留。")


class DesktopMediaIndex:
    """Account-local cache matching and verified publication into a library."""

    def __init__(self, account_root, output, work, *, progress=print, cancel=None):
        self.root = _root(account_root)
        if self.root is None:
            raise ExportError("QQ 媒体恢复需要单个已识别账号目录。")
        self.output, self.work = Path(output).resolve(), Path(work).resolve()
        for destination in (self.output, self.work):
            if destination == self.root or destination.is_relative_to(self.root) or self.root.is_relative_to(destination):
                raise ExportError("QQ 媒体目标/工作目录不能与原始账号目录重叠。")
        self.progress, self.cancel = progress, cancel
        self.assets, self.by_id, self.by_name, self.by_relative = [], defaultdict(list), defaultdict(list), {}
        self.by_content, self.content_scanned, self.published, self.failures = defaultdict(list), set(), {}, {}
        self.content_hashed = set()
        self.counts = Counter()
        self.voice_process = None
        self.voice_responses = None
        self.voice_thread = None
        self.voice_cache = {}
        self.run = uuid4().hex
        self.incoming = self.output / "attachments/qq_pc/.incoming" / self.run
        self._destination(self.incoming).mkdir(parents=True, exist_ok=False)
        self.work.mkdir(parents=True, exist_ok=True)
        self.journal = (self.work / ("qq-media-" + self.run + ".jsonl")).open("x", encoding="utf-8")
        try:
            with StageReporter(progress, "扫描账号 QQ 本地媒体缓存", completed=0, unit="文件", cancel=cancel) as stage:
                for folder, kind in _media_roots(self.root):
                    for path in _walk(folder, self.root, cancel):
                        info = path.stat()
                        if not info.st_size:
                            continue
                        relative = path.relative_to(self.root).as_posix()
                        ids = tuple(h.lower() for h in HASH_TOKEN.findall(path.stem))
                        thumbnail = any(p.casefold() in {"thumb", "thumbnail", "thumbs"} for p in path.parts)
                        thumbnail |= bool(re.search(r"[0-9a-f]{32}_(?:0|\d{2,4})(?:\.|$)", path.name, re.I))
                        # Traditional QQ keeps video poster images beside the
                        # videos, without a Thumb subdirectory or suffix.
                        thumbnail |= kind == "video" and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
                        asset = _Asset(path, relative, kind, info.st_size, info.st_mtime_ns, thumbnail, ids)
                        self.assets.append(asset)
                        self.by_relative[relative.casefold()] = asset
                        self.by_name[path.name.casefold()].append(asset)
                        for key in ids:
                            self.by_id[key].append(asset)
                        stage.update(completed=len(self.assets))
            self.counts["indexed_files"] = len(self.assets)
        except Exception:
            self.journal.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._stop_voice_decoder()
        if not self.journal.closed:
            self.journal.flush()
            self.journal.close()

    def _stop_voice_decoder(self):
        process, self.voice_process = self.voice_process, None
        if process is None:
            return
        try:
            process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        finally:
            process.stdout.close()
            if self.voice_thread:
                self.voice_thread.join(timeout=3)
                self.voice_thread = None

    def _start_voice_decoder(self):
        script = Path(__file__).resolve().parents[3] / "web/server/decode-voice.mjs"
        if not script.is_file():
            raise ExportError("本机缺少 SILK 播放转换程序，原始语音保留。")
        settings = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        environment = os.environ.copy()
        environment.update(TEMP=str(self.work), TMP=str(self.work))
        process = subprocess.Popen(["node", str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, encoding="utf-8", cwd=self.work,
                                   env=environment, **settings)
        self.voice_process, self.voice_responses = process, queue.Queue()
        def receive():
            try:
                for line in process.stdout:
                    self.voice_responses.put(line)
            finally:
                self.voice_responses.put("")
        self.voice_thread = threading.Thread(target=receive, name="qq-voice-decoder", daemon=True)
        self.voice_thread.start()

    def _voice_playback(self, resource):
        if resource.get("kind") != "voice" or resource.get("playable", True):
            return resource
        original = resource["path"]
        if original in self.voice_cache:
            return self.voice_cache[original]
        wav = self._destination(self.incoming / (uuid4().hex + ".wav"))
        try:
            with StageReporter(self.progress, "转换 QQ 语音播放副本", cancel=self.cancel):
                if resource["mime"] == "audio/silk":
                    _validate_silk_for_decoder(self._destination(self.output / original), self.cancel)
                    if self.voice_process is None:
                        self._start_voice_decoder()
                    self.voice_process.stdin.write(json.dumps({"source": str(self.output / original), "output": str(wav)}) + "\n")
                    self.voice_process.stdin.flush()
                    deadline = time.monotonic() + 60
                    while True:
                        _check(self.cancel)
                        if time.monotonic() > deadline:
                            raise ExportError("QQ SILK 语音转换超时，原始文件保留。")
                        try:
                            answer = self.voice_responses.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        if not answer or not json.loads(answer).get("ok"):
                            raise ExportError("QQ SILK 语音转换失败，原始文件保留。")
                        break
                else:
                    settings = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                    environment = {**os.environ, "TEMP": str(self.work), "TMP": str(self.work)}
                    process = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-i", str(self.output / original),
                                                "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-n", str(wav)],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.work,
                                               env=environment, **settings)
                    try:
                        deadline = time.monotonic() + 120
                        while process.poll() is None:
                            _check(self.cancel)
                            if time.monotonic() > deadline:
                                raise ExportError("QQ AMR 语音转换超时，原始文件保留。")
                            try:
                                process.wait(timeout=0.2)
                            except subprocess.TimeoutExpired:
                                pass
                        if process.returncode:
                            raise ExportError("QQ AMR 语音转换失败，原始文件保留。")
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=3)
                with wave.open(str(wav), "rb") as stream:
                    if stream.getnframes() <= 0 or stream.getsampwidth() != 2:
                        raise ExportError("QQ 播放副本不是有效 PCM 音频。")
                    seconds = stream.getnframes() / stream.getframerate()
                md5, digest = _hashes(wav, self.cancel)
                relative = f"attachments/qq_pc/{digest[:2]}/{digest}.wav"
                target = self._destination(self.output / relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if _hashes(target, self.cancel)[1] != digest:
                        raise ExportError("已有 QQ 播放副本校验失败。")
                else:
                    wav.rename(target)
                playback = {"kind": "voice", "mime": "audio/wav", "path": relative, "status": "ok", "playable": True,
                            "sha256": digest, "md5": md5, "size": target.stat().st_size, "duration": seconds,
                            "variant": "playback", "match": "decoded_original", "original_path": original,
                            "original_sha256": resource["sha256"]}
                self.voice_cache[original] = playback
                self.counts["voice_playback_files"] += 1
                self._record({"status": "playback", "resource": playback})
                return playback
        except (OSError, ValueError, ExportError, subprocess.SubprocessError, wave.Error):
            self._stop_voice_decoder()
            _check(self.cancel)
            self.counts["voice_playback_failed"] += 1
            self.progress("QQ 语音播放转换未完成；原始语音已保存，可下载。")
            retained = {**resource, "kind": "file", "mime": "application/octet-stream", "original_kind": "voice",
                        "original_mime": resource["mime"], "playable": False}
            self.voice_cache[original] = retained
            return retained

    def _destination(self, path):
        resolved = path.resolve()
        if not resolved.is_relative_to(self.output) or any(_linked(parent) for parent in (path, *path.parents) if parent.exists() and parent != self.output.parent and parent.is_relative_to(self.output)):
            raise ExportError("QQ 媒体目标越出资料库或包含链接。")
        return path

    def _record(self, data):
        self.journal.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.journal.flush()

    def _path_match(self, value):
        clean = str(value).replace("\\", "/").strip()
        if not clean or "://" in clean or "\x00" in clean or any(p == ".." for p in clean.split("/")):
            return None
        root = self.root.as_posix()
        if clean.casefold().startswith(root.casefold() + "/"):
            clean = clean[len(root) + 1:]
        elif PureWindowsPath(clean).is_absolute() or clean.startswith("/"):
            parts = clean.strip("/").split("/")
            matches = [i for i, part in enumerate(parts) if part.casefold() == self.root.name.casefold()]
            if len(matches) != 1:
                return None
            clean = "/".join(parts[matches[0] + 1:])
        elif clean.casefold().startswith(self.root.name.casefold() + "/"):
            clean = clean[len(self.root.name) + 1:]
        return self.by_relative.get(clean.casefold())

    def _scan_content(self, kind):
        if kind in self.content_scanned:
            return
        candidates = [asset for asset in self.assets if _cache_matches(kind, asset.kind) and not asset.thumbnail
                      and asset.relative not in self.content_hashed]
        total, completed = sum(asset.size for asset in candidates), 0
        with StageReporter(self.progress, "按内容校验 QQ 缓存媒体标识", completed=0, total=total, unit="字节", cancel=self.cancel) as stage:
            for asset in candidates:
                _check(self.cancel)
                try:
                    if _linked(asset.path) or not asset.path.resolve().is_relative_to(self.root):
                        continue
                    md5, _ = _hashes(asset.path, self.cancel)
                    self.by_content[md5].append(asset)
                    self.content_hashed.add(asset.relative)
                except OSError:
                    self.counts["unreadable_cache_files"] += 1
                completed += asset.size
                stage.update(completed=completed)
        self.content_scanned.add(kind)

    def _candidates(self, reference):
        found = {}
        for value in reference.paths:
            asset = self._path_match(value)
            if asset:
                found[asset.relative] = (asset, "recorded_path")
        for key in (*reference.md5, *reference.identifiers):
            for asset in self.by_id.get(key.lower(), []):
                found.setdefault(asset.relative, (asset, "cache_identifier"))
        for name in reference.filenames:
            assets = [a for a in self.by_name.get(PureWindowsPath(name).name.casefold(), []) if _cache_matches(reference.kind, a.kind)]
            # Common names such as image.png can be overwritten/reused by the
            # client. A unique basename alone cannot identify a historical asset.
            strong_name = bool(HASH_TOKEN.search(PureWindowsPath(name).name))
            if len(assets) == 1 and strong_name:
                found.setdefault(assets[0].relative, (assets[0], "unique_recorded_filename"))
        allowed = [(a, basis) for a, basis in found.values() if _cache_matches(reference.kind, a.kind)
                   or (reference.kind == "video" and a.thumbnail)]
        def preference(item):
            asset, _ = item
            match = re.search(r"[0-9a-f]{32}_(\d{1,4})(?:\.|$)", asset.path.name, re.I)
            resolution = int(match[1]) if match else 0
            return asset.thumbnail, -resolution, -asset.size, asset.relative
        return sorted(allowed, key=preference)

    def _publish(self, asset, reference, basis):
        _check(self.cancel)
        expected = set(reference.md5 or (reference.identifiers if basis == "cache_identifier" else ()))
        key = (asset.relative, tuple(sorted(expected)), reference.kind)
        if key in self.published:
            self.counts["reused_messages"] += 1
            return self.published[key]
        if key in self.failures:
            return None
        try:
            if _linked(asset.path) or not asset.path.resolve().is_relative_to(self.root):
                raise ExportError("QQ 媒体来源已变成链接或越出账号。")
            before = asset.path.stat()
            partial = self._destination(self.incoming / (uuid4().hex + ".partial"))
            md5, sha, completed = hashlib.md5(), hashlib.sha256(), 0
            report = StageReporter(self.progress, "复制并校验 QQ 本地媒体", completed=0,
                                   total=before.st_size, unit="字节", cancel=self.cancel) if before.st_size > 4 * 1024 * 1024 else nullcontext(None)
            with report as stage, asset.path.open("rb") as inp, partial.open("xb") as out:
                while True:
                    _check(self.cancel)
                    data = inp.read(4 * 1024 * 1024)
                    if not data:
                        break
                    out.write(data)
                    md5.update(data)
                    sha.update(data)
                    completed += len(data)
                    if stage:
                        stage.update(completed=completed)
                out.flush()
                os.fsync(out.fileno())
            after = asset.path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or completed != after.st_size:
                raise ExportError("QQ 媒体在复制期间变化，保留副本并跳过。")
            actual_md5, digest = md5.hexdigest(), sha.hexdigest()
            if _hashes(asset.path, self.cancel) != (actual_md5, digest):
                raise ExportError("QQ 媒体副本与原文件完整校验不一致。")
            if expected and not asset.thumbnail and actual_md5 not in expected:
                raise ExportError("QQ 缓存文件内容与消息记录的 MD5 不一致。")
            details = _inspect_copy(partial, "image" if asset.thumbnail else reference.kind)
            if not asset.thumbnail and not _kind_matches(reference.kind, details["kind"]):
                raise ExportError("QQ 媒体实际格式与消息类型不一致。")
            if details["kind"] == "file":
                extension = asset.path.suffix.lower()
                if re.fullmatch(r"\.[a-z0-9]{1,12}", extension):
                    details["extension"] = extension
                    details["mime"] = mimetypes.guess_type(asset.path.name)[0] or details["mime"]
            relative = f"attachments/qq_pc/{digest[:2]}/{digest}{details.pop('extension')}"
            target = self._destination(self.output / relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if _hashes(target, self.cancel)[1] != digest:
                    raise ExportError("资料库已有 QQ 媒体校验失败。")
                self.counts["existing_files"] += 1
            else:
                partial.rename(target)
                self.counts["copied_files"] += 1
            resource = {**details, "status": "ok", "path": relative, "size": completed, "sha256": digest,
                        "md5": actual_md5, "variant": "thumbnail" if asset.thumbnail else "original", "match": basis}
            self.published[key] = resource
            self._record({"source_relative": asset.relative, "resource": resource})
            self.progress("QQ 本地媒体：" + ("已恢复缩略图 " if asset.thumbnail else "已恢复原文件 ") + relative)
            return resource
        except (OSError, ExportError) as error:
            _check(self.cancel)
            self.failures[key] = str(error)
            self.counts["failed_candidates"] += 1
            self._record({"source_relative": asset.relative, "status": "failed", "reason": str(error)})
            return None

    def resolve(self, reference):
        if isinstance(reference, dict):
            reference = MediaReference(**reference)
        candidates = self._candidates(reference)
        thumbnails = []
        for asset, basis in candidates:
            if asset.thumbnail:
                thumbnails.append((asset, basis))
                continue
            resource = self._publish(asset, reference, basis)
            if resource:
                return resource
        content_keys = reference.md5 or reference.identifiers
        if content_keys:
            self._scan_content(reference.kind)
            for key in content_keys:
                for asset in self.by_content.get(key.lower(), []):
                    resource = self._publish(asset, reference, "content_md5")
                    if resource:
                        return resource
        for asset, basis in thumbnails:
            resource = self._publish(asset, reference, basis)
            if resource:
                return resource
        return None

    def attach(self, record, references=None):
        """Mutate only _export and return it; original message fields stay intact."""
        if references is None:
            import base64
            value = record.get("_desktop_raw", {}).get("row", {}).get("40800")
            if isinstance(value, dict) and value.get("encoding") == "base64":
                try:
                    value = base64.b64decode(value["data"], validate=True)
                except (KeyError, ValueError):
                    value = None
            references = extract_qqnt_media(value)
            references.extend(extract_legacy_media(record.get("_export", {}).get("legacy_media_refs")))
        references = list(references)
        if not references:
            return record.get("_export", {})
        meta = record.setdefault("_export", {})
        resources, unresolved, originals = [], 0, []
        for reference in references:
            _check(self.cancel)
            resource = self.resolve(reference)
            if resource:
                if resource["variant"] == "original":
                    originals.append(resource["path"])
                resource = self._voice_playback(resource)
                resource = {**resource, "reference_id": reference_id(reference)}
                if resource["path"] not in {r["path"] for r in resources}:
                    resources.append(resource)
            else:
                unresolved += 1
        meta["local_media"] = list(dict.fromkeys([*meta.get("local_media", []), *(r["path"] for r in resources)]))
        meta["desktop_media"] = resources
        meta["desktop_media_summary"] = {"unresolved": unresolved, "requested": len(references)}
        meta["original_media"] = list(dict.fromkeys([*meta.get("original_media", []), *originals]))
        if resources:
            meta["media"] = {key: value for key, value in resources[0].items() if key not in {"match", "variant", "md5"}}
            meta["media_status"] = "partial" if unresolved else "ok"
            if any(r["variant"] == "thumbnail" for r in resources):
                meta["media_status"] = "thumbnail" if not originals and not unresolved else "partial"
                self.counts["thumbnail_messages"] += 1
            self.counts["recovered_messages"] += 1
        else:
            meta["media_status"] = "missing"
            self.counts["missing_messages"] += 1
        return meta
