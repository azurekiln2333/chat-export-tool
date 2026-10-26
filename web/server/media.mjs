/**
 * Media serving helper for the Duoshan web reader.
 *
 * Scope: exported attachment files only, addressed by an already-decoded
 * export-relative path such as `attachments/voice/voice_ab12.m4a`. Nothing
 * outside `<outputDir>/attachments` is ever readable, and no path, directory or
 * database name from the export root is exposed through errors. The attachments
 * directory itself must resolve inside the real export root, so a symlinked or
 * junctioned `attachments` cannot be used to redirect serving elsewhere.
 *
 * Entry points (see .trellis/tasks/10-27-reusable-export-tool/web-contract.md):
 *
 *   resolveMediaPath(outputDir, relativePath) -> absolute real file path
 *   serveMedia(req, res, outputDir, relativePath) -> descriptor
 *
 * Failure protocol. Problems detected before anything is written throw a
 * `MediaError` carrying `.status` and `.code` (400/403/404/500), so the caller
 * can render the standard `{error:{code,message}}` JSON. `serveMedia` owns the
 * response for the HTTP-level media outcomes that need special headers:
 * 200/206/304/416 are written here. A 416 always includes a `Content-Range`
 * of the form `bytes *` plus the total size, and the returned descriptor
 * repeats `.status`/`.code`.
 *
 * Authentication is the caller's job: this module never decides who may read a
 * file, only which file may be read and how it is transmitted.
 */

import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { pipeline } from 'node:stream/promises';

/** Attachment-relative prefix every media request must start with. */
const ATTACHMENTS_PREFIX = 'attachments';

const MAX_RELATIVE_LENGTH = 4096;

const MIME_TYPES = new Map(Object.entries({
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.avif': 'image/avif',
  '.bmp': 'image/bmp',
  '.heic': 'image/heic',
  '.heif': 'image/heif',
  '.m4a': 'audio/mp4',
  '.mp4': 'video/mp4',
  '.m4v': 'video/mp4',
  '.m4b': 'audio/mp4',
  '.mp3': 'audio/mpeg',
  '.aac': 'audio/aac',
  '.wav': 'audio/wav',
  '.ogg': 'audio/ogg',
  '.oga': 'audio/ogg',
  '.opus': 'audio/ogg',
  '.weba': 'audio/webm',
  '.flac': 'audio/flac',
  '.amr': 'audio/amr',
  '.webm': 'video/webm',
  '.mov': 'video/quicktime',
  '.mkv': 'video/x-matroska',
  '.3gp': 'video/3gpp',
  '.3g2': 'video/3gpp2',
  '.avi': 'video/x-msvideo',
  '.ts': 'video/mp2t',
}));

const CACHE_CONTROL = 'private, max-age=0, must-revalidate';
const NO_STORE = 'no-store';
const DEFAULT_TYPE = 'application/octet-stream';

/** Error with a stable HTTP status and machine-readable code. */
export class MediaError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = 'MediaError';
    this.status = status;
    this.code = code;
  }
}

function fail(status, code, message) {
  return new MediaError(status, code, message);
}

/** MIME type for a media file path; unknown types are not executable. */
export function mimeTypeFor(filePath) {
  return MIME_TYPES.get(path.extname(String(filePath)).toLowerCase()) ?? DEFAULT_TYPE;
}

/** True when `target` is strictly inside `root` (both must be absolute). */
function isContained(root, target) {
  const relative = path.relative(root, target);
  return Boolean(relative) && relative !== '..'
    && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative);
}

/**
 * Validate an export-relative media path and return its real, contained file.
 *
 * Rejects absolute paths, drive letters, backslashes, NUL/control characters,
 * empty/dot segments, `..` traversal, anything outside `attachments/`, and
 * targets whose real path leaves the real attachments directory (symlink or
 * junction escapes). Only regular files are accepted.
 */
export function resolveMediaPath(outputDir, relativePath) {
  if (typeof outputDir !== 'string' || !outputDir) {
    throw fail(500, 'invalid_output_directory', 'The media root is not configured.');
  }
  if (typeof relativePath !== 'string' || relativePath.length === 0) {
    throw fail(400, 'invalid_media_path', 'A media path is required.');
  }
  if (relativePath.length > MAX_RELATIVE_LENGTH) {
    throw fail(400, 'media_path_too_long', 'The media path is too long.');
  }
  if (/[\u0000-\u001f\u007f]/.test(relativePath)) {
    throw fail(400, 'invalid_media_path', 'The media path contains control characters.');
  }
  if (relativePath.includes('\\')) {
    throw fail(400, 'invalid_media_path', 'The media path must use forward slashes.');
  }
  if (relativePath.startsWith('/') || /^[A-Za-z]:/.test(relativePath) || relativePath.includes(':')) {
    throw fail(400, 'invalid_media_path', 'Absolute media paths are not allowed.');
  }

  const segments = relativePath.split('/');
  if (segments[0] !== ATTACHMENTS_PREFIX) {
    throw fail(403, 'media_path_not_attachments', 'Only exported attachments can be served.');
  }
  for (const segment of segments) {
    if (!segment || segment === '.' || segment === '..') {
      throw fail(400, 'invalid_media_path', 'The media path has invalid segments.');
    }
  }

  const outputRoot = path.resolve(outputDir);
  let realOutput;
  try {
    realOutput = fs.realpathSync(outputRoot);
  } catch (error) {
    if (error && (error.code === 'ENOENT' || error.code === 'ENOTDIR')) {
      throw fail(404, 'media_root_missing', 'No exported attachments are available.');
    }
    throw fail(403, 'media_root_unavailable', 'The export directory cannot be read.');
  }

  const attachmentsRoot = path.join(outputRoot, ATTACHMENTS_PREFIX);
  let realRoot;
  try {
    realRoot = fs.realpathSync(attachmentsRoot);
  } catch (error) {
    if (error && (error.code === 'ENOENT' || error.code === 'ENOTDIR')) {
      throw fail(404, 'media_root_missing', 'No exported attachments are available.');
    }
    throw fail(403, 'media_root_unavailable', 'The attachments directory cannot be read.');
  }

  // The attachments directory may itself be a symlink or junction. It is only a
  // valid trust root when its real location stays inside the real export root.
  if (!isContained(realOutput, realRoot)) {
    throw fail(403, 'media_root_escape', 'The attachments directory escapes the export.');
  }

  const candidate = path.join(realRoot, ...segments.slice(1));
  if (!isContained(realRoot, candidate)) {
    throw fail(400, 'invalid_media_path', 'The media path leaves the attachments directory.');
  }

  let realTarget;
  try {
    realTarget = fs.realpathSync(candidate);
  } catch (error) {
    const code = error && error.code;
    if (code === 'ENOENT' || code === 'ENOTDIR') {
      throw fail(404, 'media_not_found', 'The requested media file was not found.');
    }
    if (code === 'EACCES' || code === 'EPERM' || code === 'ELOOP') {
      throw fail(403, 'media_forbidden', 'The requested media file cannot be read.');
    }
    throw fail(500, 'media_unavailable', 'The requested media file is unavailable.');
  }

  if (!isContained(realRoot, realTarget)) {
    throw fail(403, 'media_symlink_escape', 'The requested media file escapes the export.');
  }

  let stat;
  try {
    stat = fs.statSync(realTarget);
  } catch {
    throw fail(404, 'media_not_found', 'The requested media file was not found.');
  }
  if (!stat.isFile()) {
    throw fail(404, 'media_not_file', 'The requested media is not a regular file.');
  }
  return realTarget;
}

/**
 * Parse a single `Range` header. Returns `{start,end}`, or `{rangeError:true}`
 * for malformed, multiple or unsatisfiable ranges (all answered with 416).
 */
export function parseByteRange(headerValue, size) {
  const raw = String(headerValue).trim();
  const matchUnit = /^bytes\s*=\s*(.*)$/i.exec(raw);
  if (!matchUnit) return { rangeError: true };
  const spec = matchUnit[1].trim();
  if (!spec || spec.includes(',')) return { rangeError: true };
  const match = /^(\d*)-(\d*)$/.exec(spec);
  if (!match) return { rangeError: true };
  const [, startText, endText] = match;
  if (startText === '' && endText === '') return { rangeError: true };

  if (startText === '') {
    const suffix = Number(endText);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) return { rangeError: true };
    if (size <= 0) return { rangeError: true };
    return { start: Math.max(0, size - suffix), end: size - 1 };
  }

  const start = Number(startText);
  if (!Number.isSafeInteger(start)) return { rangeError: true };
  let end;
  if (endText === '') {
    end = size - 1;
  } else {
    end = Number(endText);
    if (!Number.isSafeInteger(end) || end < start) return { rangeError: true };
  }
  if (size <= 0 || start >= size) return { rangeError: true };
  return { start, end: Math.min(end, size - 1) };
}

function entityTag(stat) {
  return '"' + stat.size.toString(16) + '-' + Math.floor(stat.mtimeMs).toString(16) + '"';
}

function matchesEntityTag(headerValue, etag) {
  return String(headerValue)
    .split(',')
    .map((value) => value.trim().replace(/^W\//, ''))
    .some((value) => value === '*' || value === etag.replace(/^W\//, ''));
}

function lastModified(stat) {
  return new Date(stat.mtimeMs).toUTCString();
}

function notModified(req, etag, stat) {
  const ifNoneMatch = req.headers['if-none-match'];
  if (ifNoneMatch !== undefined) {
    return matchesEntityTag(ifNoneMatch, etag);
  }
  const ifModifiedSince = req.headers['if-modified-since'];
  if (ifModifiedSince === undefined) return false;
  const since = Date.parse(String(ifModifiedSince));
  if (!Number.isFinite(since)) return false;
  return Math.floor(stat.mtimeMs / 1000) * 1000 <= since;
}

function rangeStillValid(req, etag, stat) {
  const ifRange = req.headers['if-range'];
  if (ifRange === undefined) return true;
  const value = String(ifRange).trim();
  if (value.startsWith('W/')) {
    // RFC 7233: a weak validator must not be used for If-Range, so ignore the
    // Range header and answer with the full representation instead.
    return false;
  }
  if (value.startsWith('"')) {
    return matchesEntityTag(value, etag);
  }
  const since = Date.parse(value);
  if (!Number.isFinite(since)) return false;
  return Math.floor(stat.mtimeMs / 1000) * 1000 <= since;
}

function baseHeaders(realPath, stat, etag) {
  return {
    'Content-Type': mimeTypeFor(realPath),
    'Accept-Ranges': 'bytes',
    'Cache-Control': CACHE_CONTROL,
    ETag: etag,
    'Last-Modified': lastModified(stat),
    'X-Content-Type-Options': 'nosniff',
    'Content-Disposition': 'inline',
  };
}

function writeRangeError(res, size) {
  res.writeHead(416, {
    'Content-Range': 'bytes */' + size,
    'Accept-Ranges': 'bytes',
    'Content-Type': DEFAULT_TYPE,
    'Content-Length': '0',
    'Cache-Control': NO_STORE,
    'X-Content-Type-Options': 'nosniff',
  });
  res.end();
  return { status: 416, code: 'range_not_satisfiable', size };
}

/**
 * Serve one attachment over GET or HEAD with single-range support.
 *
 * `relativePath` must already be URL-decoded by the caller. The body is always
 * streamed from an open file handle, so large videos are never buffered.
 */
export async function serveMedia(req, res, outputDir, relativePath) {
  const method = String(req.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD') {
    throw fail(400, 'method_not_allowed', 'Media is only available over GET or HEAD.');
  }

  const realPath = resolveMediaPath(outputDir, relativePath);

  let handle;
  try {
    handle = await fsp.open(realPath, 'r');
  } catch (error) {
    const code = error && error.code;
    if (code === 'ENOENT' || code === 'ENOTDIR') {
      throw fail(404, 'media_not_found', 'The requested media file was not found.');
    }
    if (code === 'EACCES' || code === 'EPERM') {
      throw fail(403, 'media_forbidden', 'The requested media file cannot be read.');
    }
    throw fail(500, 'media_unavailable', 'The requested media file is unavailable.');
  }

  let stat;
  try {
    stat = await handle.stat();
  } catch {
    await handle.close().catch(() => {});
    throw fail(500, 'media_unavailable', 'The requested media file is unavailable.');
  }
  if (!stat.isFile()) {
    await handle.close().catch(() => {});
    throw fail(404, 'media_not_file', 'The requested media is not a regular file.');
  }

  const etag = entityTag(stat);
  const headers = baseHeaders(realPath, stat, etag);

  if (notModified(req, etag, stat)) {
    await handle.close().catch(() => {});
    res.writeHead(304, { ETag: etag, 'Last-Modified': headers['Last-Modified'], 'Cache-Control': CACHE_CONTROL, 'Accept-Ranges': 'bytes' });
    res.end();
    return { status: 304, code: 'not_modified', size: stat.size };
  }

  let range = null;
  const rangeHeader = req.headers.range;
  if (rangeHeader !== undefined && rangeStillValid(req, etag, stat)) {
    const parsed = parseByteRange(rangeHeader, stat.size);
    if (parsed.rangeError) {
      await handle.close().catch(() => {});
      return writeRangeError(res, stat.size);
    }
    range = parsed;
  }

  if (range) {
    const length = range.end - range.start + 1;
    res.writeHead(206, {
      ...headers,
      'Content-Length': String(length),
      'Content-Range': 'bytes ' + range.start + '-' + range.end + '/' + stat.size,
    });
    if (method === 'HEAD') {
      await handle.close().catch(() => {});
      res.end();
      return { status: 206, code: 'partial_content', size: stat.size, start: range.start, end: range.end, length };
    }
    await streamBody(handle, res, range.start, range.end);
    return { status: 206, code: 'partial_content', size: stat.size, start: range.start, end: range.end, length };
  }

  res.writeHead(200, { ...headers, 'Content-Length': String(stat.size) });
  if (method === 'HEAD') {
    await handle.close().catch(() => {});
    res.end();
    return { status: 200, code: 'ok', size: stat.size };
  }
  await streamBody(handle, res, null, null);
  return { status: 200, code: 'ok', size: stat.size };
}

/** Stream a file range to the response; client aborts are not errors. */
async function streamBody(handle, res, start, end) {
  const options = { autoClose: true };
  if (start !== null) options.start = start;
  if (end !== null) options.end = end;
  const stream = handle.createReadStream(options);
  try {
    await pipeline(stream, res);
  } catch {
    // Headers are already sent; a broken/aborted client must not trigger JSON.
    if (!res.destroyed) res.destroy();
  }
}
