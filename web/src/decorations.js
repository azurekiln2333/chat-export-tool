/**
 * Bubble (nine-patch) and inline-emoji decorations.
 *
 * This is a faithful port of the static reader's algorithm
 * (`src/duoshan_export/reader_media.py`):
 *
 *  - the nine-patch base is drawn by `::before` with `border-image`;
 *  - slice/padding values are authored at `patch.scale` density,
 *    so every pixel value is divided by `scale`;
 *  - an animated bubble draws its plain base and layers the cached corner
 *    artwork on top, with the message text kept above both;
 *  - `properties.tc` is the bubble text colour, falling back to the default
 *    when it is not a valid CSS hex colour;
 *  - `[token]` runs in message text are replaced by local inline-emoji assets.
 *
 * Generated style text only ever contains decoration metadata, never chat
 * content, so it is safe to inject into a `<style>` element.
 */

import { resolveMedia } from './media.js';

const DEFAULT_TEXT_COLOR = '#202734';
const CORNER_Z_INDEX = 0;
const HEX_COLOR = /^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$/;
export const INLINE_EMOJI_TOKEN = /\[[^\[\]\r\n]{1,35}\]/g;

/** Message types the app draws inside a bubble (cards/stickers excluded). */
export const BUBBLE_MESSAGE_TYPES = new Set([7, 8, 17, 21, 77, 83, 105, 114, 118]);

export const SIDE_ME = '1';
export const SIDE_OTHER = '0';

export const CORNERS = ['top_left', 'top_right', 'bottom_left', 'bottom_right'];

function toNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function patchScale(patch) {
  const scale = toNumber(patch && patch.scale, 1);
  return scale || 1;
}

function px(value, scale) {
  const size = toNumber(value, 0) / scale;
  return `${Number.isInteger(size) ? size : Number(size.toFixed(2))}px`;
}

function sidesOf(info) {
  return (info && info.sides) || {};
}

/**
 * The nine-patch metadata actually used to draw one bubble side: an animated
 * bubble may ship a separate `corners.background(_self)` base image.
 */
export function patchFor(decorations, bubble) {
  const info = (decorations.bubbles || {})[String(bubble.id)] || {};
  const side = String(bubble.side);
  const asset = sidesOf(info)[side] || {};
  const backgroundKey = side === SIDE_ME ? 'background_self' : 'background';
  const background = ((info.corners || {})[backgroundKey]) || asset;
  return (background && background.nine_patch) || asset.nine_patch || {};
}

/** Restored-corner artwork for one bubble side, as renderable images. */
export function cornerImages(decorations, bubble) {
  if (!bubble || !bubble.applied) return [];
  const info = (decorations.bubbles || {})[String(bubble.id)] || {};
  const corners = info.corners || {};
  const scale = patchScale(patchFor(decorations, bubble));
  const images = [];
  for (const corner of CORNERS) {
    const key = corner + (String(bubble.side) === SIDE_ME ? '_self' : '');
    const asset = corners[key];
    const src = asset ? resolveMedia(asset.path) : '';
    if (!src) continue;
    images.push({
      corner,
      src,
      className: 'corner-' + corner.replace(/_/g, '-'),
      width: px(asset.width, scale),
      height: px(asset.height, scale),
    });
  }
  return images;
}

/** True when this message type may carry a bubble and artwork exists for it. */
export function bubbleApplied(bubble) {
  return Boolean(bubble && bubble.applied);
}

/** Stylesheet text for every restored bubble style and side. */
export function buildDecorationCss(decorations) {
  const rules = [`.original-bubble .bubble-corner{z-index:${CORNER_Z_INDEX}}`];
  for (const [bubbleId, info] of Object.entries(decorations.bubbles || {})) {
    for (const [side, asset] of Object.entries(sidesOf(info))) {
      if (!asset || !asset.path || !asset.nine_patch) continue;
      const backgroundKey = side === SIDE_ME ? 'background_self' : 'background';
      const background = ((info.corners || {})[backgroundKey]) || asset;
      const patch = (background && background.nine_patch) || asset.nine_patch;
      const scale = patchScale(patch);
      const selector = `.original-bubble.theme-${bubbleId}.side-${side}`;

      const padding = (patch.padding || []).map((value) => px(value, scale)).join(' ');
      const slices = (patch.slice || []).map((value) => String(Math.round(toNumber(value, 0)))).join(' ');
      const widths = (patch.slice || []).map((value) => px(value, scale)).join(' ');
      let color = (info.properties || {}).tc || '';
      if (!HEX_COLOR.test(color)) color = DEFAULT_TEXT_COLOR;

      rules.push(
        `${selector}{padding:${padding};min-width:${px(asset.width, scale)};` +
          `min-height:${px(asset.height, scale)};color:${color};}`,
      );
      const source = resolveMedia(background.path);
      if (!source) continue;
      rules.push(
        `${selector}::before{border-style:solid;border-color:transparent;border-width:${widths};` +
          `border-image-source:url("${source}");border-image-slice:${slices} fill;` +
          'border-image-repeat:stretch;}',
      );
    }
  }
  return rules.join('\n');
}

/**
 * Split message text into safe render segments. Text segments are rendered by
 * Vue text interpolation (never `v-html`); emoji segments become `<img>`.
 * Unknown bracket runs stay part of the surrounding text, exactly like the
 * static reader.
 */
export function splitInlineEmoji(text, decorations) {
  const source = typeof text === 'string' ? text : text == null ? '' : String(text);
  const inline = (decorations && decorations.inline_emoji) || {};
  const segments = [];
  let lastIndex = 0;
  INLINE_EMOJI_TOKEN.lastIndex = 0;
  let match;
  while ((match = INLINE_EMOJI_TOKEN.exec(source)) !== null) {
    const asset = inline[match[0]];
    const src = asset ? resolveMedia(asset.path) : '';
    if (!src) continue;
    if (match.index > lastIndex) {
      segments.push({ type: 'text', value: source.slice(lastIndex, match.index) });
    }
    segments.push({ type: 'emoji', token: match[0], src });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < source.length) {
    segments.push({ type: 'text', value: source.slice(lastIndex) });
  }
  return segments;
}

/** Keep a `<style>` element in `document.head` in sync with the decorations. */
export function applyDecorationCss(css) {
  if (typeof document === 'undefined') return;
  let element = document.getElementById('duoshan-decoration-css');
  if (!element) {
    element = document.createElement('style');
    element.id = 'duoshan-decoration-css';
    document.head.appendChild(element);
  }
  element.textContent = css;
}
