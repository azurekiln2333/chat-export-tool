/**
 * Pure render model for one message.
 *
 * All parity-critical decisions from the static reader live here so they can be
 * checked in Node without a browser:
 *
 *  - which messages get a restored nine-patch bubble (`applied`);
 *  - stickers (`type 5` + a local image) replace the text body;
 *  - `kind === 'voice'` renders audio even though the app stores it as
 *    video/mpeg;
 *  - a resource that exists but is not `ok` (`missing_url`, `failed`, or an
 *    unsafe/absent path) still reports why it is unavailable;
 *  - text is split into escaped text runs and inline-emoji images.
 */

import { bubbleApplied, cornerImages, splitInlineEmoji } from './decorations.js';
import { isAvailable, mediaKind, resolveMedia, unavailableLabel } from './media.js';

function hasResourceData(resource) {
  if (!resource || typeof resource !== 'object') return false;
  return Boolean(resource.status || resource.path || resource.kind || resource.mime);
}

function basename(path) {
  return String(path || '').split('/').pop() || '附件';
}

export function describeMessage(message, { accountUid = '', decorations = {} } = {}) {
  const meta = message.meta || {};
  const edit = message.edit || {};
  const bubble = meta.bubble || {};
  const resource = meta.media || {};

  const own = bubble.side === '1' || message.sender === accountUid;
  const applied = bubbleApplied(bubble);
  const kind = mediaKind(resource);
  const href = resolveMedia(resource.path);
  const hasResource = hasResourceData(resource);
  const available = isAvailable(resource);
  const sticker = message.message_type === 5 && available && kind === 'image';
  const missing = hasResource && !available;

  const classes = ['bubble'];
  if (sticker) classes.push('sticker-bubble');
  if (applied) {
    classes.push('original-bubble', `theme-${bubble.id}`, `side-${bubble.side}`);
  }

  const localList = Array.isArray(meta.local_media) ? meta.local_media : [];

  return {
    meta,
    edit,
    bubble,
    resource,
    own,
    applied,
    classes,
    segments: splitInlineEmoji(message.text, decorations),
    corners: cornerImages(decorations, bubble),
    sticker,
    stickerSrc: sticker ? href : '',
    media: {
      hasResource,
      available,
      missing,
      href,
      kind,
      label: missing ? unavailableLabel(resource) : '',
      alt: String(message.text || ''),
      fileName: basename(resource.path),
    },
    localAttachments: localList.filter((path) => path && path !== resource.path),
    flags: {
      hidden: Boolean(edit.hidden),
      edited: edit.text_override !== null && edit.text_override !== undefined,
      note: Boolean(edit.note),
      modified: Boolean(edit.hidden)
        || (edit.text_override !== null && edit.text_override !== undefined)
        || Boolean(edit.note),
    },
  };
}
