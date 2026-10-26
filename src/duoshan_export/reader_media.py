"""Offline reader rendering for exported Duoshan chat history.

Renders message bodies, local media, stickers, inline emoji and restored
original bubble (nine-patch) decorations into escaped HTML plus the matching
stylesheet rules.

The decoration and media manifests are read from the export root. Both are
optional: an export that never ran the media/decorations step still renders
with the plain default bubble style.

Generated labels are English. Original message text, sender names and the
bracket emoji tokens that appear in message text are never rewritten.
"""

from __future__ import annotations

import collections
import html
import json
from pathlib import Path
import re
from urllib.parse import quote

from . import paths

# Bubbles can be stretched with CSS `border-image`; the corner artwork is
# layered on top of the nine-patch. The corner rule below keeps that artwork
# from covering the message text (text stays at z-index 1, see the stylesheet).
CORNER_Z_INDEX = 0

# Message types that may carry a chat bubble. Cards and stickers are excluded
# because the app draws them without a text bubble.
BUBBLE_MESSAGE_TYPES = frozenset({7, 8, 17, 21, 77, 83, 105, 114, 118})

INLINE_EMOJI_PATTERN = re.compile(r'\[[^\[\]\r\n]{1,35}\]')

SIDE_ME = '1'
SIDE_OTHER = '0'

_DEFAULT_TEXT_COLOR = '#202734'

_MEDIA_UNAVAILABLE_LABEL = {
    'missing_url': '备份未保存媒体地址',
}


def escaped(value: object) -> str:
    """HTML-escape a value for use in an attribute or text node."""
    return html.escape(str(value), quote=True)


def parsed(value: object) -> dict:
    """Best-effort JSON object decode; anything unusable becomes ``{}``."""
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or '{}')
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _manifest_name(key: str) -> str:
    names = getattr(paths, 'ROOT_FILES', {})
    return names.get(key, key + '.json')


def _bubble_scale(patch: dict) -> float:
    """Nine-patch slice/padding values are authored at this density scale."""
    try:
        scale = float(patch.get('scale') or 1)
    except (TypeError, ValueError):
        scale = 1.0
    return scale or 1.0


class ReaderMedia:
    """Render one export root's messages.

    A new instance is cheap and always starts with zero counters, so repeated
    export or rerender passes cannot inherit state from an earlier pass.
    """

    def __init__(self, output: str | Path) -> None:
        self.output = Path(output)
        self.reset()

    # ---------------------------------------------------------------- loading
    def reset(self) -> None:
        """Clear counters and reload the manifests from the export root."""
        self.manifest = self._read(_manifest_name('media_manifest'))
        self.decorations = self._read(_manifest_name('decorations'))
        self.bubbles = self.decorations.get('bubbles', {}) or {}
        self.inline = self.decorations.get('inline_emoji', {}) or {}
        self.counts = collections.Counter()
        self.missing_bubbles = collections.Counter()

    def reload(self) -> None:
        """Re-read the manifests after the media/decorations step rewrote them."""
        self.reset()

    def _read(self, name: str) -> dict:
        path = self.output / name
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # -------------------------------------------------------------- enriching
    def enrich(self, meta: dict, raw: dict, uid: str) -> dict:
        """Attach local media and bubble metadata for one message row."""
        resource = (self.manifest.get('messages') or {}).get(uid + ':' + str(raw.get('msg_uuid')))
        if resource:
            meta['media'] = resource
            self.counts[str(resource.get('kind')) + '_' + str(resource.get('status'))] += 1
            if resource.get('status') == 'ok':
                path = resource.get('path')
                if path and path not in meta['local_media']:
                    meta['local_media'].append(path)

        ext = parsed(raw.get('ext'))
        bubble = parsed(ext.get('a:chat_bubble'))
        original = parsed(raw.get('local_info')).get('l:original_bubble_id')

        # An explicit "-1" is the app's "no custom bubble" marker and wins over
        # the historical id so a deliberately default bubble stays default.
        explicit = bubble.get('bubble_id')
        if explicit is not None and str(explicit) == '-1':
            bubble_id, source = '-1', 'a:chat_bubble'
        elif explicit:
            bubble_id, source = str(explicit), 'a:chat_bubble'
        elif original:
            bubble_id, source = str(original), 'l:original_bubble_id'
        else:
            bubble_id, source = '-1', 'default'

        side = SIDE_ME if str(raw.get('sender')) == uid else SIDE_OTHER
        available = bool(
            (self.bubbles.get(bubble_id) or {}).get('sides', {}).get(side, {}).get('path')
        )
        eligible = raw.get('type') in BUBBLE_MESSAGE_TYPES
        meta['bubble'] = {
            'id': bubble_id,
            'side': side,
            'source': source,
            'available': available,
            'applied': bool(available and eligible),
        }
        if original:
            meta['bubble']['original_bubble_id'] = str(original)
        if eligible and available:
            self.counts['original_bubble_messages'] += 1
        elif eligible and bubble_id != '-1':
            self.missing_bubbles[bubble_id] += 1
        return meta

    # -------------------------------------------------------------- rendering
    def text(self, text: str) -> str:
        """Escape message text, replacing known bracket tokens with local emoji."""
        parts: list[str] = []
        position = 0
        for match in INLINE_EMOJI_PATTERN.finditer(text):
            asset = self.inline.get(match.group())
            if not asset or not asset.get('path'):
                continue
            parts.append(escaped(text[position:match.start()]))
            url = '../../' + quote(asset['path'])
            token = escaped(match.group())
            parts.append(
                '<img class="inline-emoji" src="' + url + '" alt="' + token
                + '" title="' + token + '" loading="lazy" decoding="async">'
            )
            self.counts['inline_emoji_occurrences'] += 1
            position = match.end()
        parts.append(escaped(text[position:]))
        return ''.join(parts)

    def render(self, msg: dict) -> str:
        """Render the bubble markup for one message."""
        raw, meta = msg['raw'], msg['meta']
        resource = meta.get('media') or {}
        bubble = meta['bubble']
        message_type = raw.get('type')
        sticker_ok = message_type == 5 and resource.get('status') == 'ok'

        classes = 'bubble'
        if sticker_ok:
            classes += ' sticker-bubble'
        if bubble.get('applied'):
            classes += ' original-bubble theme-' + str(bubble['id']) + ' side-' + str(bubble['side'])

        body = '<div class="' + classes + '"><div class="bubble-content">'
        if sticker_ok:
            url = '../../' + quote(resource['path'])
            body += (
                '<a class="sticker-link" href="' + url + '" target="_blank" rel="noopener">'
                '<img class="sticker" src="' + url + '" loading="lazy" decoding="async" alt="'
                + escaped(meta.get('text', '')) + '" title="点击查看原图"></a>'
            )
        else:
            body += self.text(meta.get('text') or '')

        if resource.get('status') == 'ok' and resource.get('kind') == 'voice':
            url = '../../' + quote(resource['path'])
            body += (
                '<audio controls preload="none" src="' + url + '" aria-label="语音消息">'
                '<a href="' + url + '">打开语音文件</a></audio>'
            )
        elif resource and resource.get('status') != 'ok':
            label = _MEDIA_UNAVAILABLE_LABEL.get(
                str(resource.get('status')),
                '原始地址已失效，未能下载',
            )
            body += '<small class="media-unavailable">' + label + '</small>'
        body += '</div>'

        if bubble.get('applied'):
            corners = (self.bubbles.get(bubble['id']) or {}).get('corners') or {}
            scale = _bubble_scale(self._patch_for(bubble))
            for corner in ('top_left', 'top_right', 'bottom_left', 'bottom_right'):
                key = corner + ('_self' if bubble['side'] == SIDE_ME else '')
                asset = corners.get(key)
                if not asset or not asset.get('path'):
                    continue
                width = float(asset.get('width') or 0) / scale
                height = float(asset.get('height') or 0) / scale
                body += (
                    '<img class="bubble-corner corner-' + corner.replace('_', '-')
                    + '" src="../../' + quote(asset['path'])
                    + '" alt="" aria-hidden="true" loading="lazy" style="width:'
                    + f'{width:g}' + 'px;height:' + f'{height:g}' + 'px">'
                )
        return body + '</div>'

    # ------------------------------------------------------------------- css
    def _patch_for(self, bubble: dict) -> dict:
        """The nine-patch metadata actually used to draw a bubble side."""
        info = self.bubbles.get(bubble['id']) or {}
        side = str(bubble['side'])
        asset = (info.get('sides') or {}).get(side) or {}
        background = (info.get('corners') or {}).get(
            'background_self' if side == SIDE_ME else 'background'
        ) or asset
        return background.get('nine_patch') or asset.get('nine_patch') or {}

    def css(self) -> str:
        """Stylesheet rules for every restored bubble style and side."""
        rules = ['.original-bubble .bubble-corner{z-index:' + str(CORNER_Z_INDEX) + '}']
        for bubble_id, info in self.bubbles.items():
            for side, asset in (info.get('sides') or {}).items():
                if 'path' not in asset or 'nine_patch' not in asset:
                    continue
                # Animated bubbles draw their plain base here and layer the
                # cached animated corner artwork on top in render().
                background = (info.get('corners') or {}).get(
                    'background_self' if side == SIDE_ME else 'background'
                ) or asset
                patch = background.get('nine_patch') or asset['nine_patch']
                scale = _bubble_scale(patch)
                selector = '.original-bubble.theme-' + str(bubble_id) + '.side-' + str(side)
                padding = ' '.join(f'{value / scale:g}px' for value in patch['padding'])
                slices = ' '.join(str(value) for value in patch['slice'])
                widths = ' '.join(f'{value / scale:g}px' for value in patch['slice'])
                color = (info.get('properties') or {}).get('tc') or ''
                if not re.fullmatch(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?', color):
                    color = _DEFAULT_TEXT_COLOR
                min_width = float(asset.get('width') or 0) / scale
                min_height = float(asset.get('height') or 0) / scale
                rules.append(
                    selector + '{padding:' + padding
                    + ';min-width:' + f'{min_width:g}' + 'px'
                    + ';min-height:' + f'{min_height:g}' + 'px'
                    + ';color:' + color + ';}'
                )
                rules.append(
                    selector + '::before{border-style:solid;border-color:transparent;border-width:'
                    + widths + ';border-image-source:url("' + quote(background['path'])
                    + '");border-image-slice:' + slices + ' fill;border-image-repeat:stretch;}'
                )
        return '\n'.join(rules)

    # ------------------------------------------------------------ statistics
    def statistics(self) -> dict:
        """Counters for the export summary, README and index page."""
        return {
            'media': self.manifest.get('statistics') or {},
            'original_bubble_styles': len(self.bubbles),
            'inline_emoji_assets': len(self.inline),
            'rendered': dict(self.counts),
            'messages_missing_original_bubble_asset': sum(self.missing_bubbles.values()),
            'missing_bubble_ids': dict(self.missing_bubbles),
        }
