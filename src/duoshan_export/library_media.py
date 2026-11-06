"""Explicit, resumable media retrieval from the authoritative reader library.

Only media metadata is patched through the Node reader service. Original rows,
annotations, deletion suppression and backup files are never written here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
import xml.etree.ElementTree as ET

from .errors import ExportError
from .library import _contained_file, _library_lock, _new_run
from .runtime import file_sha256

MANIFEST = "library_media_manifest.jsonl"
HOSTS = {
    "duoshan": ("douyinstatic.com", "douyinpic.com", "byteimg.com", "pstatp.com", "snssdk.com", "ibytedtos.com"),
    "xhs": ("xhscdn.com", "picasso-static.xiaohongshu.com"),
    "wechat": ("qpic.cn", "qlogo.cn", "wxapp.tc.qq.com", "wx.qpic.cn"),
}
EXACT_HOSTS = {"xhs": {"image-url-2-feature-1251524319.cos.ap-shanghai.myqcloud.com"}}
MAX_BYTES = {"image": 64 * 1024**2, "emoji": 64 * 1024**2, "voice": 64 * 1024**2,
             "video": 512 * 1024**2, "file": 128 * 1024**2}
MAX_REDIRECTS = 4
MAX_CANDIDATES = 8
_SESSION = threading.local()
_PUBLISH_LOCK = threading.Lock()
MEDIA_FIELDS = {"status", "kind", "path", "sha256", "bytes", "format", "width", "height", "frames", "duration", "mime"}
STATUS_TEXT = {"ok": "下载成功", "existing": "已有本地文件", "failed": "失败", "unsupported": "不支持",
               "pending": "待处理 / 暂无结果", "orphaned": "当前记录已不存在", "cancelled": "已取消"}
REASON_TEXT = {
    "missing_recorded_url": "备份没有记录可用的媒体网址，无法发起下载。",
    "video_url_not_recorded": "备份没有记录视频下载网址；封面图片有独立网址时可单独下载。",
    "wechat_url_not_recorded": "微信备份没有记录可直接下载的 HTTP(S) 媒体网址。",
    "encrypted_wechat_resource": "微信资源包含加密密钥或专用 CDN 标记，当前下载器不支持此协议。",
    "encrypted_media_resource": "媒体被标记为加密资源，当前下载器不支持直接获取。",
    "unapproved_media_host": "网址或重定向目标不在该应用的已支持媒体域名范围内，工具未向该目标发出请求；这不代表文件不存在。",
    "invalid_or_encrypted_image": "收到的内容无法通过图片解码校验，可能是非图片响应、加密内容或损坏文件。",
    "invalid_or_encrypted_media": "收到的内容未通过媒体解码校验，可能是加密、损坏或非媒体响应。",
    "invalid_or_encrypted_audio": "语音内容未能解码为可播放音频。",
    "resource_exceeds_size_limit": "资源大小超过当前类型的下载上限。",
    "incomplete_response": "下载字节数与服务端声明不符，已保留收到的部分文件。",
    "empty_resource": "服务端返回了空文件。",
    "invalid_content_length": "服务端返回的文件大小无效。",
    "redirect_without_location": "服务端要求重定向，但没有提供目标网址。",
    "too_many_redirects": "重定向次数超过限制。",
    "unsupported_video_container": "视频封装格式暂不支持。",
    "unsupported_attachment_format": "附件格式暂不支持自动保存。",
    "missing_audio_stream": "文件中没有可识别的音轨。",
    "missing_video_stream": "文件中没有可识别的视频轨道。",
    "existing_content_hash_mismatch": "同名本地文件的内容哈希不匹配，未覆盖原文件。",
    "copy_hash_mismatch": "复制后的内容哈希不匹配，未发布文件。",
    "media_path_validation_failed": "媒体目标路径未通过资料库目录范围校验。",
    "invalid_media_staging_directory": "媒体暂存目录不存在或未通过路径校验。",
    "downloaded": "已下载并通过媒体校验。",
    "pending_result": "此快照没有该资源的完成结果；它可能尚未开始、正在处理，或此前任务已中断。",
    "local_media_reference_present": "当前消息已关联本地文件；本报告只检查文件存在，未重新解码或校验哈希。",
    "message_no_longer_present": "日志仍保留此资源，但当前资料库已没有对应消息。",
    "historical_attempts_unknown": "旧版日志没有保存尝试过的网址，不能据此推断哪些候选网址实际请求过。",
    "cancelled": "操作已取消；已收到的部分文件和此前成功结果会保留。",
}


def reason_text(reason):
    """Translate stable reason codes without guessing a remote server's cause."""
    parts = str(reason or "pending_result").split("; ")
    translated = []
    for part in parts:
        if part in REASON_TEXT:
            translated.append(REASON_TEXT[part])
        elif part.startswith(("expired_or_unavailable_http_", "http_")):
            code = part.rsplit("_", 1)[-1]
            translated.append({"403": "服务器返回 HTTP 403，拒绝访问；可能涉及签名、权限或服务端限制，单凭状态码无法确定原因。",
                               "401": "服务器返回 HTTP 401，要求有效授权。",
                               "404": "服务器返回 HTTP 404，当前网址未找到资源。",
                               "410": "服务器返回 HTTP 410，资源已不可用。"}.get(code, f"服务器返回 HTTP {code}，下载未完成。"))
        elif part.startswith("media_staging_"):
            translated.append("媒体暂存目录创建或校验失败，尚未开始此次下载。")
        elif part.startswith("worker_"):
            translated.append("此资源处理发生异常，其他资源继续处理。错误类型：" + part[7:])
        elif part.startswith("media_decoder_"):
            translated.append("媒体解码程序未能完成。错误类型：" + part[14:])
        else:
            translated.append("处理未完成，原因代码：" + part)
    return " ".join(dict.fromkeys(translated))


def _message_refs(item):
    return [{k: message[k] for k in ("uid", "msg_uuid", "conversation_id", "conversation_name", "account_directory") if k in message}
            for message in item.get("messages", [])]


def _resource_details(item, record, *, attempts_known=None):
    attempts = record.get("attempts") if isinstance(record.get("attempts"), list) else []
    known = record.get("attempts_known", "attempts" in record) if attempts_known is None else attempts_known
    reason = record.get("reason") or ("downloaded" if record.get("status") == "ok" else "pending_result")
    return {"recorded_urls": list(item.get("urls", record.get("recorded_urls", []))),
            "attempts": attempts, "attempts_known": bool(known),
            "attempted_urls": list(dict.fromkeys(request["url"] for attempt in attempts for request in attempt.get("requests", [])
                                                if request.get("requested") and request.get("url"))),
            "reason_text": reason_text(reason), "messages": _message_refs(item) if "messages" in item else record.get("messages", [])}


class _Cancelled(Exception):
    pass


class _ResourceError(Exception):
    def __init__(self, reason, status="failed", diagnostic=None):
        self.reason, self.status = reason, status
        self.diagnostic = diagnostic
        super().__init__(reason)


def _check(cancel):
    if cancel and cancel():
        raise _Cancelled()


def _path_diagnostic(output, path, error):
    """Private local-path evidence for intermittent Windows resolution errors."""
    if isinstance(error, _ResourceError) and error.diagnostic:
        return error.diagnostic
    result = {"error_type": type(error).__name__, "root": str(output), "path": str(path),
              "message": f"媒体路径校验失败：资料库目录 {output}；目标路径 {path}。"}
    try:
        result.update(resolved_root=str(output.resolve()), resolved_path=str(path.resolve()),
                      exists=path.exists(), is_symlink=path.is_symlink())
    except OSError as detail:
        result["resolution_error"] = type(detail).__name__
    return result


def _media_path(output, relative):
    try:
        return _contained_file(output, relative)
    except (OSError, ExportError) as error:
        raise _ResourceError("media_path_validation_failed", diagnostic=_path_diagnostic(output, output / relative, error)) from error


def _decoded(value, depth=0):
    if depth > 12:
        return None
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "[", '"'):
        try:
            return _decoded(json.loads(value), depth + 1)
        except ValueError:
            return value
    if isinstance(value, dict):
        return {k: _decoded(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_decoded(v, depth + 1) for v in value]
    return value


def _urls(value):
    found = []
    def visit(node):
        if isinstance(node, str) and node.startswith(("https://", "http://")):
            if node not in found:
                found.append(node)
        elif isinstance(node, dict):
            # Prefer originals, retaining recorded alternatives as fallbacks.
            priority = ("origin_url_list", "large_url_list", "url_list", "IM_ORG", "IM_DTL", "IM_PRV")
            for key in (*[k for k in priority if k in node], *[k for k in node if k not in priority]):
                visit(node[key])
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    return found


def _resource(kind, value=None, *, reason=None):
    links = _urls(value)
    identity = str(value.get("uri") or value.get("md5") or "") if isinstance(value, dict) else ""
    if isinstance(value, dict) and any(value.get(k) for k in ("aeskey", "aes_key", "encrypt_key", "encrypted", "is_encrypted")):
        reason = "encrypted_media_resource"
    return {"kind": kind, "urls": links, "identity": identity or (links[0] if links else reason or kind),
            "reason": reason if reason else (None if links else "missing_recorded_url")}


def _extract_resources(app, raw, meta, message_type):
    """Only inspect app media payloads; never interpret arbitrary chat links."""
    body = _decoded(raw.get("content"))
    typ = int(raw.get("type") or message_type or 0) if app != "xhs" else int(raw.get("content_type") or message_type or 0)
    resources = []
    if app == "duoshan":
        body = body if isinstance(body, dict) else {}
        if typ in (5, 17, 27):
            resources.append(_resource({5: "emoji", 17: "voice", 27: "image"}[typ], body.get("url" if typ == 5 else "resource_url")))
        elif typ == 30:
            video = next((body[k] for k in ("video_url", "play_url", "play_addr", "video") if k in body and _urls(body[k])), None)
            # A resource_url is a video only when it is explicitly present.
            video = video or body.get("resource_url")
            resources.append(_resource("video", video, reason=None if _urls(video) else "video_url_not_recorded"))
            if _urls(body.get("poster")):
                resources.append(_resource("image", body["poster"]))
        elif typ in (8, 77, 105, 110):
            for key in ("cover_url", "content_thumb", "cover", "image_url"):
                if _urls(body.get(key)):
                    resources.append(_resource("image", body[key]))
    elif app == "xhs":
        body = body if isinstance(body, dict) else {}
        inner = body.get("content")
        if isinstance(inner, dict):
            body = {**body, **inner}
        if typ in (2, 11):
            image = {"imageDataMap": body.get("imageDataMap"), "link": body.get("link") if typ == 2 else None}
            if _urls(image):
                resources.append(_resource("image", image))
            live = body.get("livePhotoVideo")
            video = body.get("videoLink") or (live.get("videoLink") if isinstance(live, dict) else None)
            if video:
                resources.append(_resource("video", video))
            if not resources:
                resources.append(_resource("image" if typ == 2 else "video"))
        elif typ == 9:
            resources.append(_resource("video", body.get("videoLink") or body.get("link")))
        elif typ in (13, 16):
            resources.append(_resource("emoji", body.get("url") or body.get("image") or body.get("thumb")))
        elif typ == 3:
            for key in ("cover", "coverUrl", "image", "imageUrl", "cover_url", "imageDataMap"):
                if _urls(body.get(key)):
                    resources.append(_resource("image", body[key]))
    elif app == "wechat":
        original = raw.get("_wechat_raw") if isinstance(raw.get("_wechat_raw"), dict) else raw
        typ = int(original.get("type") or typ) & 0xffff
        kind = {3: "image", 34: "voice", 43: "video", 47: "emoji", 49: "file", 62: "video"}.get(typ)
        text = original.get("content") or raw.get("content")
        if isinstance(text, str) and ":\n<" in text and not text.lstrip().startswith("<"):
            text = text.split(":\n", 1)[1]
        if kind:
            try:
                tree = ET.fromstring(text) if isinstance(text, str) and len(text) < 8 * 1024**2 else None
            except ET.ParseError:
                tree = None
            if tree is not None:
                for element in tree.iter():
                    tag = element.tag.lower()
                    if tag not in {"img", "emoji", "videomsg", "voicemsg", "appattach"}:
                        continue
                    attrs = {str(k).lower(): v for k, v in element.attrib.items()}
                    values = {**attrs, **{child.tag.lower(): child.text or "" for child in element if child.text}}
                    encrypted = any(values.get(k) for k in ("aeskey", "cdnthumbaeskey", "cdnmidimgaeskey", "cdnbigimgaeskey"))
                    links = [html.unescape(str(v)) for k, v in values.items() if k in {"cdnurl", "thumburl", "cdnthumburl", "cdnmidimgurl", "cdnbigimgurl", "cdnvideourl", "voiceurl", "attachurl", "cdnattachurl"} and str(v).startswith(("https://", "http://"))]
                    resources.append(_resource(kind, links, reason="encrypted_wechat_resource" if encrypted else None))
            if not resources:
                resources.append(_resource(kind, reason="wechat_url_not_recorded"))
    return resources


def _allowed(app, url):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return (parts.scheme == "https" and not parts.username and not parts.password
                and parts.port in (None, 443) and bool(host)
                and (host in EXACT_HOSTS.get(app, set()) or any(host == suffix or host.endswith("." + suffix) for suffix in HOSTS.get(app, ()))))
    except ValueError:
        return False


def _session():
    import requests
    session = getattr(_SESSION, "value", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        session.headers["User-Agent"] = "okhttp/4.12.0"
        _SESSION.value = session
    return session


def _fetch(app, url, kind, run, cancel, requests=None):
    """Stream bounded responses, validating the first URL and every redirect."""
    if url.startswith("http://"):
        url = "https://" + url[7:]
    for _ in range(MAX_REDIRECTS + 1):
        _check(cancel)
        request = {"url": url, "requested": False, "status": "checking"}
        if requests is not None:
            requests.append(request)
        if not _allowed(app, url):
            request.update(status="unsupported", reason="unapproved_media_host", reason_text=reason_text("unapproved_media_host"))
            raise _ResourceError("unapproved_media_host", "unsupported")
        request["requested"] = True
        response = None
        try:
            response = _session().get(url, timeout=(8, 30), allow_redirects=False, stream=True)
            request["http_status"] = response.status_code
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise _ResourceError("redirect_without_location")
                url = urljoin(url, location)
                request.update(status="redirect", redirect_to=url)
                continue
            if response.status_code in (401, 403, 404, 410):
                raise _ResourceError("expired_or_unavailable_http_" + str(response.status_code))
            if response.status_code != 200:
                raise _ResourceError("http_" + str(response.status_code))
            limit = MAX_BYTES[kind]
            try:
                length = int(response.headers.get("Content-Length", "0"))
            except ValueError:
                raise _ResourceError("invalid_content_length")
            if length < 0 or length > limit:
                raise _ResourceError("resource_exceeds_size_limit")
            target = run / (uuid4().hex + ".partial")
            size = 0
            with target.open("xb") as stream:
                for block in response.iter_content(128 * 1024):
                    _check(cancel)
                    if not block:
                        continue
                    size += len(block)
                    if size > limit:
                        raise _ResourceError("resource_exceeds_size_limit")
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            if size == 0:
                raise _ResourceError("empty_resource")
            if length and not response.headers.get("Content-Encoding") and length != size:
                raise _ResourceError("incomplete_response")
            request["status"] = "received"
            return target, response.headers.get("Content-Type", "")
        except _ResourceError as error:
            request.update(status=error.status, reason=error.reason, reason_text=reason_text(error.reason))
            raise
        except _Cancelled:
            request.update(status="cancelled", reason="cancelled")
            raise
        except Exception as error:
            request.update(status="failed", reason=type(error).__name__, reason_text=reason_text(type(error).__name__))
            raise
        finally:
            if response is not None:
                response.close()
    raise _ResourceError("too_many_redirects")


def _publish(path, output, app, extension):
    digest = file_sha256(path)
    relative = f"attachments/{app}/online/{digest[:2]}/{digest}{extension}"
    # Serialize canonicalization and publication of shared directory chains.
    # Network/decode work remains parallel.
    with _PUBLISH_LOCK:
        target = _media_path(output, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = _media_path(output, relative)
        if target.exists():
            if file_sha256(target) != digest:
                raise _ResourceError("existing_content_hash_mismatch")
        else:
            # Normal downloads and conversions stage on the library volume.
            if path.resolve().is_relative_to((output / "attachments").resolve()):
                partial = path
            else:
                partial = target.with_name(target.name + ".partial-" + uuid4().hex)
                shutil.copyfile(path, partial)
                if file_sha256(partial) != digest:
                    raise _ResourceError("copy_hash_mismatch")
            try:
                partial.rename(target)
            except FileExistsError:
                if file_sha256(target) != digest:
                    raise _ResourceError("existing_content_hash_mismatch")
    return {"path": relative, "sha256": digest, "bytes": target.stat().st_size}


def _process(command, *, input_text=None, timeout=120):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = subprocess.run(command, input=input_text, capture_output=True, text=True, encoding="utf-8", timeout=timeout, **options)
    except (OSError, subprocess.SubprocessError) as error:
        raise _ResourceError("media_decoder_" + type(error).__name__)
    if result.returncode:
        raise _ResourceError("invalid_or_encrypted_media")
    return result


def _prepare_asset(path, mime, kind, output, app, run, node, cancel):
    _check(cancel)
    head = path.read_bytes()[:64] if path.stat().st_size < 128 else b""
    if not head:
        with path.open("rb") as stream:
            head = stream.read(64)
    originals = []
    details = {}
    if kind in ("image", "emoji"):
        from .media import _load_pillow, IMAGE_SUFFIXES
        Image = _load_pillow()
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
                details = {"format": image.format, "width": image.width, "height": image.height, "frames": getattr(image, "n_frames", 1)}
                suffix = IMAGE_SUFFIXES.get(image.format)
                if not suffix:
                    original_suffix = ".heic" if image.format in ("HEIF", "HEIC") else ".bin"
                    converted = run / (uuid4().hex + ".png")
                    with image.convert("RGBA") as preview:
                        preview.save(converted)
            if not suffix:
                # Close Pillow's source handle before Windows renames it.
                originals.append(_publish(path, output, app, original_suffix))
                path, suffix = converted, ".png"
                details.update(format="PNG", frames=1)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            raise _ResourceError("invalid_or_encrypted_image") from error
    elif kind in ("voice", "video"):
        if kind == "voice" and b"SILK_V3" in head[:20]:
            originals.append(_publish(path, output, app, ".silk"))
            path = output / originals[-1]["path"]
            converted = run / (uuid4().hex + ".wav")
            script = Path(__file__).resolve().parents[2] / "web/server/decode-voice.mjs"
            response = _process([node, str(script)], input_text=json.dumps({"source": str(path), "output": str(converted)}) + "\n")
            if not json.loads(response.stdout).get("ok"):
                raise _ResourceError("invalid_or_encrypted_audio")
            path = converted
            with path.open("rb") as stream:
                head = stream.read(64)
        probe = _process(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries", "stream=codec_name,codec_type:format=duration,format_name", "-of", "json", str(path)], timeout=60)
        info = json.loads(probe.stdout)
        streams = info.get("streams") or []
        wanted = "audio" if kind == "voice" else "video"
        if not any(s.get("codec_type") == wanted for s in streams):
            raise _ResourceError("missing_" + wanted + "_stream")
        details = {"duration": float(info.get("format", {}).get("duration") or 0), "format": "WAV" if kind == "voice" else "MP4"}
        if kind == "voice":
            if not (head.startswith(b"RIFF") and head[8:12] == b"WAVE"):
                if not originals:
                    originals.append(_publish(path, output, app, ".amr" if head.startswith(b"#!AMR") else ".bin"))
                    path = output / originals[-1]["path"]
                converted = run / (uuid4().hex + ".wav")
                _process(["ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", str(path), "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-n", str(converted)])
                path = converted
            suffix = ".wav"
        else:
            container = info.get("format", {}).get("format_name", "")
            if "mov" in container or "mp4" in container:
                suffix = ".mp4"
            elif "webm" in container:
                suffix = ".webm"
            else:
                raise _ResourceError("unsupported_video_container", "unsupported")
    else:
        # No arbitrary HTML/executable payload is published as an attachment.
        if head.startswith(b"%PDF"):
            suffix = ".pdf"
        elif head.startswith(b"PK\x03\x04"):
            suffix = ".zip"
        else:
            raise _ResourceError("unsupported_attachment_format", "unsupported")
    _check(cancel)
    primary = _publish(path, output, app, suffix)
    return {"status": "ok", "kind": kind, **primary, **details,
            "mime": mime.split(";", 1)[0], "originals": originals}


def _load_manifest(output):
    records = {}
    path = output / MANIFEST
    if path.is_file():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("resource_id"):
                    records[item["resource_id"]] = item
    return records


def _valid_record(record, output, cache=None):
    if not isinstance(record, dict) or record.get("status") != "ok":
        return False
    try:
        for asset in [record, *record.get("originals", [])]:
            path = _contained_file(output, asset["path"])
            key = (str(path), asset["bytes"], asset["sha256"])
            if cache is not None and key in cache:
                if not cache[key]:
                    return False
                continue
            valid = path.is_file() and path.stat().st_size == asset["bytes"] and file_sha256(path) == asset["sha256"]
            if cache is not None:
                cache[key] = valid
            if not valid:
                return False
        return True
    except (OSError, ExportError, KeyError, TypeError, ValueError):
        return False


def _journal(output, record, *, durable=True):
    # Leading newline isolates a torn final write without deleting its bytes.
    with (output / MANIFEST).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n" + json.dumps({"time": datetime.now(timezone.utc).isoformat(), **record}, ensure_ascii=True) + "\n")
        stream.flush()
        if durable:
            os.fsync(stream.fileno())


def _existing(meta, resource, output, count, cache=None):
    if count != 1:
        return None
    candidates = []
    primary = meta.get("media")
    if isinstance(primary, dict) and primary.get("path"):
        candidates.append(primary["path"])
    candidates.extend(meta.get("local_media") or [])
    extensions = {"voice": {".wav", ".mp3", ".m4a", ".ogg", ".flac"}, "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"},
                  "emoji": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}, "video": {".mp4", ".webm", ".mov"}, "file": {".pdf", ".zip"}}
    for relative in candidates:
        try:
            path = _contained_file(output, relative)
            expected_hash = primary.get("sha256") if isinstance(primary, dict) and primary.get("path") == relative else None
            if not expected_hash and re.fullmatch(r"[0-9a-f]{64}", path.stem):
                expected_hash = path.stem
            key = (str(path), resource["kind"], expected_hash)
            if cache is not None and key in cache:
                if cache[key]:
                    return cache[key]
                continue
            if cache is not None:
                cache[key] = None
            if path.is_file() and path.stat().st_size and path.suffix.lower() in extensions[resource["kind"]]:
                digest = file_sha256(path)
                if expected_hash and expected_hash != digest:
                    continue
                # Basic decoder validation avoids blessing a same-size damaged file.
                if not expected_hash and resource["kind"] in ("image", "emoji"):
                    from .media import _load_pillow
                    with _load_pillow().open(path) as image:
                        image.verify()
                elif not expected_hash and resource["kind"] in ("voice", "video"):
                    probe = _process(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries", "stream=codec_type", "-of", "json", str(path)], timeout=60)
                    streams = json.loads(probe.stdout).get("streams", [])
                    if not any(s.get("codec_type") == ("audio" if resource["kind"] == "voice" else "video") for s in streams):
                        continue
                record = {"status": "ok", "kind": resource["kind"], "path": relative, "sha256": digest, "bytes": path.stat().st_size, "originals": []}
                if cache is not None:
                    cache[key] = record
                return record
        except (OSError, ValueError, TypeError, ExportError, _ResourceError):
            continue
    return None


def _inventory(output, accounts, progress, cancel):
    resources = {}
    scanned, last_progress = 0, time.monotonic()
    database = sqlite3.connect((output / "reader.sqlite").as_uri() + "?mode=ro", uri=True)
    try:
        database.execute("PRAGMA query_only=ON")
        database.execute("BEGIN")
        account_rows = list(database.execute("SELECT uid,app,directory FROM library_accounts"))
        account_apps = {uid: app for uid, app, _ in account_rows}
        account_directories = {uid: directory for uid, _, directory in account_rows}
        wanted = set(str(uid) for uid in (accounts or account_apps))
        unknown = wanted - account_apps.keys()
        if unknown:
            raise ExportError("Requested account is not present in this library.")
        for uid in sorted(wanted):
            app = account_apps[uid]
            progress("识别资料库媒体：" + app)
            for index, (uuid, raw_text, meta_text, typ, conversation_id, conversation_name) in enumerate(database.execute("SELECT msg_uuid,raw_json,meta_json,message_type,conversation_id,conversation_name FROM messages WHERE uid=?", (uid,))):
                if index % 1000 == 0:
                    _check(cancel)
                raw, meta = json.loads(raw_text), json.loads(meta_text)
                scanned += 1
                if time.monotonic() - last_progress >= 5:
                    progress(f"已扫描 {scanned:,} 条记录，识别 {len(resources):,} 个媒体资源。")
                    last_progress = time.monotonic()
                items = _extract_resources(app, raw, meta, typ)
                for item in items:
                    identity = item["identity"] if item["urls"] else uid + "\0" + uuid + "\0" + item["identity"]
                    rid = hashlib.sha256((app + "\0" + item["kind"] + "\0" + identity).encode()).hexdigest()
                    group = resources.setdefault(rid, {"resource_id": rid, "app": app, **item, "messages": [], "existing_candidates": []})
                    group["urls"] = list(dict.fromkeys(group["urls"] + item["urls"]))
                    local = meta.get("local_media") if isinstance(meta.get("local_media"), list) else []
                    primary = meta.get("media") if isinstance(meta.get("media"), dict) else {}
                    if sum(r["kind"] == item["kind"] for r in items) == 1 and (local or primary.get("path")):
                        candidate = {"local_media": local, "media": primary}
                        if candidate not in group["existing_candidates"]:
                            group["existing_candidates"].append(candidate)
                    group["messages"].append({"uid": uid, "msg_uuid": uuid, "app": app,
                        "conversation_id": conversation_id, "conversation_name": conversation_name,
                        "account_directory": account_directories[uid],
                        "expected_raw_sha256": hashlib.sha256(raw_text.encode()).hexdigest(),
                        "present_paths": list(dict.fromkeys([*local, *meta.get("original_media", []), *([primary["path"]] if primary.get("path") else [])]))})
    except sqlite3.Error as error:
        raise ExportError("Choose a managed reader library with readable account/message tables.") from error
    finally:
        database.close()
    return resources


def _manifest_snapshot(output, cancel=None):
    """Read only the bytes present at open; a concurrent append is a later snapshot."""
    records = {}
    snapshot = {"bytes": 0, "parsed_records": 0, "ignored_lines": 0, "torn_final_line": False}
    path = output / MANIFEST
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return records, snapshot
    with stream:
        remaining = snapshot["bytes"] = os.fstat(stream.fileno()).st_size
        while remaining:
            _check(cancel)
            line = stream.readline(min(remaining, 8 * 1024**2 + 1))
            if not line:
                break
            remaining -= len(line)
            if len(line) > 8 * 1024**2:
                # Bound malformed single-line allocation without following appends.
                while remaining and not line.endswith(b"\n"):
                    line = stream.readline(min(remaining, 64 * 1024))
                    if not line:
                        break
                    remaining -= len(line)
                snapshot["ignored_lines"] += 1
                continue
            if not line.endswith(b"\n"):
                snapshot["torn_final_line"] = True
                snapshot["ignored_lines"] += 1
                continue
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                snapshot["ignored_lines"] += 1
                continue
            if isinstance(record, dict) and isinstance(record.get("resource_id"), str):
                records[record["resource_id"]] = record
                snapshot["parsed_records"] += 1
            else:
                snapshot["ignored_lines"] += 1
    snapshot["unique_resources"] = len(records)
    return records, snapshot


def _report_local_paths(item, output):
    paths = []
    extensions = {"voice": {".wav", ".mp3", ".m4a", ".ogg", ".flac"},
                  "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"},
                  "emoji": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"},
                  "video": {".mp4", ".webm", ".mov"}, "file": {".pdf", ".zip"}}
    for candidate in item.get("existing_candidates", []):
        primary = candidate.get("media") or {}
        for relative in [*candidate.get("local_media", []), *([primary["path"]] if primary.get("path") else [])]:
            try:
                path = _contained_file(output, relative)
                if path.suffix.lower() in extensions.get(item["kind"], set()) and path.is_file() and relative not in paths:
                    paths.append(relative)
            except (ExportError, OSError, TypeError):
                continue
    return paths


def _write_media_report(result, output):
    folder = output / "media_reports"
    if folder.is_symlink() or not folder.resolve().is_relative_to(output):
        raise ExportError("Report directory escapes its library.")
    folder.mkdir(exist_ok=True)
    if folder.is_symlink() or not folder.resolve().is_relative_to(output):
        raise ExportError("Report directory escapes its library.")
    name = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:12]
    paths = {key: folder / (name + suffix) for key, suffix in
             (("report_json", ".json"), ("report_html", ".html"), ("report_text", ".txt"))}
    result.update({key: str(path) for key, path in paths.items()})
    with paths["report_json"].open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    with paths["report_html"].open("x", encoding="utf-8", newline="\n") as page, paths["report_text"].open("x", encoding="utf-8", newline="\n") as plain:
        page.write('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><meta name="viewport" content="width=device-width"><title>媒体下载详情</title>'
                   '<style>body{font:16px system-ui,sans-serif;max-width:1150px;margin:32px auto;padding:0 20px;color:#253044}details{border:1px solid #cbd5e1;border-radius:8px;padding:14px;margin:12px 0}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fa;padding:12px}p{line-height:1.6}</style>'
                   '<h1>媒体下载详情</h1>')
        heading = f"快照时间：{result['as_of']}\n{result['snapshot_note']}\n统计：{json.dumps(result['totals'], ensure_ascii=False)}"
        page.write("<pre>" + html.escape(heading) + "</pre><p>不支持表示备份或当前工具无法发起/完成此类获取，并不等于文件不存在。完整网址仅作为可选择的文本展示，不会自动打开。</p>")
        plain.write("媒体下载详情\n" + heading + "\n\n")
        for item in result["resources"]:
            title = f"{item['status_text']} · {item['app']} · {item['kind']} · {item['resource_id']}"
            sections = [title, item["reason_text"], "消息引用：" + json.dumps(item["messages"], ensure_ascii=False),
                        "备份记录的网址：\n" + ("\n".join(item["recorded_urls"]) or "没有记录可用网址"),
                        "实际请求的网址：\n" + ("\n".join(item["attempted_urls"]) if item["attempts_known"] and item["attempted_urls"] else
                                               "未发出网络请求" if item["attempts_known"] else REASON_TEXT["historical_attempts_unknown"]),
                        "逐次尝试详情：\n" + json.dumps(item["attempts"], ensure_ascii=False, indent=2)]
            if item.get("diagnostic"):
                sections.append("路径诊断：\n" + json.dumps(item["diagnostic"], ensure_ascii=False, indent=2))
            body = "\n\n".join(sections[1:])
            page.write("<details><summary>" + html.escape(title) + "</summary><pre>" + html.escape(body) + "</pre></details>")
            plain.write("\n\n".join(sections) + "\n\n")
        page.write("</html>")


def inspect_library_media(output, *, accounts=None, progress=print, cancel=None, active_download=None):
    """Create a local offline report without taking the mutation lock or patching data."""
    output = Path(output).expanduser().resolve()
    from .library import PACKAGES
    if any(part in PACKAGES for part in output.parts):
        raise ExportError("Reports cannot be created inside original Android package folders.")
    if not (output / "reader.sqlite").is_file():
        raise ExportError("Choose an existing reader library containing reader.sqlite.")
    started = datetime.now(timezone.utc).isoformat()
    try:
        known, snapshot = _manifest_snapshot(output, cancel)
        inventory = _inventory(output, accounts, progress, cancel)
        resources = []
        totals = dict(ok=0, existing=0, failed=0, unsupported=0, pending=0, orphaned=0)
        for item in inventory.values():
            _check(cancel)
            record = known.get(item["resource_id"], {})
            paths = _report_local_paths(item, output)
            if record:
                status = record.get("status", "pending")
                reason = record.get("reason") or ("downloaded" if status == "ok" else "pending_result")
            elif paths:
                status, reason = "existing", "local_media_reference_present"
            elif item.get("reason"):
                status, reason = "unsupported", item["reason"]
            else:
                status, reason = "pending", "pending_result"
            if status not in totals:
                status = "pending"
            row = {"resource_id": item["resource_id"], "app": item["app"], "kind": item["kind"],
                   "status": status, "status_text": STATUS_TEXT[status], "reason": reason,
                   **_resource_details(item, {**record, "status": status, "reason": reason},
                                       attempts_known=None if record else False),
                   "current": True, "journal_time": record.get("time"), "local_paths": paths,
                   "diagnostic": record.get("diagnostic"), "journal_status": record.get("status")}
            resources.append(row)
            totals[status] += 1
        for rid, record in known.items():
            if rid in inventory or (accounts and not any(m.get("uid") in accounts for m in record.get("messages", []))):
                continue
            resources.append({"resource_id": rid, "app": record.get("app", ""), "kind": record.get("kind", ""),
                              "status": "orphaned", "status_text": STATUS_TEXT["orphaned"], "reason": "message_no_longer_present",
                              **_resource_details({"messages": []}, {**record, "reason": "message_no_longer_present"}),
                              "historical_messages": record.get("messages", []),
                              "current": False, "journal_status": record.get("status"), "journal_time": record.get("time")})
            totals["orphaned"] += 1
        journal_path = output / MANIFEST
        snapshot["bytes_after_inventory"] = journal_path.stat().st_size if journal_path.is_file() else 0
        changed = snapshot["bytes_after_inventory"] != snapshot["bytes"]
        active = bool(active_download) or changed
        note = "正在下载期间生成的快照，后续结果会继续变化。" if active else "只读扫描时点的快照；本报告不代表某次下载任务已完成。"
        note += " 日志固定到扫描开始时的字节数，消息来自当前 SQLite 只读快照；本报告不联网，不修改消息。"
        result = {"status": "completed", "as_of": started, "completed_at": datetime.now(timezone.utc).isoformat(),
                  "data_status": "active_snapshot" if active else "snapshot", "snapshot_note": note,
                  "active_download": active_download, "journal_grew_during_scan": changed,
                  "journal_snapshot": snapshot, "totals": totals, "resources": resources}
        _check(cancel)
        _write_media_report(result, output)
        progress("媒体详情报告已生成：" + result["report_html"])
        return result
    except _Cancelled:
        return {"status": "cancelled", "as_of": started, "resources": [], "totals": {}}


def _download(item, output, run, node, cancel):
    if item.get("reason"):
        return {"status": "unsupported", "reason": item["reason"], "attempts": [], "attempts_known": True}
    errors, attempts = [], []
    try:
        incoming = Path(item["incoming"])
        checked = _media_path(output, incoming.relative_to(output).as_posix())
        if checked != incoming or not incoming.is_dir():
            raise _ResourceError("invalid_media_staging_directory")
    except _Cancelled:
        raise
    except Exception as error:
        return {"status": "failed", "reason": "media_staging_" + type(error).__name__,
                "attempts": [], "attempts_known": True,
                "diagnostic": _path_diagnostic(output, Path(item.get("incoming", run)), error)}
    for url in item["urls"][:MAX_CANDIDATES]:
        attempt = {"recorded_url": url, "requests": []}
        attempts.append(attempt)
        try:
            path, mime = _fetch(item["app"], url, item["kind"], incoming, cancel, attempt["requests"])
            asset = _prepare_asset(path, mime, item["kind"], output, item["app"], incoming, node, cancel)
            attempt.update(status="ok", reason="downloaded", reason_text=reason_text("downloaded"))
            return {**asset, "attempts": attempts, "attempts_known": True}
        except _Cancelled as error:
            attempt.update(status="cancelled", reason="cancelled", reason_text=reason_text("cancelled"))
            error.attempts = attempts
            raise
        except _ResourceError as error:
            attempt.update(status=error.status, reason=error.reason, reason_text=reason_text(error.reason))
            errors.append((error.status, error.reason, error.diagnostic))
        except Exception as error:
            # Exceptions from requests/decoders can include signed URLs; never log them.
            attempt.update(status="failed", reason=type(error).__name__, reason_text=reason_text(type(error).__name__))
            errors.append(("failed", type(error).__name__, None))
    result = {"status": "unsupported" if errors and all(s == "unsupported" for s, _, _ in errors) else "failed",
              "reason": "; ".join(dict.fromkeys(reason for _, reason, _ in errors)) or "missing_recorded_url",
              "attempts": attempts, "attempts_known": True}
    diagnostic = next((details for _, _, details in errors if details), None)
    if diagnostic:
        result["diagnostic"] = diagnostic
    return result


def _patch(output, updates, node, progress):
    executable = shutil.which(node)
    script = Path(__file__).resolve().parents[2] / "web/server/library-media.mjs"
    if not executable or not script.is_file():
        raise ExportError("The complete reader tool and Node.js are required to attach downloaded media.")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    args = ["--updates", str(updates)] if updates is not None else ["--resume"]
    result = subprocess.run([executable, str(script), "--output", str(output), *args, "--json"],
                            capture_output=True, text=True, encoding="utf-8", **options)
    if result.returncode:
        raise ExportError("Media files are retained, but the reader metadata patch failed: " + result.stderr[:500])
    return json.loads(result.stdout)


def download_library_media(output, work, *, accounts=None, progress=print, cancel=None,
                           node="node", workers=6, download_media=False):
    """Download recorded media only after explicit opt-in; return durable totals."""
    if download_media is not True:
        raise ExportError("Online media retrieval requires explicit download_media=True / --download-media.")
    if not isinstance(workers, int) or not 1 <= workers <= 16:
        raise ExportError("workers must be between 1 and 16.")
    output, work = Path(output).expanduser().resolve(), Path(work).expanduser().resolve()
    if not (output / "reader.sqlite").is_file():
        raise ExportError("Choose an existing reader library containing reader.sqlite.")
    if output == work or output.is_relative_to(work) or work.is_relative_to(output):
        raise ExportError("Media work and library directories must be separate.")
    from .library import PACKAGES
    if any(part in PACKAGES for dest in (output, work) for part in dest.parts):
        raise ExportError("Library and work directories cannot be inside original Android package folders.")
    script = Path(__file__).resolve().parents[2] / "web/server/library-media.mjs"
    executable = shutil.which(node)
    if not executable or not script.is_file():
        raise ExportError("The complete reader tool and Node.js are required to attach downloaded media.")
    totals = dict(downloaded=0, existing=0, failed=0, unsupported=0)
    failures, successful = [], []
    cancelled = False
    with _library_lock(output):
        progress("检查并恢复未完成的媒体索引更新。")
        recovery_result = _patch(output, None, node, progress)
        known = _load_manifest(output)
        try:
            inventory = _inventory(output, accounts, progress, cancel)
        except _Cancelled:
            return {"status": "cancelled", "totals": totals, "failures": [], "manifest": str(output / MANIFEST)}
        run = _new_run(work, "library-media")
        progress(f"已识别 {len(inventory):,} 个媒体资源；开始校验已有文件。")
        pending_items = []
        reported_diagnostics = set()
        reported_reasons = set()
        last_completion_progress = time.monotonic()
        def completed(item, record, existing=False):
            nonlocal last_completion_progress
            record = {"resource_id": item["resource_id"], "app": item["app"], "kind": item["kind"], **record}
            record.update(_resource_details(item, record, attempts_known=None if existing else record.get("attempts_known", True)))
            # Existing live files need no recovery record. Downloads are fsynced
            # individually; failed discovery can be reproduced on the next run.
            if not existing:
                _journal(output, record, durable=record["status"] == "ok")
            if record["status"] == "ok":
                totals["existing" if existing else "downloaded"] += 1
                wanted_paths = [record["path"], *[r["path"] for r in record.get("originals", [])]]
                patch_messages = [message for message in item["messages"] if not set(wanted_paths).issubset(message["present_paths"])]
                if patch_messages:
                    successful.append(({**item, "messages": patch_messages}, record))
            else:
                totals["unsupported" if record["status"] == "unsupported" else "failed"] += 1
                reason_key = (item["app"], record.get("reason", "failed"))
                if reason_key not in reported_reasons:
                    progress(f"媒体{STATUS_TEXT.get(record['status'], record['status'])}（{item['app']}）：{record['reason_text']}")
                    reported_reasons.add(reason_key)
                diagnostic = record.get("diagnostic") or {}
                if diagnostic.get("message") and diagnostic["message"] not in reported_diagnostics:
                    progress(diagnostic["message"])
                    reported_diagnostics.add(diagnostic["message"])
                failures.append({"resource_id": item["resource_id"], "app": item["app"], "status": record["status"],
                                 "reason": record.get("reason", "failed"), "account_uid": item["messages"][0]["uid"],
                                 "msg_uuid": item["messages"][0]["msg_uuid"],
                                 **_resource_details(item, record),
                                 **({"diagnostic": record["diagnostic"]} if record.get("diagnostic") else {})})
            count = sum(totals.values())
            if time.monotonic() - last_completion_progress >= 2 or count == len(inventory):
                progress(f"媒体 {count:,}/{len(inventory):,}：下载 {totals['downloaded']}，已有 {totals['existing']}，失败 {totals['failed']}，不支持 {totals['unsupported']}")
                last_completion_progress = time.monotonic()
        validation_cache, existing_cache = {}, {}
        validated_started = time.monotonic()
        for item in inventory.values():
            if cancel and cancel():
                cancelled = True
                break
            record = known.get(item["resource_id"])
            if time.monotonic() - validated_started >= 5:
                progress(f"已校验 {sum(totals.values()):,}/{len(inventory):,} 个媒体资源。")
                validated_started = time.monotonic()
            if _valid_record(record, output, validation_cache):
                completed(item, record, True)
            else:
                existing = next((candidate for meta in item["existing_candidates"]
                                 if (candidate := _existing(meta, item, output, 1, existing_cache))), None)
                if existing:
                    completed(item, existing, True)
                elif item.get("reason"):
                    completed(item, {"status": "unsupported", "reason": item["reason"]})
                else:
                    pending_items.append(item)
        if not cancelled:
            # Pre-create shared paths sequentially before concurrent first use.
            # Every worker validates an existing directory, never a missing tree.
            incoming_by_app = {}
            staging_errors = {}
            for app in sorted({item["app"] for item in pending_items}):
                try:
                    relative = f"attachments/{app}/online/.incoming/{run.name}"
                    incoming = _media_path(output, relative)
                    incoming.mkdir(parents=True, exist_ok=True)
                    incoming_by_app[app] = str(_media_path(output, relative))
                except Exception as error:
                    staging_errors[app] = {"status": "failed", "reason": "media_staging_" + type(error).__name__,
                                          "diagnostic": _path_diagnostic(output, output / relative, error)}
            ready = []
            for item in pending_items:
                if item["app"] in staging_errors:
                    completed(item, staging_errors[item["app"]])
                else:
                    item["incoming"] = incoming_by_app[item["app"]]
                    ready.append(item)
            pending_items = ready
            iterator = iter(pending_items)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                running = {}
                def submit():
                    for _ in range(workers - len(running)):
                        item = next(iterator, None)
                        if item is None:
                            return
                        running[pool.submit(_download, item, output, run, node, cancel)] = item
                submit()
                while running:
                    finished, _ = wait(running, timeout=0.25, return_when=FIRST_COMPLETED)
                    if cancel and cancel():
                        cancelled = True
                    for future in finished:
                        item = running.pop(future)
                        try:
                            completed(item, future.result())
                        except _Cancelled as error:
                            cancelled = True
                            attempts = getattr(error, "attempts", [])
                            record = {"resource_id": item["resource_id"], "app": item["app"], "kind": item["kind"],
                                      "status": "cancelled", "reason": "cancelled", "attempts": attempts,
                                      "attempts_known": hasattr(error, "attempts")}
                            record.update(_resource_details(item, record))
                            _journal(output, record)
                        except Exception as error:
                            completed(item, {"status": "failed", "reason": "worker_" + type(error).__name__, "attempts_known": False})
                    if not cancelled:
                        submit()
        updates = run / "media-updates.jsonl"
        with updates.open("x", encoding="utf-8", newline="\n") as stream:
            for item, record in successful:
                media = {k: v for k, v in record.items() if k in MEDIA_FIELDS}
                for message in item["messages"]:
                    identity = {k: message[k] for k in ("uid", "msg_uuid", "app", "expected_raw_sha256")}
                    patch = {**identity, "local_media": [record["path"]], "original_media": [r["path"] for r in record.get("originals", [])],
                             "media": media, "media_status": "available"}
                    stream.write(json.dumps(patch, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        patch_result = _patch(output, updates, node, progress) if successful else None
        result = {"status": "cancelled" if cancelled else "completed", "totals": totals, "failures": failures,
                  "manifest": str(output / MANIFEST), "patch_result": patch_result, "recovery_result": recovery_result, "work": str(run)}
        (run / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
        return result
