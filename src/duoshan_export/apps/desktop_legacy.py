"""Classic QQ codec bridge: a local, isolated x86 helper and private copies."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import threading
import time
from uuid import uuid4

from ..errors import ExportError
from ..progress import ProgressEvent, StageReporter
from .qq_legacy_keys import legacy_key_candidates

SQLITE_HEADER = b"SQLite format 3\0"
KERNEL_SHA256 = "46f2cd17c647ed0b2aad1c0c448135fbd4485eebb6d14392e9223257e1aee9c2"


def _check(cancel):
    if cancel and cancel():
        raise ExportError("旧版 QQ 处理已取消；工作副本保留。")


def _hash(path, cancel=None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            _check(cancel)
            digest.update(block)
    return digest.hexdigest()


def component_directory():
    configured = os.environ.get("CHAT_ARCHIVE_LEGACY_QQ_DIR")
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[3] / ".work" / "legacy-qq"


def _bundle(directory=None):
    root = Path(directory or component_directory()).resolve()
    helper, kernel = root / "legacy-qq-helper.exe", root / "KernelUtil.dll"
    if not helper.is_file() or not kernel.is_file():
        raise ExportError("旧版 QQ/TIM 需要本地 32 位组件包；请按 docs/desktop-import.md 配置 KernelUtil.dll 和辅助程序。")
    if _hash(kernel) != KERNEL_SHA256:
        raise ExportError("旧版 QQ/TIM 组件版本尚未验证；当前支持已验证的 9.7.23.29348 组件包。")
    return root, helper


def _helper(command, request, *, directory=None, progress=print, cancel=None):
    root, helper = _bundle(directory)
    _check(cancel)
    if os.name != "nt":
        raise ExportError("旧版 QQ/TIM 本地组件只支持 Windows。")
    events = queue.Queue(maxsize=64)
    stopped = threading.Event()
    environment = dict(os.environ)
    runtime = Path(request["output"]).parent if request and request.get("output") else root
    environment["TEMP"] = environment["TMP"] = str(runtime)
    try:
        proc = subprocess.Popen([str(helper), command, str(root)], cwd=runtime, env=environment,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as error:
        raise ExportError("旧版 QQ/TIM 辅助程序无法启动；请重新构建本地 32 位组件包。") from error

    def put(value):
        while not stopped.is_set():
            try:
                events.put(value, timeout=0.1)
                return
            except queue.Full:
                continue

    def read():
        try:
            while True:
                line = proc.stdout.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    put({"invalid": True})
                    break
                try:
                    value = json.loads(line)
                    put(value if isinstance(value, dict) else {"invalid": True})
                except (ValueError, UnicodeError):
                    put({"invalid": True})
        finally:
            put(None)

    reader = threading.Thread(target=read, name="legacy-qq-helper-events", daemon=True)
    reader.start()
    result = None
    started = time.monotonic()
    try:
        if request:
            proc.stdin.write((json.dumps(request, ensure_ascii=True) + "\n").encode("utf-8"))
            proc.stdin.flush()
        proc.stdin.close()
        with StageReporter(progress, "运行旧版 QQ 隔离组件", cancel=cancel) as stage:
            while True:
                _check(cancel)
                if time.monotonic() - started > 3600:
                    raise ExportError("旧版 QQ 组件运行超过一小时；已中止辅助程序并保留工作副本。")
                try:
                    event = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if event is None:
                    break
                if event.get("invalid"):
                    raise ExportError("旧版 QQ 组件返回了无效响应；没有发布解密结果。")
                if event.get("type") == "result":
                    if result is not None:
                        raise ExportError("旧版 QQ 组件返回了重复结果。")
                    result = event
                elif event.get("type") == "progress":
                    name = event.get("stage")
                    labels = {"copy": "复制旧版 QQ 解密工作文件", "verify_copy": "校验旧版 QQ 工作副本哈希", "verify_key": "验证旧版 QQ 密钥与数据库", "decrypt": "解密旧版 QQ 工作文件"}
                    if name not in labels:
                        continue
                    stage.update(detail=labels[name])
                    done, total = event.get("completed"), event.get("total")
                    if name == "copy" and type(done) is int and type(total) is int and 0 <= done <= total:
                        progress(ProgressEvent(f"{labels[name]}：{done:,}/{total:,} 字节", stage=labels[name], completed=done, total=total, unit="字节"))
                    else:
                        progress(ProgressEvent(labels[name], stage=labels[name]))
            # stdout can close before a malfunctioning helper exits.
            while proc.poll() is None:
                _check(cancel)
                if time.monotonic() - started > 3600:
                    raise ExportError("旧版 QQ 组件没有正常退出；工作副本保留。")
                stopped.wait(0.1)
            if result is None:
                raise ExportError("旧版 QQ 隔离组件异常退出；没有发布解密结果。")
            if proc.returncode and result.get("ok"):
                raise ExportError("旧版 QQ 组件退出码异常；没有发布解密结果。")
        return result
    except (OSError, ValueError) as error:
        _check(cancel)
        raise ExportError("旧版 QQ 组件通信中断；工作副本保留。") from error
    finally:
        stopped.set()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if not proc.stdin.closed:
            proc.stdin.close()
        reader.join(timeout=5)
        proc.stdout.close()


def probe_legacy_components(*, directory=None, progress=print, cancel=None):
    result = _helper("probe", None, directory=directory, progress=progress, cancel=cancel)
    if not result.get("ok") or result.get("protocol") != 1 or result.get("architecture") != "x86":
        raise ExportError("旧版 QQ/TIM 组件无法加载；请检查组件包依赖和 32 位运行库。")
    return result


def _standalone(source, output, *, progress, cancel):
    """Strip a validated QQ header; normalize large shifted-lock-page databases."""
    with source.open("rb") as stream:
        head = stream.read(1040)
    offset = 1024 if head.startswith(b"SQLite header 3\0") and head[1024:1040] == SQLITE_HEADER else 0
    if head[offset:offset + 16] != SQLITE_HEADER:
        raise ExportError("旧版 QQ 组件未生成可读取的 SQLite 数据库。")
    total = source.stat().st_size - offset
    with source.open("rb") as stream, output.open("xb") as target:
        stream.seek(offset)
        with StageReporter(progress, "生成旧版 QQ 标准数据库", completed=0, total=total, unit="字节", cancel=cancel) as stage:
            done = 0
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                _check(cancel)
                target.write(block)
                done += len(block)
                stage.update(completed=done)
    if offset and source.stat().st_size >= 0x40000000:
        from .desktop_crypto import _normalize_qq_database
        normalized = output.with_name(output.name + ".normalized-" + uuid4().hex[:8])
        _normalize_qq_database(output, normalized, progress=progress, cancel=cancel)
        output = normalized
    with StageReporter(progress, "校验旧版 QQ 标准数据库", cancel=cancel):
        try:
            with closing(sqlite3.connect(output.as_uri() + "?mode=ro", uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                db.set_progress_handler(lambda: int(bool(cancel and cancel())), 10000)
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ExportError("旧版 QQ 数据库完整性校验失败；工作副本保留。")
                db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError as error:
            _check(cancel)
            raise ExportError("旧版 QQ 解密结果损坏或格式不受支持；没有发布解密结果。") from error
    return output


def decrypt_legacy_snapshot(source_snapshot, output, *, keys=None, account=None,
                            relative_path=None, progress=print, cancel=None):
    source, output = Path(source_snapshot).resolve(), Path(output).resolve()
    if source == output or output.exists():
        raise ExportError("旧版 QQ 解密输出不能覆盖快照或已有文件。")
    _check(cancel)
    for suffix in ("-wal", "-journal"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.is_file() and sidecar.stat().st_size:
            raise ExportError("旧版 QQ 快照仍含 WAL/journal；当前私有组件不支持安全合并，请在客户端正常关闭后重新复制。")
    with source.open("rb") as stream:
        header = stream.read(1040)
    plain = header.startswith(SQLITE_HEADER) or (header.startswith(b"SQLite header 3\0") and header[1024:1040] == SQLITE_HEADER)
    candidates = legacy_key_candidates(keys, account=account, relative_path=relative_path, source=source)
    if not plain and not candidates:
        raise ExportError("旧版 QQ/TIM 缺少该账号的经典 QQ 16 字节密钥（32 位十六进制）；QQ NT 密钥不能代替。请在 PC 密钥中输入已有旧版密钥。")
    if not plain:
        probe_legacy_components(progress=progress, cancel=cancel)
    output.parent.mkdir(parents=True, exist_ok=True)
    with StageReporter(progress, "校验旧版 QQ 输入快照", cancel=cancel):
        before = _hash(source, cancel)
    work = source
    if not plain:
        for index, key in enumerate(candidates, 1):
            _check(cancel)
            progress(f"验证旧版 QQ 候选密钥 {index}/{len(candidates)}（密钥不写入日志）。")
            work = output.with_name(output.name + ".codec-" + uuid4().hex[:8])
            result = _helper("decrypt", {"source": str(source), "output": str(work), "key": key.hex()}, progress=progress, cancel=cancel)
            if result.get("ok"):
                break
            code = str(result.get("error", ""))
            if code not in {"sql_26", "verify_26", "key_26", "integrity_failed", "verify_11", "sql_11"}:
                raise ExportError("旧版 QQ 组件处理失败（组件、依赖或文件状态不匹配）；工作副本保留。")
        else:
            raise ExportError("旧版 QQ 密钥未通过数据库校验，或快照已经损坏；没有发布解密结果。")
    partial = output.with_name(output.name + ".partial-" + uuid4().hex[:8])
    normalized = _standalone(work, partial, progress=progress, cancel=cancel)
    with StageReporter(progress, "复核旧版 QQ 输入快照未变化", cancel=cancel):
        if _hash(source, cancel) != before:
            raise ExportError("旧版 QQ 输入快照在处理期间变化；没有发布解密结果。")
    _check(cancel)
    normalized.rename(output)
    return output
