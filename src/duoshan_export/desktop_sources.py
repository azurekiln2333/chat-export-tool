"""Read-only discovery of Windows chat accounts, independent of Android adapters."""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import re
import uuid

LABELS = {"wechat_pc": "微信（PC）", "qq_pc": "QQ（PC）"}
SQLITE_HEADER = b"SQLite format 3\x00"


def documents_directory() -> Path:
    """Resolve redirected Documents through Windows, with portable fallbacks."""
    if os.name == "nt":
        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                        ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]
        identifier = GUID.from_buffer_copy(uuid.UUID("fdd39ad0-238f-46af-adb4-6c85480369c7").bytes_le)
        address = ctypes.c_void_p()
        shell = ctypes.WinDLL("shell32")
        ole = ctypes.WinDLL("ole32")
        shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        try:
            if shell.SHGetKnownFolderPath(ctypes.byref(identifier), 0, None, ctypes.byref(address)) == 0 and address.value:
                return Path(ctypes.wstring_at(address)).expanduser().resolve()
        finally:
            if address.value:
                ole.CoTaskMemFree(address)
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "Personal")
            return Path(os.path.expandvars(value)).expanduser().resolve()
        except (OSError, ValueError):
            pass
    return (Path.home() / "Documents").resolve()


def _format(root: Path) -> str | None:
    if (root / "db_storage/message").is_dir():
        return "wechat4"
    if (root / "Msg").is_dir() and ((root / "Msg/MicroMsg.db").is_file() or (root / "Msg/Multi").is_dir()):
        return "wechat3"
    if (root / "nt_qq/nt_db/nt_msg.db").is_file() or (root / "nt_db/nt_msg.db").is_file():
        return "qqnt"
    if (root / "Msg3.0.db").is_file():
        return "qq_legacy"
    return None


def _root_for(path: Path) -> Path | None:
    """Accept an account, known nested DB directory or file, never its siblings."""
    start = path.parent if path.is_file() else path
    for parent in (start, *start.parents):
        if _format(parent):
            # nt_qq is a structural folder, not the account identifier. A file
            # picker may start inside it; preserve the outer owning account.
            if parent.name.lower() == "nt_qq" and _format(parent.parent) == "qqnt":
                return parent.parent
            return parent
        if parent.name.lower() in {"tencent files", "wechat files", "xwechat_files"}:
            break
    return None


def canonical_account(root: Path, format_name: str) -> str:
    name = root.name
    # WeChat 4 appends a four-hex instance suffix to wxid directories.
    if format_name == "wechat4" and name.startswith("wxid_"):
        name = re.sub(r"_[0-9a-fA-F]{4}$", "", name)
    return name


def database_files(source) -> list[Path]:
    """Only application DB locations; received .db attachments are never parsed."""
    root = Path(source["source"] if isinstance(source, dict) else source).expanduser().resolve()
    root = _root_for(root) or root
    fmt = _format(root)
    candidates = []
    if fmt == "wechat4":
        for folder in ("db_storage/message", "db_storage/contact", "db_storage/session"):
            directory = root / folder
            if directory.is_dir():
                candidates.extend(p for p in directory.glob("*.db") if not any(k in p.name.lower() for k in ("fts", "media_", "resource")))
    elif fmt == "wechat3":
        for folder in ("Msg", "Msg/Multi"):
            candidates.extend(p for p in (root / folder).glob("*.db") if re.fullmatch(r"(?:MSG\d*|MicroMsg|Contact)\.db", p.name, re.I))
    elif fmt == "qqnt":
        folder = root / "nt_qq/nt_db" if (root / "nt_qq/nt_db").is_dir() else root / "nt_db"
        candidates.extend(folder / n for n in ("nt_msg.db", "profile_info.db", "group_info.db") if (folder / n).is_file())
        candidates.extend(root / n for n in ("Msg3.0.db", "Misc.db", "Info.db") if (root / n).is_file())
    elif fmt == "qq_legacy":
        candidates.extend(root / n for n in ("Msg3.0.db", "Misc.db", "Info.db") if (root / n).is_file())
    found = []
    for path in sorted(set(candidates)):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        found.append(path)
        for suffix in ("-wal", "-shm", "-journal"):
            side = path.with_name(path.name + suffix)
            if side.is_file() and not side.is_symlink() and side.resolve().is_relative_to(root):
                found.append(side)
    return found


desktop_database_files = database_files


def _describe(root: Path) -> dict | None:
    fmt = _format(root)
    if not fmt:
        return None
    app = "wechat_pc" if fmt.startswith("wechat") else "qq_pc"
    account = canonical_account(root, fmt)
    uid = hashlib.sha256((app + "\0" + account).encode("utf-8")).hexdigest()[:32]
    files = [p for p in database_files(root) if p.suffix == ".db"]
    encrypted = False
    readable = 0
    for p in files:
        try:
            with p.open("rb") as stream:
                plain = stream.read(16) == SQLITE_HEADER
            readable += plain
            encrypted |= not plain
        except OSError:
            encrypted = True
    status = "待检查数据库"
    if encrypted:
        status = "检测到加密库，导入时检查密钥"
    if fmt == "qq_legacy":
        status = "旧版 QQ：加密格式需单独验证"
    legacy_database_present = fmt == "qqnt" and (root / "Msg3.0.db").is_file()
    if legacy_database_present:
        status += "；含旧版 QQ 记录，将分别检查并导入"
    return {"app": app, "package": app, "source": str(root), "kind": "desktop", "account": account,
            "account_uid": uid, "uid": uid, "label": LABELS[app] + " · " + account,
            "format": fmt, "status": status, "database_count": len(files), "plaintext_databases": readable,
            "encrypted": encrypted, "legacy_database_present": legacy_database_present}


def discover_desktop_sources(paths=None) -> list[dict]:
    """No extraction, login, process access or writes are performed here."""
    if paths is None:
        docs = documents_directory()
        paths = [docs / name for name in ("Tencent Files", "xwechat_files", "WeChat Files")]
    elif isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    found = {}
    def add(root):
        if root.is_symlink():
            return
        item = _describe(root)
        if item:
            found[os.path.normcase(str(root))] = item
    for value in paths:
        path = Path(value).expanduser().resolve()
        if not path.exists():
            continue
        account_root = _root_for(path)
        if account_root:
            add(account_root)
            continue
        if not path.is_dir():
            continue
        try:
            children = sorted(path.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.is_symlink() or child.name.lower() in {"all users", "all_users", "backup", "nt_qq", "cache", "files", "msg"}:
                continue
            if _format(child):
                add(child)
            elif child.name.lower() in {"tencent files", "wechat files", "xwechat_files"}:
                for nested in child.iterdir():
                    if nested.is_dir() and nested.name.lower() not in {"all users", "all_users", "backup", "nt_qq"}:
                        add(nested)
    return sorted(found.values(), key=lambda r: (r["app"], r["account"], r["source"]))
