"""Verified desktop database decryption and read-only Windows key discovery.

Inputs are private snapshots created by the desktop adapter, never original
application files. No keys or memory dumps are persisted. The SQLCipher page
format is documented upstream by Zetetic; desktop-specific layouts were
cross-checked against fangtimmy/wechat4-db-decrypt and
DaizeDong/qq-history-recovery. This module implements those formats directly.
"""
from __future__ import annotations

from contextlib import closing
import csv
import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import pickle
import re
import sqlite3
import struct
import subprocess
import sys
import threading
from uuid import uuid4

from ..errors import ExportError
from ..progress import StageReporter

SQLITE_HEADER = b"SQLite format 3\0"
PAGE_SIZE = 4096


@dataclass(frozen=True)
class CipherProfile:
    name: str
    reserve: int
    kdf: str
    iterations: int
    mac: str
    offset: int = 0


WECHAT4 = CipherProfile("wechat4", 80, "sha512", 256000, "sha512")
WECHAT3 = CipherProfile("wechat3", 48, "sha1", 64000, "sha1")
QQNT = CipherProfile("qqnt", 48, "sha512", 4000, "sha1", 1024)
QQNT4 = CipherProfile("qqnt4", 80, "sha512", 4000, "sha512", 1024)


def _cancel(cancel):
    if cancel and cancel():
        raise ExportError("PC 数据库处理已取消；已复制的工作文件保留。")


def _mac_key(key, salt, profile):
    return hashlib.pbkdf2_hmac(profile.kdf, key, bytes(v ^ 0x3A for v in salt), 2, 32)


def _page_valid(page, number, mac_key, profile):
    if len(page) != PAGE_SIZE:
        return False
    start = 16 if number == 1 else 0
    stop = PAGE_SIZE - profile.reserve + 16
    digest = hmac.new(mac_key, page[start:stop] + struct.pack("<I", number), profile.mac).digest()
    return hmac.compare_digest(digest, page[stop:stop + len(digest)])


def validate_raw_key(key, page, profile=WECHAT4):
    return len(key) == 32 and _page_valid(page, 1, _mac_key(key, page[:16], profile), profile)


def _candidate_entries(keys, account, relative_path, source, salt):
    """Select scoped values, never assume an unrelated account's key matches."""
    if keys is None:
        return []
    if isinstance(keys, (str, bytes)):
        return [keys]
    if isinstance(keys, (list, tuple)):
        return list(keys)
    if not isinstance(keys, dict):
        raise ExportError("PC 密钥必须是字符串或按账号/数据库路径索引的 JSON 对象。")
    if any(k in keys for k in ("enc_key", "raw_key", "key", "passphrase")):
        return [keys]
    normalized = {str(k).replace("\\", "/"): v for k, v in keys.items()}
    selectors = [str(source).replace("\\", "/"), relative_path, salt.hex(), account, "default", "*"]
    result = []
    for selector in selectors:
        if selector and str(selector).replace("\\", "/") in normalized:
            value = normalized[str(selector).replace("\\", "/")]
            if isinstance(value, dict) and not any(k in value for k in ("enc_key", "raw_key", "key", "passphrase")):
                result.extend(_candidate_entries(value, None, relative_path, source, salt))
            else:
                result.extend(value if isinstance(value, list) else [value])
    return result


def _key_material(entry):
    """Yield (bytes, raw-or-passphrase), including explicit hex passphrases."""
    if isinstance(entry, dict):
        if entry.get("passphrase") is not None:
            value = entry["passphrase"]
            if entry.get("encoding") == "hex":
                try:
                    yield bytes.fromhex(str(value)), False
                except ValueError:
                    return
            else:
                yield str(value).encode("utf-8"), False
            return
        value = entry.get("enc_key", entry.get("raw_key", entry.get("key")))
        if value is None:
            return
        yield from _key_material(value)
        return
    if isinstance(entry, bytes):
        if len(entry) == 32:
            yield entry, True
        yield entry, False
        return
    value = str(entry).strip()
    match = re.fullmatch(r"(?:[xX]')?([0-9a-fA-F]{64}|[0-9a-fA-F]{96})'?", value)
    if match:
        raw = bytes.fromhex(match[1])
        yield raw[:32], True
        if len(raw) == 32:
            yield raw, False
    if value:
        yield value.encode("utf-8"), False


def _resolve_key(entries, page, profiles):
    for profile in profiles:
        for entry in entries:
            for material, raw in _key_material(entry):
                key = material if raw else hashlib.pbkdf2_hmac(profile.kdf, material, page[:16], profile.iterations, 32)
                if validate_raw_key(key, page, profile):
                    return key, profile
    return None, None


def _decode_page(page, number, key, mac_key, profile):
    # QQ's wrapper moves SQLite's fixed 1 GiB locking byte into this page.
    # It is the only interior all-zero page that is not ciphertext.
    if profile.offset and len(page) == PAGE_SIZE and number == (0x40000000 - profile.offset) // PAGE_SIZE + 1 and not any(page):
        return bytes(PAGE_SIZE)
    if not _page_valid(page, number, mac_key, profile):
        raise ExportError(f"数据库第 {number:,} 页完整性校验失败；没有发布损坏的解密结果。")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as error:
        raise ExportError("PC 数据库解密需要 cryptography：请安装本工具的 xhs/full 依赖。") from error
    start = 16 if number == 1 else 0
    end = PAGE_SIZE - profile.reserve
    decoder = Cipher(algorithms.AES(key), modes.CBC(page[end:end + 16])).decryptor()
    plain = decoder.update(page[start:end]) + decoder.finalize()
    if number == 1:
        plain = SQLITE_HEADER + plain
        # SQLite may reserve more bytes than the codec needs: SQLCipher 4.12
        # keeps the pager's original reserve when its HMAC mode is changed
        # after PRAGMA key. Preserve that legal larger reserve in the header.
        if plain[16:18] != struct.pack(">H", PAGE_SIZE) or plain[20] < profile.reserve:
            raise ExportError("解密后的 SQLite 页结构与已验证加密格式不一致。")
        # Publish a standalone snapshot, not a database requiring a stale WAL.
        plain = plain[:18] + b"\1\1" + plain[20:]
    return plain + bytes(profile.reserve)


def _wal_checksum(data, endian, initial=(0, 0)):
    if len(data) % 8:
        raise ExportError("WAL 校验区域长度无效。")
    s0, s1 = initial
    for a, b in struct.iter_unpack(endian + "II", data):
        s0 = (s0 + a + s1) & 0xFFFFFFFF
        s1 = (s1 + b + s0) & 0xFFFFFFFF
    return s0, s1


def _committed_wal(wal, cancel):
    """Validate SQLite's rolling checksum; retain only the last commit prefix."""
    with wal.open("rb") as f:
        header = f.read(32)
        if len(header) < 32:
            return {}, None
        magic, version, size = struct.unpack(">III", header[:12])
        if magic not in (0x377F0682, 0x377F0683) or size != PAGE_SIZE or version != 3007000:
            raise ExportError("数据库 WAL 格式尚不支持；请关闭客户端后重新复制。")
        endian = "<" if magic == 0x377F0682 else ">"
        checksum = _wal_checksum(header[:24], endian)
        if checksum != struct.unpack(">II", header[24:32]):
            raise ExportError("数据库 WAL 文件头校验失败；请重新复制快照。")
        salt = header[16:24]
        pending, committed, count = {}, {}, None
        while True:
            _cancel(cancel)
            offset = f.tell()
            frame = f.read(24)
            page = f.read(PAGE_SIZE)
            if len(frame) != 24 or len(page) != PAGE_SIZE:
                break
            number, db_size = struct.unpack(">II", frame[:8])
            if not number or frame[8:16] != salt:
                break
            expected = _wal_checksum(frame[:8] + page, endian, checksum)
            if expected != struct.unpack(">II", frame[16:24]):
                break
            checksum = expected
            pending[number] = offset + 24
            if db_size:
                committed.update(pending)
                pending.clear()
                count = db_size
        return committed, count


def _verify_database(path, cancel=None):
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            if cancel:
                db.set_progress_handler(lambda: int(bool(cancel())), 10000)
            result = db.execute("PRAGMA quick_check").fetchall()
            if result != [("ok",)]:
                raise ExportError("解密数据库 quick_check 未通过，工作文件已保留。")
            db.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as error:
        _cancel(cancel)
        raise ExportError("解密结果不是可完整读取的 SQLite 数据库；工作文件已保留。") from error


def decrypt_snapshot(source_snapshot, output, *, keys=None, account=None,
                     relative_path=None, app=None, progress=print, cancel=None,
                     sqlcipher_module_path=None):
    """Decrypt a copied snapshot. Strict page MACs and committed WAL are checked."""
    source = Path(source_snapshot).resolve()
    output = Path(output).resolve()
    if output == source:
        raise ExportError("解密输出不能覆盖数据库快照。")
    _cancel(cancel)
    with source.open("rb") as f:
        header = f.read(1024)
    if header.startswith(SQLITE_HEADER):
        # SQLite applies committed WAL itself; both connections reference private
        # files. The source backup() reads no original application database.
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise ExportError("解密输出已存在，请使用新的工作目录。")
        partial = output.with_name(output.name + ".partial-" + uuid4().hex[:8])
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(partial)) as target, StageReporter(progress, "合并 PC 明文数据库快照", completed=0, unit="页", cancel=cancel) as stage:
                def backup_progress(status, remaining, total):
                    _cancel(cancel)
                    stage.update(completed=total - remaining, total=total)
                db.backup(target, pages=256, progress=backup_progress)
        with StageReporter(progress, "校验解密数据库", cancel=cancel):
            _cancel(cancel)
            _verify_database(partial, cancel)
        partial.rename(output)
        return output
    is_nt = b"QQ_NT DB" in header[:128]
    if header.startswith(b"SQLite header") and not is_nt:
        raise ExportError("识别到旧版 QQ/TIM 私有加密数据库（Msg3.0）；它不能用 QQ NT 密钥解密。请提供受支持的解密库或导出记录。")
    profiles = (QQNT4, QQNT) if is_nt else (WECHAT4, WECHAT3)
    offset = profiles[0].offset
    with source.open("rb") as f:
        f.seek(offset)
        page = f.read(PAGE_SIZE)
    entries = _candidate_entries(keys, account, relative_path, source, page[:16])
    if not entries:
        raise ExportError("数据库已加密，缺少此账号/数据库的有效密钥。请登录对应客户端后读取密钥，或选择 PC 密钥 JSON 文件。")
    key, profile = _resolve_key(entries, page, profiles)
    if key is None:
        raise ExportError("提供的 PC 数据库密钥未通过页校验；请确认账号、数据库版本和密钥对应。")
    size = source.stat().st_size - offset
    if size <= 0 or size % PAGE_SIZE:
        raise ExportError("加密数据库页长度不完整；请重新复制数据库快照。")
    total = size // PAGE_SIZE
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ExportError("解密输出已存在，请使用新的工作目录。")
    partial = output.with_name(output.name + ".partial-" + uuid4().hex[:8])
    mac_key = _mac_key(key, page[:16], profile)
    with source.open("rb") as inp, partial.open("xb+") as out:
        inp.seek(offset)
        logical_pages = None
        with StageReporter(progress, "解密 PC 数据库", completed=0, total=total, unit="页", cancel=cancel) as stage:
            for number in range(1, total + 1):
                _cancel(cancel)
                encoded = inp.read(PAGE_SIZE)
                if logical_pages and number > logical_pages and not any(encoded):
                    # QQ preallocates zero-filled trailing capacity. It is not
                    # a database page. The bound comes from authenticated page 1
                    # and SQLite's change-counter / version-valid-for contract.
                    stage.update(completed=number, detail=source.name)
                    continue
                decoded = _decode_page(encoded, number, key, mac_key, profile)
                if number == 1 and decoded[24:28] == decoded[92:96]:
                    declared = struct.unpack_from(">I", decoded, 28)[0]
                    if 0 < declared <= total:
                        logical_pages = declared
                if logical_pages is None or number <= logical_pages:
                    out.write(decoded)
                stage.update(completed=number, detail=source.name)
        wal = source.with_name(source.name + "-wal")
        if wal.is_file() and wal.stat().st_size:
            frames, db_size = _committed_wal(wal, cancel)
            with wal.open("rb") as stream, StageReporter(progress, "合并 PC 数据库已提交 WAL", completed=0, total=len(frames), unit="页", cancel=cancel) as stage:
                for index, (number, position) in enumerate(frames.items(), 1):
                    _cancel(cancel)
                    if number <= db_size:
                        stream.seek(position)
                        out.seek((number - 1) * PAGE_SIZE)
                        out.write(_decode_page(stream.read(PAGE_SIZE), number, key, mac_key, profile))
                    stage.update(completed=index)
                if db_size:
                    out.truncate(db_size * PAGE_SIZE)
        out.flush()
        os.fsync(out.fileno())
    if profile.offset and partial.stat().st_size >= 0x40000000 - profile.offset:
        # QQ reserves a different 1 GiB lock page because of its wrapper.
        # Rebuild rows/indexes through an isolated reader with QQ's lock offset;
        # the importing process always retains standard SQLite global settings.
        normalized = output.with_name(output.name + ".normalized-" + uuid4().hex[:8])
        _normalize_qq_database(partial, normalized, progress=progress, cancel=cancel)
        partial = normalized
    with StageReporter(progress, "校验解密数据库", cancel=cancel):
        _cancel(cancel)
        _verify_database(partial, cancel)
    partial.rename(output)
    return output


def _emit_qq_dump(source):
    """Private subprocess: stream typed SQLite records, never textual chat logs."""
    import _sqlite3
    import ctypes.util
    library = Path(sys.base_prefix) / "DLLs" / "sqlite3.dll" if os.name == "nt" else ctypes.util.find_library("sqlite3")
    dll = ctypes.CDLL(str(library or _sqlite3.__file__))
    control = dll.sqlite3_test_control
    control.restype = ctypes.c_int
    control(11, ctypes.c_uint(0x40000000 - 1024))  # SQLITE_TESTCTRL_PENDING_BYTE
    output = sys.stdout.buffer
    def send(value):
        payload = pickle.dumps(value, protocol=4)
        output.write(struct.pack("<Q", len(payload)))
        output.write(payload)
        output.flush()
    with closing(sqlite3.connect(Path(source).as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ExportError("QQ 数据库原始锁页布局校验失败。")
        objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
        tables = [(name, sql) for kind, name, sql in objects if kind == "table" and not name.startswith("sqlite_")]
        quote = lambda value: '"' + value.replace('"', '""') + '"'
        total = sum(db.execute("SELECT count(*) FROM " + quote(name)).fetchone()[0] for name, _ in tables)
        send(("start", total))
        for name, sql in tables:
            columns = [row[1] for row in db.execute("PRAGMA table_info(" + quote(name) + ")")]
            if "WITHOUT ROWID" not in sql.upper() and not {c.lower() for c in columns}.intersection({"rowid", "oid", "_rowid_"}):
                columns = ["rowid"] + columns
            send(("table", name, sql, columns))
            cursor = db.execute("SELECT " + ",".join(quote(c) for c in columns) + " FROM " + quote(name))
            while rows := cursor.fetchmany(128):
                send(("rows", name, rows))
        if db.execute("SELECT count(*) FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()[0]:
            send(("sequence", db.execute("SELECT name,seq FROM sqlite_sequence").fetchall()))
        for kind, name, sql in objects:
            if kind in ("index", "view", "trigger") and not name.startswith("sqlite_"):
                send(("schema", sql))
        send(("metadata", db.execute("PRAGMA user_version").fetchone()[0], db.execute("PRAGMA application_id").fetchone()[0]))
        send(("done",))


class _DataUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise ExportError("QQ 数据库记录流含不支持的数据类型。")


def _normalize_qq_database(source, output, *, progress, cancel):
    """Convert QQ's shifted lock-page layout into an ordinary standalone DB."""
    import io
    error_log = output.with_name(output.name + ".log")
    script = "import sys;sys.path.insert(0,sys.argv.pop(1));from duoshan_export.apps.desktop_crypto import _emit_qq_dump;_emit_qq_dump(sys.argv[1])"
    executable = Path(sys.executable)
    if executable.stem.lower() == "pythonw" and executable.with_name("python.exe").is_file():
        executable = executable.with_name("python.exe")
    with error_log.open("wb") as errors, closing(sqlite3.connect(output)) as target:
        target.execute("PRAGMA journal_mode=OFF")
        target.execute("PRAGMA synchronous=OFF")
        if cancel:
            target.set_progress_handler(lambda: int(bool(cancel())), 10000)
        proc = subprocess.Popen([str(executable), "-c", script, str(Path(__file__).resolve().parents[2]), str(source)],
                                stdout=subprocess.PIPE, stderr=errors,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        stopped = threading.Event()
        def watch_cancel():
            while not stopped.wait(0.1):
                if cancel and cancel():
                    if proc.poll() is None:
                        try:
                            proc.terminate()
                        except ProcessLookupError:
                            pass
                    return
        watcher = threading.Thread(target=watch_cancel, daemon=True, name="qq-normalizer-cancel")
        watcher.start()
        def read_exact(length):
            result = bytearray()
            while len(result) < length:
                _cancel(cancel)
                block = proc.stdout.read(min(length - len(result), 1024 * 1024))
                if not block:
                    _cancel(cancel)
                    raise ExportError("QQ 数据库格式转换中断，工作文件与诊断日志已保留。")
                result.extend(block)
            return result
        try:
            completed = 0
            columns = {}
            with StageReporter(progress, "转换 QQ 标准数据库", completed=0, unit="条", cancel=cancel) as stage:
                while True:
                    _cancel(cancel)
                    length = struct.unpack("<Q", read_exact(8))[0]
                    if not 0 < length <= 512 * 1024 * 1024:
                        raise ExportError("QQ 数据库记录流长度无效。")
                    event = _DataUnpickler(io.BytesIO(read_exact(length))).load()
                    kind = event[0]
                    if kind == "start":
                        stage.update(total=event[1])
                    elif kind == "table":
                        target.execute(event[2])
                        columns[event[1]] = event[3]
                        stage.update(detail=event[1])
                    elif kind == "rows":
                        name, rows = event[1:]
                        table = '"' + name.replace('"', '""') + '"'
                        fields = ",".join('"' + c.replace('"', '""') + '"' for c in columns[name])
                        target.executemany("INSERT INTO " + table + "(" + fields + ") VALUES(" + ",".join("?" for _ in rows[0]) + ")", rows)
                        completed += len(rows)
                        stage.update(completed=completed)
                    elif kind == "sequence":
                        target.execute("DELETE FROM sqlite_sequence")
                        target.executemany("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", event[1])
                    elif kind == "schema":
                        stage.update(detail="重建索引、视图及触发器")
                        target.execute(event[1])
                    elif kind == "metadata":
                        target.execute("PRAGMA user_version=" + str(int(event[1])))
                        target.execute("PRAGMA application_id=" + str(int(event[2])))
                    elif kind == "done":
                        break
                    else:
                        raise ExportError("QQ 数据库记录流类型无效。")
                stage.update(detail="提交标准数据库")
                _cancel(cancel)
                target.commit()
                if proc.wait() != 0:
                    raise ExportError("QQ 数据库格式转换失败，诊断日志已保留。")
        except sqlite3.DatabaseError as error:
            _cancel(cancel)
            raise ExportError("QQ 数据库标准化写入失败，工作文件与诊断日志已保留。") from error
        finally:
            stopped.set()
            watcher.join()
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
            proc.stdout.close()


def _windows_memory():
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ExportError("自动读取 PC 聊天数据库密钥需要 64 位 Windows Python。")
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
    dll.OpenProcess.restype = wt.HANDLE
    dll.CloseHandle.argtypes = (wt.HANDLE,)
    dll.VirtualQueryEx.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
    dll.VirtualQueryEx.restype = ctypes.c_size_t
    dll.ReadProcessMemory.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
    dll.ReadProcessMemory.restype = wt.BOOL
    return dll


class _MemoryInfo(ctypes.Structure):
    _fields_ = [("base", ctypes.c_ulonglong), ("allocation", ctypes.c_ulonglong),
                ("allocation_protect", wt.DWORD), ("pad", wt.DWORD),
                ("size", ctypes.c_ulonglong), ("state", wt.DWORD),
                ("protect", wt.DWORD), ("kind", wt.DWORD), ("pad2", wt.DWORD)]


def _read_process(dll, handle, address, length):
    if not 0 < length <= 2 * 1024 * 1024 or not 0x10000 <= address < 0x800000000000:
        return b""
    buf = ctypes.create_string_buffer(length)
    used = ctypes.c_size_t()
    dll.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, length, ctypes.byref(used))
    return buf.raw[:used.value]


def _regions(dll, handle):
    address = 0
    info = _MemoryInfo()
    found = []
    while address < 0x800000000000:
        if not dll.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)):
            break
        if info.state == 0x1000 and info.size and not (info.protect & 0x101):
            found.append((info.base, info.size))
        next_address = info.base + info.size
        if next_address <= address:
            break
        address = next_address
    return found


def _chunks(dll, handle, regions, cancel):
    for address, size in regions:
        tail = b""
        for offset in range(0, size, 2 * 1024 * 1024):
            _cancel(cancel)
            data = _read_process(dll, handle, address + offset, min(size - offset, 2 * 1024 * 1024))
            yield address + offset - len(tail), tail + data, min(size - offset, 2 * 1024 * 1024)
            tail = data[-256:]


def recover_wechat_keys(snapshot_root, *, progress=print, cancel=None):
    """Read-only memory scan. Return only HMAC-verified per-salt raw keys.

    snapshot_root is a private copied db_storage/Msg tree. A process holding
    another account's keys simply yields no matches. No client is launched,
    suspended, modified or injected; only QUERY_INFORMATION and VM_READ used.
    """
    root = Path(snapshot_root).resolve()
    inventory = {}
    for path in root.rglob("*.db"):
        _cancel(cancel)
        with path.open("rb") as f:
            page = f.read(PAGE_SIZE)
        if len(page) == PAGE_SIZE and not page.startswith((SQLITE_HEADER, b"SQLite header")):
            inventory[page[:16].hex()] = page
    if not inventory:
        return {}
    dll = _windows_memory()
    proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                          capture_output=True, text=True, encoding="mbcs", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    pids = []
    for row in csv.reader(proc.stdout.splitlines()):
        if len(row) >= 5 and row[0].lower() == "weixin.exe" and row[1].isdigit():
            weight = int("".join(c for c in row[4] if c.isdigit()) or "0")
            pids.append((weight, int(row[1])))
    if not pids:
        progress("没有正在运行的 PC 微信进程；可登录对应账号后重试读取密钥。")
        return {}
    result, seen = {}, set()
    pattern = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
    needle = b"com.Tencent.WCDB.Config.Cipher"
    mask = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")

    def candidates(data):
        for match in pattern.finditer(data):
            run = match[1]
            for start in sorted({0, max(0, len(run) - 64), *range(0, max(1, len(run) - 63), 32)}):
                key = bytes.fromhex(run[start:start + 64].decode())
                if len(key) != 32 or key in seen:
                    continue
                seen.add(key)
                for salt, page in inventory.items():
                    if salt not in result and validate_raw_key(key, page):
                        result[salt] = {"enc_key": key.hex()}

    for _, pid in sorted(pids, reverse=True):
        _cancel(cancel)
        handle = dll.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            progress(f"无法只读访问微信进程 {pid}；可使用密钥文件或以相同权限运行工具。")
            continue
        try:
            regions = _regions(dll, handle)
            anchors = set()
            progress(f"只读检查微信进程 {pid}，查找并校验本次快照密钥。")
            with StageReporter(progress, "读取 PC 微信密钥", completed=0, total=sum(n for _, n in regions), unit="字节", cancel=cancel) as stage:
                done = 0
                for base, data, scanned in _chunks(dll, handle, regions, cancel):
                    candidates(data)
                    offset = data.find(needle)
                    while offset >= 0:
                        anchors.add(base + offset)
                        offset = data.find(needle, offset + 1)
                    done += scanned
                    stage.update(completed=done)
            if anchors and len(result) < len(inventory):
                pointers = [struct.pack("<QQ", a, len(needle)) for a in anchors]
                progress(f"检查 WCDB 配置对象；已验证 {len(result)}/{len(inventory)} 个数据库密钥。")
                blobs = set()
                with StageReporter(progress, "校验 PC 微信配置密钥", completed=0, total=sum(n for _, n in regions), unit="字节", cancel=cancel) as stage:
                    done = 0
                    for base, data, scanned in _chunks(dll, handle, regions, cancel):
                        for pointer in pointers:
                            pos = data.find(pointer)
                            while pos >= 0:
                                node = _read_process(dll, handle, base + pos - 0x10, 0x50)
                                if len(node) >= 0x30:
                                    config = struct.unpack_from("<Q", node, 0x28)[0]
                                    obj = _read_process(dll, handle, config + 0x88, 0x28)
                                    if len(obj) >= 0x18:
                                        address, length = struct.unpack_from("<QQ", obj, 8)
                                        if 0 < length <= 1024 and (address, length) not in blobs:
                                            blobs.add((address, length))
                                            blob = _read_process(dll, handle, address, length)
                                            candidates(blob)
                                            candidates(bytes(v ^ mask[i % len(mask)] for i, v in enumerate(blob)))
                                pos = data.find(pointer, pos + 1)
                        done += scanned
                        stage.update(completed=done)
        finally:
            dll.CloseHandle(handle)
        if len(result) == len(inventory):
            break
    progress(f"PC 微信密钥检查完成：通过页完整性验证 {len(result)}/{len(inventory)} 个；密钥仅在本次任务内存中使用。")
    return result


def _qq_context_keys(data, base, read, inventory):
    """Find SQLCipher contexts by their documented numeric shape and DB salt.

    Only following structural pointers avoids testing arbitrary strings from
    process memory. SQLCipher releases add fields, so locate the salt pointer
    in a bounded neighborhood rather than assuming one client-version offset.
    Every candidate still requires an independent database page HMAC.
    """
    marker = struct.pack("<7I", 4000, 2, 16, 32, 16, 16, PAGE_SIZE)
    position = data.find(marker)
    while position >= 0:
        context = read(base + position, 160)
        if len(context) >= 144:
            for salt_offset in range(44, 85, 8):
                salt_pointer = struct.unpack_from("<Q", context, salt_offset)[0]
                salt = read(salt_pointer, 16)
                page = inventory.get(salt)
                if page is None:
                    continue
                for ctx_offset in (salt_offset + 32, salt_offset + 40):
                    pointer = struct.unpack_from("<Q", context, ctx_offset)[0]
                    cipher_context = read(pointer, 40)
                    if len(cipher_context) < 32:
                        continue
                    key_pointer = struct.unpack_from("<Q", cipher_context, 8)[0]
                    key = read(key_pointer, 32)
                    for profile in (QQNT4, QQNT):
                        if validate_raw_key(key, page, profile):
                            yield salt.hex(), {"enc_key": key.hex()}
                            break
        position = data.find(marker, position + 1)


def recover_qq_keys(snapshot_root, *, progress=print, cancel=None):
    """Recover HMAC-verified QQ NT keys from running QQ memory, read-only.

    Requires copied database snapshots. Does not attach a debugger, inject,
    alter process memory, log key values, or persist memory/key material.
    """
    inventory = {}
    for path in Path(snapshot_root).resolve().rglob("*.db"):
        _cancel(cancel)
        with path.open("rb") as stream:
            header = stream.read(1024)
            if b"QQ_NT DB" not in header[:128]:
                continue
            page = stream.read(PAGE_SIZE)
        if len(page) == PAGE_SIZE:
            inventory[page[:16]] = page
    if not inventory:
        return {}
    dll = _windows_memory()
    proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/FO", "CSV", "/NH"],
                          capture_output=True, text=True, encoding="mbcs", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    pids = []
    for row in csv.reader(proc.stdout.splitlines()):
        if len(row) >= 5 and row[0].lower() == "qq.exe" and row[1].isdigit():
            pids.append((int("".join(c for c in row[4] if c.isdigit()) or "0"), int(row[1])))
    if not pids:
        progress("没有正在运行的 QQ 进程；请登录对应账号后重试读取密钥。")
        return {}
    result = {}
    for _, pid in sorted(pids, reverse=True):
        _cancel(cancel)
        handle = dll.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            progress(f"无法只读访问 QQ 进程 {pid}；可使用密钥文件或以相同权限运行工具。")
            continue
        try:
            regions = _regions(dll, handle)
            progress(f"只读检查 QQ 进程 {pid}，按数据库盐值定位并校验 SQLCipher 密钥。")
            with StageReporter(progress, "读取 QQ NT 密钥", completed=0,
                               total=sum(n for _, n in regions), unit="字节", cancel=cancel) as stage:
                done = 0
                read = lambda address, length: _read_process(dll, handle, address, length)
                for base, data, scanned in _chunks(dll, handle, regions, cancel):
                    for salt, entry in _qq_context_keys(data, base, read, inventory):
                        result[salt] = entry
                    done += scanned
                    stage.update(completed=done, detail=f"已验证 {len(result)}/{len(inventory)} 个数据库")
                    if len(result) == len(inventory):
                        break
        finally:
            dll.CloseHandle(handle)
        if len(result) == len(inventory):
            break
    progress(f"QQ NT 密钥检查完成：通过页完整性验证 {len(result)}/{len(inventory)} 个；密钥仅在本次任务内存中使用。")
    return result
