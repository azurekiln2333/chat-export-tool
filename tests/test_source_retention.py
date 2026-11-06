"""Archive retries retain superseded and incomplete files instead of deleting."""
from contextlib import contextmanager
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from duoshan_export import source


class SourceRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-retention-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "backup.tar"
        self.destination = self.root / "work" / "extracted"

    def make_archive(self, value):
        with tarfile.open(self.archive, "w") as archive:
            member = tarfile.TarInfo("my.maya.android/databases/test.db")
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))

    def test_changed_archive_retains_previous_extraction(self):
        self.make_archive(b"first snapshot")
        source.extract_archive(self.archive, self.destination, relevant_only=False, root=self.destination.parent)
        self.make_archive(b"second snapshot")
        source.extract_archive(self.archive, self.destination, relevant_only=False, root=self.destination.parent)
        relative = "my.maya.android/databases/test.db"
        self.assertEqual((self.destination / relative).read_bytes(), b"second snapshot")
        prior = list(self.destination.parent.glob("extracted.retained-*"))
        self.assertEqual(len(prior), 1)
        self.assertEqual((prior[0] / relative).read_bytes(), b"first snapshot")
        reused = source.extract_archive(self.archive, self.destination, relevant_only=False, root=self.destination.parent)
        self.assertTrue(reused["reused"])

    def test_malformed_member_retains_partial_without_publishing_it(self):
        self.archive.write_bytes(b"archive fixture")
        class MalformedArchive:
            def __iter__(self):
                member = tarfile.TarInfo("my.maya.android/databases/test.db")
                member.size = 3
                yield member
            def extractfile(self, member):
                return io.BytesIO(b"member longer than its declared size")
        @contextmanager
        def open_archive(path):
            yield MalformedArchive()
        with patch.object(source, "_open_tar", open_archive), patch.object(Path, "unlink", side_effect=AssertionError("unexpected deletion")):
            result = source.extract_archive(self.archive, self.destination, relevant_only=False)
        self.assertEqual(result["extracted"], 0)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertFalse((self.destination / "my.maya.android/databases/test.db").exists())
        partials = list(self.destination.rglob("*.partial-*"))
        self.assertEqual(len(partials), 1)
        self.assertTrue(partials[0].read_bytes())


if __name__ == "__main__":
    unittest.main()
