/**
 * Media URL and kind helpers.
 *
 * The API serves files under `media/attachments/...`. Relative paths are used
 * on purpose (no leading slash, no hard-coded host) so the reader works behind
 * a proxy prefix and keeps working after the export is moved to a new
 * directory. `resolveMedia` refuses anything that is not a contained
 * `attachments/` reference.
 */

const MEDIA_PREFIX = 'media/';
const ALLOWED_ROOT = 'attachments/';

const IMAGE_MIME = /^image\//i;
const AUDIO_MIME = /^audio\//i;
const VIDEO_MIME = /^video\//i;

function mediaPrefix() {
  const override = typeof window !== 'undefined' ? window.__READER_MEDIA_PREFIX__ : '';
  return typeof override === 'string' && override ? override.replace(/\/+$/, '') + '/' : MEDIA_PREFIX;
}

/** Encode a relative path segment-by-segment, keeping `/` separators. */
function encodePath(path) {
  return path
    .split('/')
    .map((segment) => encodeURIComponent(segment))
    .join('/');
}

/**
 * Build a URL for a manifest path such as `attachments/voice/x.m4a`.
 * Returns '' for anything outside `attachments/` or containing traversal.
 */
export function resolveMedia(relativePath) {
  if (typeof relativePath !== 'string') return '';
  const clean = relativePath.replace(/^\/+/, '').replace(/\\/g, '/');
  if (!clean.startsWith(ALLOWED_ROOT)) return '';
  if (clean.split('/').some((segment) => segment === '..' || segment === '.' || segment === '')) {
    return '';
  }
  const absolute = /^[a-zA-Z]:/.test(clean) || clean.includes('://');
  if (absolute) return '';
  return mediaPrefix() + encodePath(clean);
}

/**
 * Decide how to render a manifest resource.
 * `kind === 'voice'` is authoritative (the app stores AAC audio as video/mpeg).
 */
export function mediaKind(resource) {
  if (!resource || typeof resource !== 'object') return 'none';
  const kind = String(resource.kind || '');
  if (kind === 'voice') return 'audio';
  const mime = String(resource.mime || '');
  if (kind === 'emoji') return IMAGE_MIME.test(mime) || !mime ? 'image' : 'file';
  if (AUDIO_MIME.test(mime)) return 'audio';
  if (IMAGE_MIME.test(mime)) return 'image';
  if (VIDEO_MIME.test(mime)) return 'video';
  if (kind === 'video') return 'video';
  if (kind === 'image') return 'image';
  return 'file';
}

export function isAvailable(resource) {
  return Boolean(resource) && resource.status === 'ok' && Boolean(resolveMedia(resource.path));
}

export const MEDIA_UNAVAILABLE_LABEL = {
  missing_url: '备份未保存媒体地址',
  not_downloaded: '尚未下载到本地，可在资料库查看下载详情',
  failed: '媒体下载失败，可在资料库查看下载详情',
  unsupported: '当前无法获取此媒体，可在资料库查看具体原因',
};

export function unavailableLabel(resource) {
  const status = String((resource && resource.status) || '');
  return MEDIA_UNAVAILABLE_LABEL[status] || '媒体不可用';
}

/** Human-readable byte size for detail panes. */
export function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let index = 0;
  let size = bytes;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 10 || index === 0 ? Math.round(size) : size.toFixed(1)} ${units[index]}`;
}
