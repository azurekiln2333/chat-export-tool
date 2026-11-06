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


def _fetch(app, url, kind, run, cancel):
    """Stream bounded responses, validating the first URL and every redirect."""
    if url.startswith("http://"):
        url = "https://" + url[7:]
    for _ in range(MAX_REDIRECTS + 1):
        _check(cancel)
        if not _allowed(app, url):
            raise _ResourceError("unapproved_media_host", "unsupported")
        response = _session().get(url, timeout=(8, 30), allow_redirects=False, stream=True)
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    raise _ResourceError("redirect_without_location")
                url = urljoin(url, location)
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
            return target, response.headers.get("Content-Type", "")
        finally:
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
        account_apps = dict(database.execute("SELECT uid,app FROM library_accounts"))
        wanted = set(str(uid) for uid in (accounts or account_apps))
        unknown = wanted - account_apps.keys()
        if unknown:
            raise ExportError("Requested account is not present in this library.")
        for uid in sorted(wanted):
            app = account_apps[uid]
            progress("识别资料库媒体：" + app)
            for index, (uuid, raw_text, meta_text, typ) in enumerate(database.execute("SELECT msg_uuid,raw_json,meta_json,message_type FROM messages WHERE uid=?", (uid,))):
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
                        "expected_raw_sha256": hashlib.sha256(raw_text.encode()).hexdigest(),
                        "present_paths": list(dict.fromkeys([*local, *meta.get("original_media", []), *([primary["path"]] if primary.get("path") else [])]))})
    except sqlite3.Error as error:
        raise ExportError("Choose a managed reader library with readable account/message tables.") from error
    finally:
        database.close()
    return resources


def _download(item, output, run, node, cancel):
    if item.get("reason"):
        return {"status": "unsupported", "reason": item["reason"]}
    errors = []
    try:
        incoming = Path(item["incoming"])
        checked = _media_path(output, incoming.relative_to(output).as_posix())
        if checked != incoming or not incoming.is_dir():
            raise _ResourceError("invalid_media_staging_directory")
    except _Cancelled:
        raise
    except Exception as error:
        return {"status": "failed", "reason": "media_staging_" + type(error).__name__,
                "diagnostic": _path_diagnostic(output, Path(item.get("incoming", run)), error)}
    for url in item["urls"][:MAX_CANDIDATES]:
        try:
            path, mime = _fetch(item["app"], url, item["kind"], incoming, cancel)
            return _prepare_asset(path, mime, item["kind"], output, item["app"], incoming, node, cancel)
        except _Cancelled:
            raise
        except _ResourceError as error:
            errors.append((error.status, error.reason, error.diagnostic))
        except Exception as error:
            # Exceptions from requests/decoders can include signed URLs; never log them.
            errors.append(("failed", type(error).__name__, None))
    result = {"status": "unsupported" if errors and all(s == "unsupported" for s, _, _ in errors) else "failed",
              "reason": "; ".join(dict.fromkeys(reason for _, reason, _ in errors)) or "missing_recorded_url"}
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
        last_completion_progress = time.monotonic()
        def completed(item, record, existing=False):
            nonlocal last_completion_progress
            record = {"resource_id": item["resource_id"], "app": item["app"], "kind": item["kind"], **record}
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
                diagnostic = record.get("diagnostic") or {}
                if diagnostic.get("message") and diagnostic["message"] not in reported_diagnostics:
                    progress(diagnostic["message"])
                    reported_diagnostics.add(diagnostic["message"])
                failures.append({"resource_id": item["resource_id"], "app": item["app"], "status": record["status"],
                                 "reason": record.get("reason", "failed"), "account_uid": item["messages"][0]["uid"],
                                 "msg_uuid": item["messages"][0]["msg_uuid"],
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
                        except _Cancelled:
                            cancelled = True
                        except Exception as error:
                            completed(item, {"status": "failed", "reason": "worker_" + type(error).__name__})
                    if not cancelled:
                        submit()
        updates = run / "media-updates.jsonl"
        with updates.open("x", encoding="utf-8", newline="\n") as stream:
            for item, record in successful:
                media = {k: v for k, v in record.items() if k not in {"originals", "time", "app", "resource_id"}}
                for message in item["messages"]:
                    identity = {k: v for k, v in message.items() if k != "present_paths"}
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
