"""Download Xiaohongshu chat media referenced by an export into ``attachments/``.

Xiaohongshu's IM keeps photos and videos on its CDN; the local backup stores
only the message rows with their original URLs. This module reads every
``<uid>/messages.jsonl`` of an export, collects the distinct media addresses
(``msg-img.xhscdn.com``, ``msg-video.xhscdn.com``, avatars, note covers) and
downloads each once into ``attachments/<uid>/<sha1>.<ext>``. A
``media_manifest.json`` at the export root records per-URL status, size, hash
and the referencing messages, so the exporter can embed local paths and a
later run can resume (existing files with the recorded size are kept).

Downloads are strictly opt-in: nothing runs unless this module is invoked
(``python -m duoshan_export.apps.xhs_media EXPORT``). Every request is a plain
GET of a URL that already appears in the user's own backup — no URLs are
invented, no headers beyond a browser-like User-Agent. Failures are recorded
per URL in the manifest instead of aborting the run.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..errors import ExportError
from ..paths import DIRECTORIES

MANIFEST_NAME = "media_manifest.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

URL_RE = re.compile(r'https://[^\s",\'\\]+')
# Hosts that carry user content worth archiving; feed/business hosts are skipped.
MEDIA_HOST_MARKERS = (
    "msg-img.", "msg-video.", "sns-avatar-", "sns-img.", "picasso-static",
    "sns-na-i", "image-url-2-feature", "fe-video-qc", "fe-platform.",
)
# Image/video variants worth both the preview and the original.
VARIANT_IGNORE = ("imageView2",)

MAGIC_EXTS = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"RIFF", ".webp"),   # RIFF....WEBP
    (b"ftyp", ".mp4"),    # isobmff: offset 4
    (b"ID3", ".mp3"),
    (b"\xff\xfb", ".mp3"),
    (b"\xff\xf3", ".mp3"),
)

REQUEST_TIMEOUT = 60
CHUNK = 1 << 16


def sniff_ext(head: bytes, content_type: str) -> str:
    """File extension from magic bytes, falling back to the content type."""
    for magic, ext in MAGIC_EXTS:
        if head.startswith(magic):
            if ext == ".webp" and head[8:12] != b"WEBP":
                continue
            if ext == ".mp4" and head[4:8] != b"ftyp":
                continue
            return ext
    ctype = content_type.split(";")[0].strip().lower()
    table = {"image/webp": ".webp", "image/jpeg": ".jpg", "image/png": ".png",
             "image/gif": ".gif", "image/heic": ".heic", "video/mp4": ".mp4",
             "audio/mp4": ".m4a", "audio/mpeg": ".mp3"}
    return table.get(ctype, ".bin")


def _local_name(url: str, ext: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20] + ext


def scan_export(export_root: Path) -> dict[str, dict[str, Any]]:
    """Distinct media URLs across all account JSONL files, with referencers.

    Xiaohongshu nests its message payloads several JSON layers deep (a
    ``content`` string containing JSON containing JSON...), where URLs appear
    escaped as ``https:\\/\\/...``. Every string in the row is therefore
    JSON-decoded recursively and each decoded layer is scanned, so URLs are
    found at any depth with their real characters.
    """
    refs: dict[str, dict[str, Any]] = {}
    for jsonl in sorted(export_root.glob("*/messages.jsonl")):
        uid = jsonl.parent.name
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            uuid = row.get("uuid") or row.get("msg_uuid") or ""
            for url in _iter_urls(row):
                host = urlsplit(url).netloc
                if not any(marker in host for marker in MEDIA_HOST_MARKERS):
                    continue
                key = _canonical_url(url)
                entry = refs.setdefault(key, {"uid": uid, "messages": [],
                                              "url": key})
                entry["uid"] = uid  # media belongs to the account whose jsonl it appeared in
                if uuid and uuid not in entry["messages"]:
                    entry["messages"].append(uuid)
    return refs


def _iter_urls(node: Any, _depth: int = 0) -> Iterator[str]:
    """Yield every http(s) URL found in ``node`` at any JSON nesting depth."""
    if _depth > 12:
        return
    if isinstance(node, dict):
        for value in node.values():
            yield from _iter_urls(value, _depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_urls(value, _depth + 1)
    elif isinstance(node, str):
        for raw_url in URL_RE.findall(node):
            yield raw_url.rstrip('",]}')
        text = node.strip()
        if text[:1] in ('{', '[', '"'):
            try:
                decoded = json.loads(text)
            except ValueError:
                return
            yield from _iter_urls(decoded, _depth + 1)


def _canonical_url(url: str) -> str:
    """Collapse resize/format query variants of a CDN URL to one download.

    ``msg-img``/``msg-video`` queries are server-side resize hints
    (``?imageView2/...``); the bare path serves the original, so one fetch
    per base path is enough and every variant maps to the same file.
    """
    host = urlsplit(url).netloc
    if any(marker in host for marker in ("msg-img.", "msg-video.")):
        return url.split("?", 1)[0]
    return url


def load_manifest(export_root: Path) -> dict[str, Any]:
    path = export_root / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_manifest(export_root: Path, manifest: dict[str, Any]) -> None:
    path = export_root / MANIFEST_NAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    temporary.replace(path)


def download_one(url: str, destination: Path) -> dict[str, Any]:
    """Download ``url`` to ``destination``; returns a manifest record."""
    import urllib.request

    record: dict[str, Any] = {"url": url, "file": None, "status": "pending",
                              "size": None, "sha1": None, "error": None}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read()
        if not data:
            record["status"] = "empty"
            record["error"] = "0 bytes"
            return record
        ext = sniff_ext(data[:16], content_type)
        name = _local_name(url, ext)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / name
        target.write_bytes(data)
        record.update({
            "file": f"{DIRECTORIES['attachments']}/{destination.name}/{name}",
            "status": "ok", "size": len(data),
            "sha1": hashlib.sha1(data).hexdigest(),
            "content_type": content_type,
        })
    except Exception as error:  # per-URL failure must not kill the run
        record["status"] = "failed"
        record["error"] = f"{type(error).__name__}: {error}"
    return record


def download_media(export_root: str | Path, workers: int = 8,
                   only_hosts: tuple[str, ...] = ()) -> dict[str, Any]:
    """Download every referenced media URL of ``export_root``; resume-aware.

    Returns summary counts; writes ``media_manifest.json`` at the export root.
    Existing files recorded as ``ok`` in the manifest are not re-downloaded.
    """
    export_root = Path(export_root)
    if not export_root.is_dir():
        raise ExportError(f"Export directory not found: {export_root}")
    refs = scan_export(export_root)
    manifest = load_manifest(export_root)
    old_records = manifest.get("media") if isinstance(manifest.get("media"), dict) else {}

    todo: list[str] = []
    for url in refs:
        previous = old_records.get(url)
        if previous and previous.get("status") == "ok":
            path = export_root / str(previous.get("file") or "")
            if path.is_file() and path.stat().st_size == previous.get("size"):
                continue  # already downloaded
        if only_hosts and not any(host in url for host in only_hosts):
            continue
        todo.append(url)

    results: dict[str, dict[str, Any]] = {u: r for u, r in old_records.items()
                                          if r.get("status") == "ok"}
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for url in todo:
            uid = refs[url]["uid"]
            destination = export_root / DIRECTORIES["attachments"] / uid
            futures[pool.submit(download_one, url, destination)] = url
        for future in concurrent.futures.as_completed(futures):
            record = future.result()
            results[futures[future]] = record
            if record["status"] != "ok":
                failures += 1

    manifest = {
        "format": 1,
        "app": "xhs",
        "downloaded_at": _now_iso(),
        "requested": len(todo),
        "ok": sum(1 for r in results.values() if r.get("status") == "ok"),
        "failed": failures,
        "total_bytes": sum(r.get("size") or 0 for r in results.values()
                           if r.get("status") == "ok"),
        "media": results,
    }
    save_manifest(export_root, manifest)
    return {k: manifest[k] for k in ("requested", "ok", "failed", "total_bytes")}


def local_lookup(export_root: str | Path) -> dict[str, str]:
    """``{url: relative path from export root}`` for successfully downloaded media.

    Both the canonical (query-less) URL and every manifest key are indexed, so
    resize/format variants of the same image resolve to the downloaded file.
    """
    manifest = load_manifest(Path(export_root))
    media = manifest.get("media") if isinstance(manifest.get("media"), dict) else {}
    lookup: dict[str, str] = {}
    for url, record in media.items():
        if record.get("status") == "ok" and record.get("file"):
            lookup[url] = record["file"]
            lookup[url.split("?", 1)[0]] = record["file"]
    return lookup


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m duoshan_export.apps.xhs_media",
        description="Download the CDN media (photos/videos) referenced by a Xiaohongshu export.")
    parser.add_argument("export", type=Path, help="Export directory produced by 'export'")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent downloads (default: 8)")
    args = parser.parse_args(argv)
    summary = download_media(args.export, workers=args.workers)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
