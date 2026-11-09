"""Classic key selection cannot reuse/truncate another account's NT key."""
from pathlib import Path
import unittest

from duoshan_export.apps.qq_legacy_keys import legacy_key_candidates

KEY = "00112233445566778899aabbccddeeff"
OTHER = "ffeeddccbbaa99887766554433221100"


class LegacyKeyTests(unittest.TestCase):
    def test_account_and_relative_database_scope(self):
        keys = {"account-a": {"Msg3.0.db": {"legacy_key": KEY}}, "account-b": OTHER}
        self.assertEqual(legacy_key_candidates(keys, account="account-a", relative_path="Msg3.0.db"), [bytes.fromhex(KEY)])
        self.assertEqual(legacy_key_candidates(keys, account="account-c", relative_path="Msg3.0.db"), [])
        self.assertEqual(legacy_key_candidates(keys, account="account-a", relative_path="Other.db"), [])

    def test_explicit_legacy_key_and_duplicates(self):
        entries = [{"legacy_key": KEY, "key": OTHER * 2}, "x'" + KEY + "'", bytes.fromhex(KEY)]
        self.assertEqual(legacy_key_candidates(entries), [bytes.fromhex(KEY)])

    def test_nt_keys_passwords_and_malformed_inputs_are_rejected(self):
        for value in (KEY * 2, bytes.fromhex(KEY * 2), "password", "0" * 32, {"passphrase": KEY}, "x'" + KEY, 123, None):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(legacy_key_candidates(value), [])

    def test_windows_path_selectors_and_multiple_candidates(self):
        keys = {"nt_qq\\Msg3.0.db": [KEY, OTHER]}
        self.assertEqual(legacy_key_candidates(keys, relative_path="nt_qq/Msg3.0.db"), [bytes.fromhex(KEY), bytes.fromhex(OTHER)])
