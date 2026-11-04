"""Pluggable per-app export rules.

The package ships with rules for two apps:

``duoshan`` (``my.maya.android``)
    The original, fully verified pipeline in :mod:`..source`,
    :mod:`..database`, :mod:`..exporter` and friends. Its behaviour is the
    reference and is never modified by other app rules.

``xhs`` (``com.xingin.xhs``, Xiaohongshu/RED)
    A self-contained adapter in :mod:`.xhs` and :mod:`.xhs_export` that
    reuses the archive extraction, the shared reader helpers and the output
    naming contract, and adds its own decryption (WCDB fork) and schema
    mapping. See ``docs/xhs-export.md``.

:func:`resolve_app` decides which rules apply to a prepared source. With the
default ``auto`` policy the Duoshan pipeline is tried first and is only
bypassed when it cannot find any Duoshan account *and* Xiaohongshu app data
is present, so Duoshan behaviour is bit-for-bit unchanged for Duoshan
backups.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ExportError
from . import xhs

APP_AUTO = "auto"
APP_DUOSHAN = "duoshan"
APP_XHS = "xhs"
SUPPORTED_APPS = (APP_AUTO, APP_DUOSHAN, APP_XHS)


def resolve_app(app: str, layout) -> str:
    """Return the app rules to use for a prepared source layout.

    ``app`` is one of ``auto``, ``duoshan`` or ``xhs``. Under ``auto`` the
    Duoshan account discovery is attempted first; only when it finds nothing
    and Xiaohongshu data is detected does the XHS adapter take over. The
    original Duoshan error is re-raised when neither app matches, so error
    messages stay identical to the Duoshan-only behaviour.
    """
    app = str(app or APP_AUTO).strip().lower()
    if app == APP_DUOSHAN:
        return APP_DUOSHAN
    if app == APP_XHS:
        return APP_XHS
    if app != APP_AUTO:
        raise ExportError(
            f"Unsupported --app value: {app!r}. Supported: {', '.join(SUPPORTED_APPS)}.")

    from .. import source as duoshan_source

    try:
        accounts = duoshan_source.discover_accounts(layout)
    except ExportError as duoshan_error:
        if xhs.discover(layout) is not None:
            return APP_XHS
        raise duoshan_error
    if accounts:
        return APP_DUOSHAN
    if xhs.discover(layout) is not None:
        return APP_XHS
    return APP_DUOSHAN


def xhs_app_dir(layout) -> Path | None:
    """Best-effort Xiaohongshu app directory of a layout (or ``None``)."""
    source = xhs.discover(layout)
    return source.app_dir if source is not None else None
