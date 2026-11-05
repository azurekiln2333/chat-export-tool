/** Small shared helpers: debounce and display formatting. */

/** Trailing-edge debounce used by the search boxes. */
export function debounce(fn, wait = 300) {
  let timer = 0;
  return function debounced(...args) {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn.apply(this, args), wait);
  };
}

/** Trim a timestamp string for compact list display (keeps date, drops seconds). */
export function shortTime(value) {
  if (typeof value !== 'string' || !value) return '';
  return value.length > 16 ? value.slice(0, 16) : value;
}

export function plural(count, one, many) {
  return Number(count) === 1 ? one : many;
}

/** Guess a media kind from a file extension for reused local attachments. */
export function kindFromPath(path) {
  const clean = String(path || '').toLowerCase().split(/[?#]/)[0];
  const ext = clean.includes('.') ? clean.slice(clean.lastIndexOf('.') + 1) : '';
  if (['m4a', 'mp3', 'wav', 'aac', 'ogg', 'amr', 'opus', 'flac'].includes(ext)) return 'audio';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'avif'].includes(ext)) return 'image';
  if (['mp4', 'mov', 'webm', 'mkv', 'm4v'].includes(ext)) return 'video';
  return 'file';
}
/** Reader window title per exporting app; never assumes Duoshan. */
export function appTitle(label) {
  if (label === 'Xiaohongshu') return '小红书聊天记录';
  if (label === 'Duoshan') return '多闪聊天记录';
  return '聊天记录';
}
