"""Failed private snapshots/decryption remain inspectable and retryable."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from duoshan_export import database


class DatabaseRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="database-retention-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "111111_im.db"
        self.work = self.root / "work"
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("CREATE TABLE msg (text TEXT)")
            db.execute("INSERT INTO msg VALUES ('original')")
            db.commit()

    def test_partial_snapshot_is_retained_and_retry_succeeds(self):
        original = self.source.read_bytes()
        def fail_copy(source, target):
            Path(target).write_bytes(b"retained partial snapshot")
            raise OSError("synthetic disk full")
        with patch.object(database.shutil, "copyfile", side_effect=fail_copy), patch.object(database.shutil, "rmtree", side_effect=AssertionError("unexpected deletion")):
            with self.assertRaisesRegex(OSError, "disk full"):
                database._snapshot(self.source, self.work)
        partials = list(self.work.rglob("*.tmp/111111_im.db"))
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].read_bytes(), b"retained partial snapshot")
        snapshot = database._snapshot(self.source, self.work)
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertEqual(partials[0].read_bytes(), b"retained partial snapshot")
        self.assertEqual(self.source.read_bytes(), original)

    def test_decryption_failure_keeps_destination_and_partial_then_retries(self):
        original = self.source.read_bytes()
        database._snapshot(self.source, self.work)
        target = self.root / "plain.sqlite"
        target.write_bytes(b"previous output")
        def fail_copy(source, destination):
            Path(destination).write_bytes(b"retained partial plaintext")
            raise OSError("synthetic disk full")
        with patch.object(database.shutil, "copyfile", side_effect=fail_copy), patch.object(Path, "unlink", side_effect=AssertionError("unexpected deletion")):
            with self.assertRaisesRegex(OSError, "disk full"):
                database.decrypt_database(self.source, "111111", target, self.work)
        self.assertEqual(target.read_bytes(), b"previous output")
        partials = list(self.root.glob("plain.sqlite.*.tmp"))
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].read_bytes(), b"retained partial plaintext")
        database.decrypt_database(self.source, "111111", target, self.work)
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("SELECT text FROM msg").fetchone(), ("original",))
        self.assertEqual(len(list(self.root.glob("plain.sqlite.retained-*"))), 1)
        self.assertTrue(partials[0].exists())
        self.assertEqual(self.source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
