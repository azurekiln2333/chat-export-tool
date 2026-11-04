"""Minimal read-only MMKV file reader used to locate app-managed secrets.

Several Android apps keep their database passphrases in MMKV key-value files
(for example ``files/mmkv/<instance>``). This module parses the on-disk MMKV
append-log format without any native dependency:

    [uint32 little-endian actual size]
    [optional varint ItemSizeHolder 0x0FFFFFFF]
    then repeated entries of  [varint key length][key][varint value length][value]

Keys are UTF-8. Values are raw bytes; the app's byte-array helper stores an
inner ``[varint length][bytes]`` prefix inside the value blob, so
:func:`decode_bytes` strips it when present. Like MMKV itself, the *last*
valid entry for a key wins; entries whose header cannot be parsed are treated
as end-of-log garbage.

Only plain (unencrypted) MMKV files are supported: the reader refuses files
that do not look like the append-log format instead of guessing. The original
file is opened read-only and never modified.
"""

from __future__ import annotations

import os
from pathlib import Path
import struct

ITEM_SIZE_HOLDER = b"\xff\xff\xff\x07"


def _read_varint(buf: bytes, pos: int, end: int) -> tuple[int, int] | None:
    """Decode one little-endian base-128 varint; ``None`` when malformed."""
    result = 0
    shift = 0
    start = pos
    while pos < end:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 35:
            return None
    _ = start
    return None


def parse_entries(path: os.PathLike[str] | str) -> list[tuple[str, bytes]]:
    """Return every valid ``(key, value)`` entry in file order.

    Repeated keys are kept: callers that want the effective value should use
    :func:`load` (last one wins), while callers that need the append history
    can walk the list themselves.
    """
    data = Path(path).read_bytes()
    if len(data) < 4:
        return []
    declared = struct.unpack_from("<I", data, 0)[0]
    # The header holds the payload length after the 4-byte size field
    # (matching MMKV's writer); guard against a corrupted or oversized value.
    end = min(4 + declared, len(data)) if declared else len(data)
    pos = 4
    if data[pos:pos + 4] == ITEM_SIZE_HOLDER:
        pos += 4
    entries: list[tuple[str, bytes]] = []
    while pos < end:
        key_len = _read_varint(data, pos, end)
        if key_len is None:
            break
        length, pos = key_len
        if length <= 0 or length > 8192 or pos + length > end:
            break
        key = data[pos:pos + length]
        pos += length
        try:
            key_text = key.decode("utf-8")
        except UnicodeDecodeError:
            break
        if not key_text.isprintable():
            break
        value_len = _read_varint(data, pos, end)
        if value_len is None:
            break
        vlength, pos = value_len
        if vlength < 0 or pos + vlength > end:
            break
        entries.append((key_text, data[pos:pos + vlength]))
        pos += vlength
    return entries


def load(path: os.PathLike[str] | str) -> dict[str, bytes]:
    """Parse an MMKV file into ``{key: value}`` with last-write-wins semantics."""
    result: dict[str, bytes] = {}
    for key, value in parse_entries(path):
        result[key] = value
    return result


def decode_bytes(value: bytes) -> bytes:
    """Strip the inner ``[varint length][bytes]`` prefix the app writes.

    MMKV itself stores a byte array as ``[varint length][data]``; some app
    wrappers additionally keep that prefix inside the value blob. When the
    blob starts with a consistent length prefix it is removed, otherwise the
    blob is returned unchanged.
    """
    decoded = _read_varint(value, 0, len(value))
    if decoded is not None:
        length, pos = decoded
        if pos + length == len(value):
            return value[pos:pos + length]
    return value


def decode_string(value: bytes) -> str:
    """Decode a value as UTF-8 text, ignoring a trailing NUL."""
    return decode_bytes(value).decode("utf-8", errors="replace").rstrip("\x00")
