"""Decrypt WCDB (SQLCipher-fork) databases used by some Android apps.

Some apps encrypt their databases with Tencent's WCDB library configured via
``SQLiteCipherSpec`` values that differ from stock SQLCipher defaults. Such
files cannot be opened by the ``sqlcipher3`` wheel (a different SQLCipher
build derives the per-page HMAC differently, and a failed open attempt also
rewrites or removes the ``-wal`` sidecar). This module therefore decrypts the
pages manually and re-assembles a plain SQLite database.

Page layout implemented here (Xiaohongshu's verified configuration):

    page size      = 1024
    kdf            = PBKDF2-HMAC-SHA1, 64000 iterations, 32-byte key
    cipher         = AES-256-CBC, IV stored per page in the reserve area
    per-page       = [ciphertext][IV(16)][HMAC-SHA256(32)]  (reserve = 48)
    page 1         = first 16 bytes hold the KDF salt, its plaintext starts
                     with the SQLite magic that sqlcipher writes itself

Encrypted layouts:
    page 1:  [salt 16][ciphertext 960][iv 16][hmac 32]
    page N:  [ciphertext 976][iv 16][hmac 32]

WAL frames are decrypted with the same key and merged with standard SQLite
WAL semantics: frames matching the WAL header salts are replayed in order and
the last committed frame per page wins. Only committed frames are applied.

Decryption requires the ``cryptography`` package (``pip install cryptography``,
available as the ``[xhs]`` extra of this tool). The source file is opened
read-only; nothing is ever written back to the original backup.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
import os
from pathlib import Path
import struct

from ..errors import ExportError

MAGIC = b"SQLite format 3\x00"
PAGE_SIZE = 1024
IV_SIZE = 16
HMAC_SIZE = 32
RESERVE = IV_SIZE + HMAC_SIZE
SALT_SIZE = 16
KDF_ITER = 64000
KDF_ALGORITHM = "sha1"
HMAC_ALGORITHM = "sha256"
HMAC_SALT_MASK = 0x3A
FAST_KDF_ITER = 2
WAL_HDR_SIZE = 32
WAL_FRAME_HDR_SIZE = 24

CRYPTO_HINT = (
    "Decrypting this app's WCDB databases needs the 'cryptography' package. "
    "Install it with 'pip install cryptography' (or 'pip install "
    "duoshan-chat-export-tool[xhs]') and try again."
)


def _load_cryptography():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        return Cipher, algorithms, modes
    except ImportError as error:  # pragma: no cover - depends on environment
        raise ExportError(CRYPTO_HINT) from error


def derive_keys(passphrase: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """Return ``(aes_key, hmac_key)`` for a passphrase and the page-1 salt."""
    aes_key = hashlib.pbkdf2_hmac(KDF_ALGORITHM, passphrase, salt, KDF_ITER, dklen=32)
    hmac_salt = bytes(byte ^ HMAC_SALT_MASK for byte in salt)
    hmac_key = hashlib.pbkdf2_hmac(
        KDF_ALGORITHM, aes_key, hmac_salt, FAST_KDF_ITER, dklen=32)
    return aes_key, hmac_key


def verify_page_hmac(page: bytes, pgno: int, hmac_key: bytes) -> bool:
    """Verify one encrypted 1024-byte page image against its stored HMAC."""
    stored = page[PAGE_SIZE - HMAC_SIZE:]
    mac = hmac_lib.new(
        hmac_key, page[0:PAGE_SIZE - HMAC_SIZE] + struct.pack("<I", pgno),
        getattr(hashlib, HMAC_ALGORITHM)).digest()
    return hmac_lib.compare_digest(mac, stored)


def _decrypt_page_image(page: bytes, aes_key: bytes, pgno: int) -> bytes:
    """Decrypt one 1024-byte page image into the plaintext page image."""
    if len(page) < PAGE_SIZE:
        raise ValueError(f"page image too small: {len(page)}")
    if pgno == 1:
        data_start = SALT_SIZE
        data_len = PAGE_SIZE - RESERVE - SALT_SIZE
    else:
        data_start = 0
        data_len = PAGE_SIZE - RESERVE
    iv = page[PAGE_SIZE - RESERVE:PAGE_SIZE - RESERVE + IV_SIZE]
    ciphertext = page[data_start:data_start + data_len]
    if len(ciphertext) % 16:
        raise ValueError("ciphertext is not AES block aligned")
    Cipher, algorithms, modes = _load_cryptography()
    decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    out = bytearray(PAGE_SIZE)
    if pgno == 1:
        out[0:SALT_SIZE] = MAGIC
    out[data_start:data_start + len(plaintext)] = plaintext
    return bytes(out)


def decrypt_main_file(path: os.PathLike[str] | str, passphrase: bytes) -> tuple[dict[int, bytes], bytes]:
    """Decrypt the main database file. Returns ``({pgno: page}, kdf_salt)``."""
    data = Path(path).read_bytes()
    if len(data) % PAGE_SIZE or not data:
        raise ExportError(
            f"{Path(path).name} is not a {PAGE_SIZE}-byte-page WCDB database (size {len(data)}).")
    salt = data[0:SALT_SIZE]
    aes_key, _ = derive_keys(passphrase, salt)
    pages: dict[int, bytes] = {}
    for pgno in range(1, len(data) // PAGE_SIZE + 1):
        offset = (pgno - 1) * PAGE_SIZE
        pages[pgno] = _decrypt_page_image(data[offset:offset + PAGE_SIZE], aes_key, pgno)
    return pages, salt


def decrypt_wal(path: os.PathLike[str] | str, passphrase: bytes, salt: bytes,
                pages: dict[int, bytes]) -> tuple[dict[int, bytes], int]:
    """Decrypt WAL frames and merge committed pages into ``pages`` in place.

    ``salt`` is the KDF salt from the main file's page 1. Returns
    ``({pgno: page}, applied_frame_count)``. Frames whose salts do not match
    the WAL header (older generations past the valid region) end the replay.
    """
    wal_path = Path(path)
    if not wal_path.is_file():
        return pages, 0
    data = wal_path.read_bytes()
    if len(data) < WAL_HDR_SIZE:
        return pages, 0
    magic, _version, page_size, _ckpt, salt1, salt2, _c1, _c2 = struct.unpack(
        ">8I", data[:WAL_HDR_SIZE])
    if magic not in (0x377F0682, 0x377F0683) or page_size != PAGE_SIZE:
        return pages, 0
    aes_key, _ = derive_keys(passphrase, salt)
    frame_size = WAL_FRAME_HDR_SIZE + page_size
    applied = 0
    position = WAL_HDR_SIZE
    pending: dict[int, bytes] = {}
    while position + frame_size <= len(data):
        pgno, commit_size, fsalt1, fsalt2, _fc1, _fc2 = struct.unpack(
            ">6I", data[position:position + WAL_FRAME_HDR_SIZE])
        if fsalt1 != salt1 or fsalt2 != salt2:
            break
        try:
            page = _decrypt_page_image(
                data[position + WAL_FRAME_HDR_SIZE:position + frame_size], aes_key, pgno)
        except Exception:
            break
        pending[pgno] = page
        if commit_size > 0:
            # End of a commit record: everything accumulated becomes visible.
            pages.update(pending)
            pending.clear()
            applied += 1
        position += frame_size
    return pages, applied


def decrypt_database(db_path: os.PathLike[str] | str, passphrase: bytes,
                     destination: os.PathLike[str] | str) -> dict:
    """Decrypt ``db_path`` (main file plus optional ``-wal``) to a plain SQLite file.

    Returns ``{"pages", "wal_commits", "size", "output"}``. The destination is
    written atomically after the assembled image is verified to start with a
    valid SQLite header; the original files are never modified.
    """
    source = Path(db_path)
    if not source.is_file():
        raise ExportError(f"Database file not found: {source}")
    if Path(destination).resolve() == source.resolve():
        raise ExportError("Refusing to overwrite the source database with its decrypted copy.")
    pages, salt = decrypt_main_file(source, passphrase)
    wal_path = source.parent / (source.name + "-wal")
    pages, commits = decrypt_wal(wal_path, passphrase, salt, pages)
    total = max(pages) if pages else 0
    image = bytearray()
    for pgno in range(1, total + 1):
        image += pages.get(pgno, bytes(PAGE_SIZE))
    if image[:16] != MAGIC:
        raise ExportError(
            f"Could not decrypt {source.name} with the known {Path(db_path).name} passphrase: "
            "the decrypted page 1 does not contain a SQLite header. The password may have "
            "rotated or the database may use an unsupported cipher configuration.")
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(destination_path.name + f".{os.getpid()}.tmp")
    temporary.write_bytes(bytes(image))
    _verify_integrity(temporary, source.name)
    temporary.replace(destination_path)
    return {"pages": total, "wal_commits": commits, "size": len(image),
            "output": str(destination_path)}


def _verify_integrity(path: Path, label: str) -> None:
    import sqlite3

    try:
        connection = sqlite3.connect(path)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
    except sqlite3.DatabaseError as error:
        raise ExportError(
            f"Decrypted {label} is not a readable SQLite database ({error}). "
            "The passphrase is wrong or the cipher configuration is unsupported.") from error
    if result != [("ok",)]:
        temporary_rows = result[:3]
        raise ExportError(
            f"Decrypted {label} failed its integrity check: {temporary_rows!r}. "
            "The page images did not reassemble into a consistent database.")
