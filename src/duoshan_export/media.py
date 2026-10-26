"""Download and reuse Duoshan chat media assets for an export.

This module owns the optional media step of the export pipeline:

* :func:`download_media` rebuilds the media inventory from the already exported
  ``messages.jsonl`` files, then reuses or (only when ``config.download`` is
  true) downloads every voice/sticker resource and writes the English
  ``media_manifest.json``.
* :func:`prepare_decorations` rebuilds the original chat-bubble and inline-emoji
  decoration assets from the prepared backup and writes the English
  ``decoration_manifest.json``.

Both entry points are safe to call offline. They always merge and preserve the
records that an earlier ``--reuse-media`` import (or a previous run) already put
into the output manifest, so a valid imported success is never replaced by an
offline placeholder, and an offline call never erases imported decorations.
Network access happens only when ``config.download`` is true.

The module imports cleanly without ``requests`` or Pillow: those packages are
loaded lazily and reported with actionable install guidance only when a code
path that needs them is actually executed.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import struct
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Iterable
from urllib.parse import parse_qs, urljoin, urlsplit

from . import paths
from .errors import ExportError
from .runtime import file_sha256, write_json

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExportConfig
    from .source import SourceLayout

__all__ = ['download_media', 'prepare_decorations']

# --------------------------------------------------------------------- policy
# Hosts that served the verified Duoshan media references. Anything else is
# rejected so a crafted backup can never redirect the downloader elsewhere.
ALLOWED_HOST_SUFFIXES = (
    '.douyinstatic.com', '.douyinpic.com', '.byteimg.com', '.pstatp.com',
    '.snssdk.com', '.ibytedtos.com',
)
MAX_RESOURCE_BYTES = 64 * 1024 * 1024
MAX_REDIRECTS = 4
MAX_ATTEMPTS = 6
CONNECT_TIMEOUT = 8
READ_TIMEOUT = 35
FFPROBE_TIMEOUT = 60
FFMPEG_TIMEOUT = 600

IMAGE_SUFFIXES = {'PNG': '.png', 'JPEG': '.jpg', 'GIF': '.gif',
                  'WEBP': '.webp', 'AVIF': '.avif'}

JOURNAL_NAME = 'media_download_results.jsonl'
CACHE_DIR_NAME = 'media_cache'

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

_SESSION = threading.local()


# ------------------------------------------------------------------- small utils
def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_column(value: object) -> object:
    """Decode a stored JSON column; a dict/list value is returned unchanged."""
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _urls(value: object) -> list[str]:
    """Collect distinct http(s) references from a decoded content blob."""
    found: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            if item.startswith(('http://', 'https://')) and item not in found:
                found.append(item)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _resource_id(kind: str, key: str) -> str:
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:28]
    return kind + '_' + digest


def _account_items(output: Path, accounts: Iterable[str]) -> list[tuple[str, Path]]:
    if not output.is_dir():
        return []
    wanted = tuple(str(uid) for uid in (accounts or ()))
    result: list[tuple[str, Path]] = []
    for child in sorted(output.iterdir()):
        if not child.is_dir() or not child.name.isdecimal():
            continue
        if wanted and child.name not in wanted:
            continue
        result.append((child.name, child))
    return result


def _normalize_record(record: dict) -> dict:
    """Keep legacy journal paths English and fix HEIF-to-PNG conversion labels."""
    record = dict(record)
    relative = record.get('path')
    if relative:
        relative = paths.english_relative_path(relative)
        record['path'] = relative
        if record.get('format') in ('HEIF', 'HEIC') and relative.endswith('.png'):
            record.setdefault('source_format', record.get('format'))
            record['format'] = 'PNG'
    return record


# ----------------------------------------------------------------- inventory
def _build_inventory(config: 'ExportConfig') -> tuple[dict, set[str]]:
    """Read the exported JSONL files and group voice/sticker resources.

    Returns ``(groups, row_keys)`` where ``groups`` maps ``kind`` to a dict of
    unique resource items and ``row_keys`` is every ``uid:msg_uuid`` present.
    """
    groups: dict[str, dict] = {'voice': {}, 'emoji': {}}
    row_keys: set[str] = set()
    for uid, folder in _account_items(Path(config.output), config.accounts):
        jsonl = folder / paths.ACCOUNT_FILES['messages_jsonl']
        if not jsonl.is_file():
            continue
        try:
            lines = jsonl.read_text(encoding='utf-8').splitlines()
        except OSError as error:
            raise ExportError(f'Cannot read {jsonl}: {error}') from error
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            uuid = str(row.get('msg_uuid') or '')
            if not uuid:
                continue
            meta = row.get('_export') if isinstance(row.get('_export'), dict) else {}
            account = str(meta.get('account_uid') or uid)
            row_keys.add(account + ':' + uuid)
            try:
                msg_type = int(row.get('type'))
            except (TypeError, ValueError):
                continue
            if msg_type not in (5, 17):
                continue
            kind = 'voice' if msg_type == 17 else 'emoji'
            body = _load_column(row.get('content'))
            if not isinstance(body, dict):
                body = {}
            resource = body.get('resource_url' if kind == 'voice' else 'url') or {}
            if not isinstance(resource, dict):
                resource = {}
            links = _urls(resource)
            key = (resource.get('uri') or body.get('md5') or body.get('image_id')
                   or (links[0].split('?')[0] if links else uuid))
            key = str(key)
            item = groups[kind].setdefault(
                key, {'kind': kind, 'key': key, 'urls': [], 'messages': []})
            item['urls'] = list(dict.fromkeys(item['urls'] + links))
            item['messages'].append({'uid': account, 'msg_uuid': uuid,
                                     'time': row.get('created_time')})
    return groups, row_keys

# ----------------------------------------------------------- existing records
def _load_known(output: Path, work: Path) -> dict[str, dict]:
    """Records already available for reuse, keyed by ``resource_id``.

    Precedence (low to high) is: resumable work journal, imported message
    associations, then the completed output manifest. The output manifest wins
    because it is the current state of this export, so a verified ``--reuse-media``
    import is never shadowed by a stale journal entry from another run.
    """
    known: dict[str, dict] = {}

    def merge(record: dict) -> None:
        rid = str(record.get('resource_id') or '')
        if not rid:
            return
        normalized = _normalize_record(record)
        current = known.get(rid)
        # A verified success always wins over a failed/not_downloaded record,
        # even when the failure comes from the newer output manifest, so an
        # interrupted download resumes from whatever actually completed.
        if current is not None and _valid_ok(current, output) \
                and not _valid_ok(normalized, output):
            return
        known[rid] = normalized

    journal = Path(work) / JOURNAL_NAME
    if journal.is_file():
        try:
            lines = journal.read_text(encoding='utf-8').splitlines()
        except OSError:
            lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                merge(record)
    manifest = _read_json(Path(output) / paths.ROOT_FILES['media_manifest'])
    for entry in (manifest.get('messages') or {}).values():
        if isinstance(entry, dict):
            merge(entry)
    for record in manifest.get('resources') or []:
        if isinstance(record, dict):
            merge(record)
    return known


def _load_associations(output: Path) -> dict[str, dict]:
    manifest = _read_json(Path(output) / paths.ROOT_FILES['media_manifest'])
    messages = manifest.get('messages') or {}
    return {str(key): dict(value) for key, value in messages.items()
            if isinstance(value, dict)}


def _valid_ok(record: object, output: Path) -> bool:
    """True when a record claims success and its file really matches it.

    The file must stay inside the export tree, and every integrity value the
    record stores (size and SHA256) is verified - a matching size never hides a
    mismatching hash.
    """
    if not isinstance(record, dict) or record.get('status') != 'ok':
        return False
    relative = record.get('path')
    if not relative:
        return False
    relative = paths.english_relative_path(str(relative))
    if relative.startswith(('/', '\\')) or ':' in relative \
            or '..' in relative.replace('\\', '/').split('/'):
        return False
    root = Path(output).resolve()
    try:
        full = (root / relative).resolve()
    except OSError:
        return False
    if not full.is_relative_to(root) or not full.is_file():
        return False
    expected = record.get('bytes')
    digest = record.get('sha256')
    if expected is None and not digest:
        return False  # nothing to verify against: never trust it as a success
    if expected is not None:
        try:
            if full.stat().st_size != int(expected):
                return False
        except (OSError, TypeError, ValueError):
            return False
    if digest:
        try:
            if file_sha256(full) != str(digest):
                return False
        except OSError:
            return False
    return True


def _existing_for(item: dict, resource_id: str, known: dict[str, dict],
                  associations: dict[str, dict]) -> dict | None:
    """The best existing record for an inventory item, or ``None``."""
    for message in item['messages']:
        key = message['uid'] + ':' + message['msg_uuid']
        entry = associations.get(key)
        if not isinstance(entry, dict):
            continue
        candidate = entry.get('resource_id')
        if candidate and str(candidate) in known:
            return known[str(candidate)]
        if candidate:
            return entry
    return known.get(resource_id)


# --------------------------------------------------------------------- network
def _load_requests():
    try:
        import requests  # type: ignore import-not-found
    except ImportError as error:
        raise ExportError(
            "Downloading media needs the 'requests' package. Install it with "
            "'pip install requests', or run the media step offline."
        ) from error
    return requests


def _load_pillow(required: bool = True):
    try:
        from PIL import Image  # type: ignore import-not-found
    except ImportError as error:
        if required:
            raise ExportError(
                "Image handling needs Pillow. Install it with 'pip install Pillow', "
                "and optionally 'pip install pillow-heif' for HEIF/HEIC stickers."
            ) from error
        return None
    try:
        from pillow_heif import register_heif_opener  # type: ignore import-not-found
        register_heif_opener()
    except Exception:  # HEIF support is optional
        pass
    return Image


def _allowed(url: str) -> bool:
    parts = urlsplit(url)
    hostname = parts.hostname or ''
    return bool(
        parts.scheme == 'https' and hostname
        and any(hostname.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES)
        and not parts.username and not parts.password
        and parts.port in (None, 443)
    )


def _session():
    session = getattr(_SESSION, 'session', None)
    if session is None:
        session = _load_requests().Session()
        session.headers['User-Agent'] = 'okhttp/4.12.0'
        _SESSION.session = session
    return session


def _choose_urls(urls: Iterable[str]) -> list[str]:
    """Deduplicate candidate URLs, preferring the longest-lived signed one."""
    def expiry(url: str) -> int:
        try:
            return int(parse_qs(urlsplit(url).query).get('x-expires', ['0'])[0])
        except (ValueError, TypeError):
            return 0
    return sorted(dict.fromkeys(urls), key=expiry, reverse=True)


def _fetch(url: str) -> tuple[bytes, str, str]:
    """Fetch a media resource, validating every hop against the host policy."""
    if url.startswith('http://'):
        url = 'https://' + url[7:]
    for _ in range(MAX_REDIRECTS + 1):
        if not _allowed(url):
            raise ExportError('Unexpected media destination: ' + urlsplit(url).netloc)
        response = _session().get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                                  stream=True, allow_redirects=False)
        if response.is_redirect:
            location = response.headers.get('Location')
            response.close()
            if not location:
                raise ExportError('Media redirect had no destination')
            url = urljoin(url, location)
            continue
        try:
            response.raise_for_status()
            chunks: list[bytes] = []
            length = 0
            for chunk in response.iter_content(128 * 1024):
                length += len(chunk)
                if length > MAX_RESOURCE_BYTES:
                    raise ExportError('Media resource exceeds the size limit')
                chunks.append(chunk)
            return b''.join(chunks), url, response.headers.get('Content-Type', '')
        finally:
            response.close()
    raise ExportError('Too many redirects while fetching media')


def _probe_audio(config: 'ExportConfig', path: Path) -> tuple[dict, str]:
    command = [str(config.ffprobe), '-v', 'error',
               '-show_entries', 'stream=codec_name,codec_type',
               '-show_entries', 'format=duration,format_name',
               '-of', 'json', str(path)]
    try:
        completed = subprocess.run(command, capture_output=True,
                                   timeout=FFPROBE_TIMEOUT, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as error:
        raise ExportError(f'ffprobe is required for voice conversion: {error}') from error
    if completed.returncode:
        raise ExportError('ffprobe could not decode the downloaded audio resource')
    try:
        info = json.loads(completed.stdout.decode('utf-8', 'replace') or '{}')
    except ValueError:
        info = {}
    streams = [stream for stream in info.get('streams') or []
               if isinstance(stream, dict) and stream.get('codec_type') == 'audio']
    if not streams:
        raise ExportError('The downloaded resource has no audio stream')
    return info, str(streams[0].get('codec_name') or '')


def _prepare_audio(config: 'ExportConfig', data: bytes, stem: str) -> tuple[Path, dict]:
    """Store a voice message as a self-contained M4A/AAC file."""
    cache = Path(config.work) / CACHE_DIR_NAME
    cache.mkdir(parents=True, exist_ok=True)
    source = cache / (stem + '.bin')
    source.write_bytes(data)
    info, codec = _probe_audio(config, source)
    target = (Path(config.output) / paths.DIRECTORIES['attachments']
              / paths.DIRECTORIES['voice'] / (stem + '.m4a'))
    target.parent.mkdir(parents=True, exist_ok=True)
    container = str((info.get('format') or {}).get('format_name') or '')
    if codec == 'aac' and 'mov,mp4' in container:
        target.write_bytes(data)
    else:
        command = [str(config.ffmpeg), '-v', 'error', '-nostdin', '-y', '-i', str(source),
                   '-vn', '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', str(target)]
        try:
            completed = subprocess.run(command, capture_output=True,
                                       timeout=FFMPEG_TIMEOUT, creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as error:
            raise ExportError(f'ffmpeg is required for voice conversion: {error}') from error
        if completed.returncode:
            raise ExportError('ffmpeg could not convert the voice resource')
    try:
        duration = float((info.get('format') or {}).get('duration') or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return target, {'codec': codec, 'duration': duration, 'format': 'M4A'}


def _prepare_image(data: bytes, stem: str, output: Path) -> tuple[Path, dict]:
    """Store a sticker, keeping its animation and original container when known."""
    Image = _load_pillow()
    with Image.open(io.BytesIO(data)) as image:
        image_format = image.format
        width, height = image.size
        frames = getattr(image, 'n_frames', 1)
        image.verify()
    suffix = IMAGE_SUFFIXES.get(str(image_format))
    target = (output / paths.DIRECTORIES['attachments']
              / paths.DIRECTORIES['stickers'] / (stem + (suffix or '.png')))
    target.parent.mkdir(parents=True, exist_ok=True)
    if suffix:
        target.write_bytes(data)
        details = {'format': image_format, 'source_format': image_format,
                   'width': width, 'height': height, 'frames': frames}
    else:
        with Image.open(io.BytesIO(data)) as image:
            image.convert('RGBA').save(target, format='PNG')
        details = {'format': 'PNG', 'source_format': image_format,
                   'width': width, 'height': height, 'frames': 1}
    return target, details


def _download_item(config: 'ExportConfig', item: dict, resource_id: str) -> dict:
    """Download one unique resource and return its manifest record."""
    requests = _load_requests()
    output = Path(config.output)
    base = {'kind': item['kind'], 'key': item['key'], 'resource_id': resource_id,
            'message_count': len(item['messages'])}
    if not item['urls']:
        return {**base, 'status': 'missing_url'}
    errors: list[str] = []
    for url in _choose_urls(item['urls'])[:MAX_ATTEMPTS]:
        try:
            data, actual, mime = _fetch(url)
            if item['kind'] == 'voice':
                target, details = _prepare_audio(config, data, resource_id)
            else:
                target, details = _prepare_image(data, resource_id, output)
            return {**base, 'status': 'ok',
                    'path': target.relative_to(output).as_posix(),
                    'bytes': target.stat().st_size, 'source_bytes': len(data),
                    'source_sha256': hashlib.sha256(data).hexdigest(),
                    'sha256': file_sha256(target), 'source_url': actual,
                    'mime': mime, **details}
        except requests.HTTPError as error:
            errors.append('HTTP ' + str(getattr(error.response, 'status_code', '?')))
        except requests.RequestException as error:
            errors.append(type(error).__name__)
        except ExportError as error:
            errors.append('rejected: ' + str(error)[:120])
        except Exception as error:  # a broken file must not abort the whole export
            errors.append(type(error).__name__ + ': ' + str(error)[:120])
    return {**base, 'status': 'failed', 'errors': errors}


# ------------------------------------------------------------------- assemble
def _message_entry(record: dict) -> dict:
    """Per-message association entry: the resource result without internals."""
    excluded = ('source_url', 'key', 'source_sha256', 'sha256', 'errors')
    return {key: value for key, value in record.items() if key not in excluded}


def _statistics(records: Iterable[dict]) -> dict:
    records = list(records)
    statistics: dict[str, dict] = {}
    for kind in ('voice', 'emoji'):
        selected = [record for record in records if record.get('kind') == kind]
        successful = [record for record in selected if record.get('status') == 'ok']
        statistics[kind] = {
            'unique_total': len(selected),
            'unique_downloaded': len(successful),
            'message_total': sum(int(record.get('message_count') or 0) for record in selected),
            'messages_downloaded': sum(int(record.get('message_count') or 0)
                                       for record in successful),
            'bytes': sum(int(record.get('bytes') or 0) for record in successful),
            'animated_unique': sum(1 for record in successful
                                   if int(record.get('frames') or 1) > 1),
        }
    return statistics


def _write_journal(work: Path, records: Iterable[dict]) -> None:
    journal = Path(work) / JOURNAL_NAME
    journal.parent.mkdir(parents=True, exist_ok=True)
    temporary = journal.with_name(journal.name + '.tmp')
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    temporary.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')
    os.replace(temporary, journal)


def download_media(config: 'ExportConfig') -> dict:
    """Rebuild the media manifest, reusing everything already available.

    Existing valid records (including those copied in by ``--reuse-media``) are
    preserved on both the offline and online paths. A resource is only fetched
    when downloads are enabled and no usable local copy exists.
    """
    output = Path(config.output)
    work = Path(config.work)
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    groups, row_keys = _build_inventory(config)
    items = [item for kind in ('voice', 'emoji') for item in groups[kind].values()]
    known = _load_known(output, work)
    associations = _load_associations(output)

    records: dict[str, dict] = {}
    pending: list[dict] = []
    reused = 0
    for item in items:
        rid = _resource_id(item['kind'], item['key'])
        existing = _existing_for(item, rid, known, associations)
        if isinstance(existing, dict) and existing.get('resource_id'):
            rid = str(existing['resource_id'])
        item['_resource_id'] = rid
        if _valid_ok(existing, output):
            record = _normalize_record(dict(existing))
            record.update(kind=item['kind'], key=item.get('key'), resource_id=rid,
                          message_count=len(item['messages']))
            records[rid] = record
            reused += 1
        elif config.download:
            pending.append(item)
        elif isinstance(existing, dict) and existing.get('status') \
                and existing.get('status') != 'ok':
            record = _normalize_record(dict(existing))
            record.update(kind=item['kind'], key=item.get('key'), resource_id=rid,
                          message_count=len(item['messages']))
            record.pop('path', None)
            records[rid] = record
        else:
            status = 'missing_url' if not item['urls'] else 'not_downloaded'
            records[rid] = {'kind': item['kind'], 'key': item.get('key'),
                            'resource_id': rid, 'message_count': len(item['messages']),
                            'status': status}

    downloaded = 0
    if pending:
        workers = max(1, min(int(getattr(config, 'workers', 6) or 6), 32))
        journal = work / JOURNAL_NAME
        journal.parent.mkdir(parents=True, exist_ok=True)
        # Append + flush every finished resource so a crash keeps its progress.
        with journal.open('a', encoding='utf-8', newline='\n') as stream, \
                ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_download_item, config, item, item['_resource_id']): item
                       for item in pending}
            for future in as_completed(futures):
                record = future.result()
                records[record['resource_id']] = record
                downloaded += 1
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                stream.flush()

    message_entries: dict[str, dict] = {}
    covered: set[str] = set()
    for item in items:
        record = records[item['_resource_id']]
        for message in item['messages']:
            key = message['uid'] + ':' + message['msg_uuid']
            covered.add(key)
            message_entries[key] = _message_entry(record)

    # Keep imported successes whose message the inventory no longer groups: a
    # matching manifest must never lose media just because a heuristic changed.
    for key, entry in associations.items():
        if key in covered or key not in row_keys:
            continue
        rid = str(entry.get('resource_id') or '')
        if not rid:
            continue
        if rid not in records:
            source = known.get(rid)
            if _valid_ok(source, output) or (source and source.get('status') != 'ok'):
                record = _normalize_record(dict(source))
            else:
                record = _normalize_record(dict(entry))
            record.setdefault('kind', entry.get('kind'))
            record['resource_id'] = rid
            if not _valid_ok(record, output):
                record.pop('path', None)
                if record.get('status') == 'ok':
                    record['status'] = 'not_downloaded'
            records[rid] = record
        message_entries[key] = dict(entry)
        message_entries[key]['resource_id'] = rid
        covered.add(key)

    counts: dict[str, int] = {}
    for entry in message_entries.values():
        rid = str(entry.get('resource_id') or '')
        if rid:
            counts[rid] = counts.get(rid, 0) + 1
    resources = []
    for rid, record in records.items():
        record['message_count'] = counts.get(rid, int(record.get('message_count') or 0))
        resources.append(record)
    resources.sort(key=lambda record: (str(record.get('kind')),
                                       str(record.get('resource_id') or '')))

    manifest = {
        'resources': resources,
        'messages': {key: message_entries[key] for key in sorted(message_entries)},
        'statistics': _statistics(resources),
    }
    write_json(output / paths.ROOT_FILES['media_manifest'], manifest)
    _write_journal(work, resources)

    status_counts: dict[str, int] = {}
    for record in resources:
        status = str(record.get('status') or 'unknown')
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        'output': str(output),
        'mode': 'download' if config.download else 'offline',
        'resources': len(resources),
        'messages': len(message_entries),
        'status_counts': status_counts,
        'statistics': manifest['statistics'],
        'reused': reused,
        'downloaded': downloaded,
        'manifest': str(output / paths.ROOT_FILES['media_manifest']),
    }

# ------------------------------------------------------------- decorations
# The original bubble cache is a Keva key/value store (a ``.blk`` text arena
# plus a ``.chk`` table of 8-byte entries). The offsets below are the verified
# layout of ``bubble_detail_info`` on this Android build; unknown entries are
# skipped instead of guessed.
def _blk_offset(index: int) -> int:
    if index < 64:
        return 48 + index * 16
    if index < 128:
        return 1072 + (index - 64) * 32
    if index < 384:
        return 3120 + (index - 128) * 64
    raise ValueError(index)


def _blk_text(blk: bytes, index: int) -> str:
    position = _blk_offset(index)
    if position + 4 > len(blk):
        raise ValueError('bubble cache reference is out of range')
    length = struct.unpack_from('<I', blk, position)[0]
    if length >> 24 != 0x81:
        raise ValueError('unexpected bubble cache reference marker')
    count = length & 0xffffff
    if not 0 < count < len(blk) - position - 4:
        raise ValueError('bubble cache text is out of range')
    return blk[position + 4:position + 4 + count].decode('utf-8', 'replace')


def _read_bubble_cache(directory: Path) -> dict:
    """Decode the bubble cache into a key/value mapping, or ``{}``."""
    directory = Path(directory)
    try:
        blk = (directory / 'bubble_detail_info.blk').read_bytes()
        chk = (directory / 'bubble_detail_info.chk').read_bytes()
    except OSError:
        return {}
    result: dict = {}
    for position in range(0x128, len(chk) - 7, 8):
        first, second = struct.unpack_from('<II', chk, position)
        if not first:
            continue
        kind = first >> 24
        if kind not in (0x81, 0x84, 0x86, 0x87):
            continue
        try:
            key = _blk_text(blk, first & 0xffffff)
            if kind == 0x81:
                value = struct.unpack('<i', struct.pack('<I', second))[0]
            elif kind == 0x84:
                value = struct.unpack('<f', struct.pack('<I', second))[0]
            elif kind == 0x86:
                if second >> 24 != 1:
                    continue
                value = _blk_text(blk, second & 0xffffff)
            else:
                value = {'unparsed_array_ref': hex(second)}
        except (ValueError, struct.error):
            continue
        result.setdefault(key, value)
    return result


def _empty_bubble() -> dict:
    return {'sides': {}, 'corners': {}, 'properties': {}}


def _apply_bubble_values(bubbles: dict, values: dict) -> dict:
    """Fold cache entries into bubble metadata and return the wanted side URLs."""
    wanted: dict = {}
    for key, value in values.items():
        if '__' in key and key[:19].isdecimal():
            bubble_id, prop = key.split('__', 1)
            bubbles.setdefault(bubble_id, _empty_bubble())['properties'][prop] = value
            continue
        if key.endswith(('_1_bg', '_0_bg')):
            parts = key.split('_')
            if len(parts) >= 3 and parts[0][:19].isdecimal():
                wanted[(parts[0], parts[1])] = value
            continue
        if key[:19].isdecimal():
            parts = key.split('_')
            if len(parts) == 3:
                bubble_id, side, prop = parts
                info = bubbles.setdefault(bubble_id, _empty_bubble())
                info.setdefault('padding', {}).setdefault(side, {})[prop] = value
    return wanted


def _png_chunks(data: bytes) -> dict:
    chunks: dict = {}
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return chunks
    position = 8
    while position + 12 <= len(data):
        size = struct.unpack_from('>I', data, position)[0]
        kind = data[position + 4:position + 8].decode('ascii', 'replace')
        chunks[kind] = data[position + 8:position + 8 + size]
        position += size + 12
    return chunks


def _nine_patch(chunks: dict, width: int, height: int) -> dict | None:
    patch = chunks.get('npTc')
    if not patch or len(patch) < 48 or patch[1] != 2 or patch[2] != 2:
        return None
    try:
        left, right, top, bottom = struct.unpack_from('>4i', patch, 12)
        x1, x2, y1, y2 = struct.unpack_from('>4i', patch, 32)
    except struct.error:
        return None
    if not (0 < x1 < x2 < width and 0 < y1 < y2 < height):
        return None
    return {'padding': [top, right, bottom, left],
            'slice': [y1, width - x2, height - y2, x1], 'scale': 3}


def _manual_size(data: bytes) -> dict:
    """Header-only image size reader used when Pillow is unavailable."""
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        width, height = struct.unpack_from('>II', data, 16)
        return {'format': 'PNG', 'width': width, 'height': height}
    if data[:6] in (b'GIF87a', b'GIF89a') and len(data) >= 10:
        width, height = struct.unpack_from('<HH', data, 6)
        return {'format': 'GIF', 'width': width, 'height': height}
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        if data[12:16] == b'VP8X' and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], 'little')
            height = 1 + int.from_bytes(data[27:30], 'little')
            return {'format': 'WEBP', 'width': width, 'height': height}
        if data[12:16] == b'VP8 ' and len(data) >= 30:
            width = int.from_bytes(data[26:28], 'little') & 0x3fff
            height = int.from_bytes(data[28:30], 'little') & 0x3fff
            return {'format': 'WEBP', 'width': width, 'height': height}
    if data[:2] == b'\xff\xd8':
        position = 2
        while position + 9 < len(data):
            if data[position] != 0xff:
                position += 1
                continue
            marker = data[position + 1]
            if marker in (0xd8, 0xd9):
                position += 2
                continue
            length = struct.unpack_from('>H', data, position + 2)[0]
            if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
                height, width = struct.unpack_from('>HH', data, position + 5)
                return {'format': 'JPEG', 'width': width, 'height': height}
            position += 2 + length
    return {}


def _image_info(data: bytes) -> dict:
    Image = _load_pillow(required=False)
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as image:
                return {'format': image.format, 'width': image.width,
                        'height': image.height, 'frames': getattr(image, 'n_frames', 1)}
        except Exception:  # fall back to header parsing for odd files
            pass
    return _manual_size(data)


def _store_asset(data: bytes, target: Path, output: Path, *, source_url: str | None = None,
                 fallback_size: tuple | None = None, version=None) -> dict:
    """Write an asset and build manifest metadata, including the nine-patch."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    metadata: dict = {'path': target.relative_to(output).as_posix(),
                      'sha256': hashlib.sha256(data).hexdigest()}
    info = _image_info(data)
    width = info.get('width')
    height = info.get('height')
    if width is None and fallback_size:
        width = fallback_size[0]
    if height is None and fallback_size:
        height = fallback_size[1]
    if width is not None:
        metadata['width'] = int(width)
    if height is not None:
        metadata['height'] = int(height)
    frames = info.get('frames')
    if frames is not None:
        metadata['frames'] = int(frames)
    if source_url:
        metadata['source_url'] = source_url
    if version is not None:
        metadata['version'] = version
    chunks = _png_chunks(data)
    if chunks:
        metadata['png_chunks'] = {kind: (payload.hex() if kind == 'npTc' else len(payload))
                                  for kind, payload in chunks.items()
                                  if kind not in ('IDAT', 'IEND')}
        if width and height:
            patch = _nine_patch(chunks, int(width), int(height))
            if patch:
                metadata['nine_patch'] = patch
    return metadata


def _valid_asset(asset: object, output: Path) -> bool:
    if not isinstance(asset, dict) or not asset.get('path'):
        return False
    relative = paths.english_relative_path(str(asset['path']))
    try:
        return (Path(output) / relative).is_file()
    except OSError:
        return False


def _layout_field(layout, name: str):
    value = getattr(layout, name, None) if layout is not None else None
    return Path(value) if value else None


def prepare_decorations(config: 'ExportConfig', layout: 'SourceLayout') -> dict:
    """Rebuild bubble and inline-emoji assets, merging anything already imported.

    Reused entries from a previous export survive untouched; local assets from a
    supplied backup are added on top; bubble backgrounds are only fetched from
    the network when ``config.download`` is true.
    """
    output = Path(config.output)
    output.mkdir(parents=True, exist_ok=True)
    existing = _read_json(output / paths.ROOT_FILES['decorations'])
    bubbles = copy.deepcopy(existing.get('bubbles') or {})
    inline = copy.deepcopy(existing.get('inline_emoji') or {})
    for bubble_id, info in list(bubbles.items()):
        if not isinstance(info, dict):
            info = {}
            bubbles[bubble_id] = info
        info.setdefault('sides', {})
        info.setdefault('corners', {})
        info.setdefault('properties', {})

    downloaded = 0
    local = 0
    bubbles_path = _layout_field(layout, 'bubbles_path')
    if bubbles_path is not None:
        wanted = _apply_bubble_values(bubbles, _read_bubble_cache(bubbles_path))
        for (bubble_id, side), url in sorted(wanted.items()):
            info = bubbles.setdefault(bubble_id, _empty_bubble())
            info.setdefault('sides', {})
            info.setdefault('corners', {})
            info.setdefault('properties', {})
            current = info['sides'].get(side)
            if _valid_asset(current, output):
                continue
            target = (output / paths.DIRECTORIES['attachments']
                      / paths.DIRECTORIES['bubbles'] / bubble_id
                      / ('self.png' if str(side) == '1' else 'guest.png'))
            if target.is_file():
                try:
                    payload = target.read_bytes()
                except OSError:
                    payload = b''
                if payload:
                    info['sides'][side] = _store_asset(payload, target, output,
                                                       source_url=str(url))
                    local += 1
                    continue
            if config.download and url:
                try:
                    payload, actual, _ = _fetch(str(url))
                    info['sides'][side] = _store_asset(payload, target, output,
                                                       source_url=actual)
                    downloaded += 1
                    continue
                except Exception as error:
                    record = dict(current) if isinstance(current, dict) else {}
                    record.pop('path', None)
                    record['status'] = 'failed'
                    record['source_url'] = str(url)
                    record['error'] = str(error)[:180]
                    info['sides'][side] = record
                    continue
            record = dict(current) if isinstance(current, dict) else {}
            record.pop('path', None)
            record.setdefault('status', 'not_downloaded')
            record.setdefault('source_url', str(url))
            info['sides'][side] = record

    im_resources = _layout_field(layout, 'im_resources_dir')
    if im_resources is not None:
        root = im_resources / 'chat_bubble'
        if root.is_dir():
            for bubble_dir in sorted(root.iterdir()):
                animation = bubble_dir / 'animation'
                if not bubble_dir.is_dir() or not animation.is_dir():
                    continue
                info = bubbles.setdefault(bubble_dir.name, _empty_bubble())
                info.setdefault('corners', {})
                for source in sorted(animation.iterdir()):
                    if not source.is_file() or source.suffix.lower() not in ('.png', '.webp'):
                        continue
                    try:
                        payload = source.read_bytes()
                    except OSError:
                        continue
                    target = (output / paths.DIRECTORIES['attachments']
                              / paths.DIRECTORIES['bubbles'] / bubble_dir.name
                              / paths.asset_filename(source.name))
                    try:
                        info['corners'][source.stem] = _store_asset(payload, target, output)
                    except OSError:
                        continue
                    local += 1

    small_emoji = _layout_field(layout, 'small_emoji_dir')
    if small_emoji is not None and small_emoji.is_dir():
        documents = []
        for info_path in small_emoji.glob('**/info.json'):
            try:
                document = json.loads(info_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if isinstance(document, dict):
                documents.append((int(document.get('version') or 0), info_path, document))
        documents.sort(key=lambda item: item[0])
        for _, info_path, document in documents:
            for sticker in document.get('stickers') or []:
                if not isinstance(sticker, dict):
                    continue
                name = sticker.get('display_name')
                uri = str(sticker.get('uri') or '')
                if not name or not uri:
                    continue
                source = info_path.parent / 'static' / uri
                if not source.is_file():
                    continue
                try:
                    payload = source.read_bytes()
                except OSError:
                    continue
                target = (output / paths.DIRECTORIES['attachments']
                          / paths.DIRECTORIES['inline_emoji']
                          / paths.asset_filename(Path(uri).name))
                try:
                    inline[str(name)] = _store_asset(
                        payload, target, output,
                        fallback_size=(sticker.get('width'), sticker.get('height')),
                        version=document.get('version'))
                except OSError:
                    continue
                local += 1

    manifest = {'bubbles': bubbles, 'inline_emoji': inline}
    write_json(output / paths.ROOT_FILES['decorations'], manifest)
    sides = sum(1 for info in bubbles.values() if isinstance(info, dict)
                for asset in (info.get('sides') or {}).values() if asset.get('path'))
    corners = sum(1 for info in bubbles.values() if isinstance(info, dict)
                  for asset in (info.get('corners') or {}).values() if asset.get('path'))
    return {
        'output': str(output),
        'mode': 'download' if config.download else 'offline',
        'bubbles': len(bubbles),
        'inline_emoji': len(inline),
        'bubble_sides': sides,
        'bubble_corners': corners,
        'downloaded': downloaded,
        'local_assets': local,
        'manifest': str(output / paths.ROOT_FILES['decorations']),
    }
