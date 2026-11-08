"""Independent SQLCipher fixtures for desktop page profiles and committed WAL."""
from contextlib import closing
import hashlib
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from duoshan_export.apps import desktop_crypto as crypto
from duoshan_export.errors import ExportError

try:
    import sqlcipher3
except ImportError:
    sqlcipher3 = None


class DesktopCryptoTests(unittest.TestCase):
    def setUp(self):
        # Retained by design: workspace deletion rules also cover test fixtures.
        self.root = Path(tempfile.mkdtemp(prefix="desktop-crypto-"))

    def _fixture(self, profile, *, wal=False, larger_reserve=False):
        source = self.root / (profile + ".db")
        db = sqlcipher3.connect(str(source))
        if profile in ("wechat3", "qqnt") and not larger_reserve:
            # Configure defaults before key: SQLCipher 4.12 otherwise retains
            # an 80-byte SQLite pager reserve even with a 48-byte SHA1 codec.
            db.execute("PRAGMA cipher_default_hmac_algorithm=HMAC_SHA1")
        try:
            db.execute("PRAGMA key='synthetic-password-not-a-real-key'")
        finally:
            db.execute("PRAGMA cipher_default_hmac_algorithm=HMAC_SHA512")
        db.execute("PRAGMA cipher_page_size=4096")
        if profile == "wechat3":
            db.execute("PRAGMA kdf_iter=64000")
            db.execute("PRAGMA cipher_hmac_algorithm=HMAC_SHA1")
            db.execute("PRAGMA cipher_kdf_algorithm=PBKDF2_HMAC_SHA1")
        elif profile == "qqnt":
            db.execute("PRAGMA kdf_iter=4000")
            db.execute("PRAGMA cipher_hmac_algorithm=HMAC_SHA1")
            db.execute("PRAGMA cipher_kdf_algorithm=PBKDF2_HMAC_SHA512")
        elif profile == "qqnt4":
            db.execute("PRAGMA kdf_iter=4000")
        if wal:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE message(id INTEGER PRIMARY KEY, text TEXT)")
        db.execute("INSERT INTO message VALUES(1,'基础记录')")
        db.commit()
        if wal:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("INSERT INTO message VALUES(2,'只存在于已提交 WAL')")
            db.commit()
            db.execute("INSERT INTO message VALUES(3,'未提交，必须排除')")
        snapshot = self.root / ("snapshot-" + profile + ".db")
        data = source.read_bytes()
        if profile.startswith("qqnt"):
            prefix = bytearray(1024)
            prefix[:16] = b"SQLite header 3\0"
            prefix[32:40] = b"QQ_NT DB"
            data = bytes(prefix) + data
        snapshot.write_bytes(data)
        if wal:
            shutil.copyfile(str(source) + "-wal", str(snapshot) + "-wal")
        db.close()
        return snapshot

    @unittest.skipUnless(sqlcipher3, "SQLCipher binding required for independent encrypted fixtures")
    def test_sqlite_reserve_larger_than_codec_is_preserved(self):
        source = self._fixture("wechat3", larger_reserve=True)
        output = crypto.decrypt_snapshot(source, self.root / "larger-reserve.db",
            keys="synthetic-password-not-a-real-key", progress=lambda _: None)
        with closing(sqlite3.connect(output)) as db:
            self.assertEqual(db.execute("SELECT * FROM message").fetchall(), [(1, "基础记录")])

    @unittest.skipUnless(sqlcipher3, "SQLCipher binding required for independent encrypted fixtures")
    def test_profiles_and_committed_wal_roundtrip_without_source_changes(self):
        for profile in ("wechat4", "wechat3", "qqnt", "qqnt4"):
            with self.subTest(profile=profile):
                source = self._fixture(profile, wal=True)
                paths = (source, Path(str(source) + "-wal"))
                before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
                target = crypto.decrypt_snapshot(source, self.root / (profile + "-plain.db"),
                    keys={"test-account": {"passphrase": "synthetic-password-not-a-real-key"}},
                    account="test-account", app="qq_pc" if profile == "qqnt" else "wechat_pc", progress=lambda _: None)
                with closing(sqlite3.connect(target)) as db:
                    self.assertEqual(db.execute("SELECT * FROM message ORDER BY id").fetchall(), [(1, "基础记录"), (2, "只存在于已提交 WAL")])
                self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])

    @unittest.skipUnless(sqlcipher3, "SQLCipher binding required for independent encrypted fixtures")
    def test_qq_zero_preallocation_is_excluded_but_zero_live_pages_fail(self):
        source = self._fixture("qqnt4")
        with source.open("ab") as stream:
            stream.write(bytes(4096 * 3))
        output = crypto.decrypt_snapshot(source, self.root / "preallocated.db",
            keys="synthetic-password-not-a-real-key", progress=lambda _: None)
        self.assertEqual(output.stat().st_size, source.stat().st_size - 1024 - 3 * 4096)
        with closing(sqlite3.connect(output)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM message").fetchone()[0], 1)
        with source.open("r+b") as stream:
            stream.seek(1024 + 4096)
            stream.write(bytes(4096))
        with self.assertRaisesRegex(ExportError, "第 2 页完整性"):
            crypto.decrypt_snapshot(source, self.root / "zero-live.db",
                keys="synthetic-password-not-a-real-key", progress=lambda _: None)
        self.assertFalse((self.root / "zero-live.db").exists())

    @unittest.skipUnless(sqlcipher3, "SQLCipher binding required for independent encrypted fixtures")
    def test_qq_context_recovery_binds_candidate_to_database_salt_and_hmac(self):
        source = self._fixture("qqnt4")
        page = source.read_bytes()[1024:5120]
        salt = page[:16]
        key = hashlib.pbkdf2_hmac("sha512", b"synthetic-password-not-a-real-key", salt, 4000, 32)
        context = bytearray(176)
        struct.pack_into("<7I", context, 4, 4000, 2, 16, 32, 16, 16, 4096)
        struct.pack_into("<Q", context, 72, 0x20000)
        struct.pack_into("<Q", context, 104, 0x30000)
        cipher = bytearray(40)
        struct.pack_into("<Q", cipher, 8, 0x40000)
        memory = {0x10000: context, 0x20000: salt, 0x30000: cipher, 0x40000: key}
        def read(address, length):
            for base, data in memory.items():
                if base <= address < base + len(data):
                    return bytes(data[address - base:address - base + length])
            return b""
        found = dict(crypto._qq_context_keys(context, 0x10000, read, {salt: page}))
        self.assertEqual(found, {salt.hex(): {"enc_key": key.hex()}})
        memory[0x40000] = bytes(32)
        self.assertEqual(list(crypto._qq_context_keys(context, 0x10000, read, {salt: page})), [])

    @unittest.skipUnless(sqlcipher3, "SQLCipher binding required for independent encrypted fixtures")
    def test_bad_key_and_corruption_never_publish(self):
        source = self._fixture("wechat4")
        for keys in ({"wrong-account": "not-it"}, {"default": "wrong-key"}):
            with self.assertRaises(ExportError):
                crypto.decrypt_snapshot(source, self.root / "bad.db", keys=keys, account="account", progress=lambda _: None)
            self.assertFalse((self.root / "bad.db").exists())
        damaged = bytearray(source.read_bytes())
        damaged[-150] ^= 0xFF
        source.write_bytes(damaged)
        with self.assertRaises(ExportError):
            crypto.decrypt_snapshot(source, self.root / "corrupt.db", keys="synthetic-password-not-a-real-key", progress=lambda _: None)
        self.assertFalse((self.root / "corrupt.db").exists())

    @unittest.skipUnless(sqlcipher3, "SQLCipher binding required for independent encrypted fixtures")
    def test_raw_key_and_invalid_wal_never_publish(self):
        source = self._fixture("wechat4", wal=True)
        salt = source.read_bytes()[:16]
        key = hashlib.pbkdf2_hmac("sha512", b"synthetic-password-not-a-real-key", salt, 256000, 32)
        target = crypto.decrypt_snapshot(source, self.root / "raw-key.db",
            keys={salt.hex(): {"enc_key": key.hex()}}, progress=lambda _: None)
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM message").fetchone()[0], 2)
        wal = Path(str(source) + "-wal")
        damaged = bytearray(wal.read_bytes())
        damaged[24] ^= 1
        wal.write_bytes(damaged)
        with self.assertRaisesRegex(ExportError, "WAL 文件头校验失败"):
            crypto.decrypt_snapshot(source, self.root / "bad-wal.db",
                keys=key, progress=lambda _: None)
        self.assertFalse((self.root / "bad-wal.db").exists())

    @unittest.skipUnless(sqlcipher3, "SQLCipher binding required for independent encrypted fixtures")
    def test_committed_wal_excludes_valid_uncommitted_tail(self):
        source = self._fixture("wechat4", wal=True)
        wal = Path(str(source) + "-wal")
        committed, size = crypto._committed_wal(wal, None)
        data = wal.read_bytes()
        last = 32 + ((len(data) - 32) // (24 + crypto.PAGE_SIZE) - 1) * (24 + crypto.PAGE_SIZE)
        frame = data[last:last + 24]
        page = data[last + 24:last + 24 + crypto.PAGE_SIZE]
        header = struct.pack(">II", 9999, 0) + data[16:24]
        endian = "<" if data[:4] == bytes.fromhex("377f0682") else ">"
        checksum = crypto._wal_checksum(header[:8] + page, endian, struct.unpack(">II", frame[16:24]))
        with wal.open("ab") as stream:
            stream.write(header + struct.pack(">II", *checksum) + page)
        self.assertEqual(crypto._committed_wal(wal, None), (committed, size))
        result = crypto.decrypt_snapshot(source, self.root / "uncommitted-tail.db",
            keys="synthetic-password-not-a-real-key", progress=lambda _: None)
        with closing(sqlite3.connect(result)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM message").fetchone()[0], 2)

    def test_plaintext_cancel_does_not_publish(self):
        source = self.root / "cancel-source.db"
        with closing(sqlite3.connect(source)) as db:
            db.execute("CREATE TABLE t(value)")
            db.execute("INSERT INTO t VALUES('retained')")
            db.commit()
        cancelled = False
        def progress(event):
            nonlocal cancelled
            cancelled = True
        with self.assertRaisesRegex(ExportError, "已取消"):
            crypto.decrypt_snapshot(source, self.root / "cancelled.db",
                progress=progress, cancel=lambda: cancelled)
        self.assertFalse((self.root / "cancelled.db").exists())
        self.assertTrue(list(self.root.glob("cancelled.db.partial-*")))

    def test_qq_normalization_preserves_raw_values_rowids_and_schema(self):
        source = self.root / "qq-source.db"
        with closing(sqlite3.connect(source)) as db:
            db.execute("CREATE TABLE data(number INTEGER, text TEXT, binary BLOB)")
            db.execute("CREATE INDEX by_number ON data(number)")
            db.execute("INSERT INTO data(rowid,number,text,binary) VALUES(?,?,?,?)", (42, 2**60 + 3, "甲\0乙", b"\0\xff\x01"))
            db.execute("PRAGMA user_version=7")
            db.commit()
        before = hashlib.sha256(source.read_bytes()).digest()
        target = self.root / "normalized.db"
        crypto._normalize_qq_database(source, target, progress=lambda _: None, cancel=None)
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("SELECT rowid,* FROM data").fetchall(), [(42, 2**60 + 3, "甲\0乙", b"\0\xff\x01")])
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='by_number'").fetchone()[0], 1)
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).digest())

    def test_qq_normalization_cancel_stops_only_helper(self):
        source = self.root / "qq-cancel-source.db"
        with closing(sqlite3.connect(source)) as db:
            db.execute("CREATE TABLE data(value)")
        cancelled = False
        def progress(event):
            nonlocal cancelled
            cancelled = True
        with self.assertRaisesRegex(ExportError, "已取消"):
            crypto._normalize_qq_database(source, self.root / "cancelled-normalize.partial",
                progress=progress, cancel=lambda: cancelled)
        self.assertFalse(any(t.name == "qq-normalizer-cancel" for t in __import__("threading").enumerate()))

    def test_plaintext_wal_is_read_from_private_copy(self):
        source = self.root / "plain.db"
        with closing(sqlite3.connect(source)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("CREATE TABLE t(value TEXT)")
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("INSERT INTO t VALUES('WAL 数据')")
            db.commit()
            copied = self.root / "copied.db"
            shutil.copyfile(source, copied)
            shutil.copyfile(str(source) + "-wal", str(copied) + "-wal")
            result = crypto.decrypt_snapshot(copied, self.root / "standalone.db", progress=lambda _: None)
            with closing(sqlite3.connect(result)) as target:
                self.assertEqual(target.execute("SELECT * FROM t").fetchall(), [("WAL 数据",)])

    def test_legacy_qq_rejected_instead_of_empty_success(self):
        source = self.root / "Msg3.0.db"
        source.write_bytes(b"SQLite header 3\0" + bytes(4096))
        with self.assertRaisesRegex(ExportError, "旧版 QQ/TIM"):
            crypto.decrypt_snapshot(source, self.root / "legacy.db", keys="key", progress=lambda _: None)


if __name__ == "__main__":
    unittest.main()
