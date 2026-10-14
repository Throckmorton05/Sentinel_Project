"""Tests for Sentinel Guardian authentication."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sentinel.daemon.security import guardian_auth


class GuardianAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        """
        Give every test its own temporary Guardian credential file.
        """

        self.temp_dir = tempfile.TemporaryDirectory()

        self.auth_path = Path(self.temp_dir.name) / "guardian-auth.json"

        self._real_fstat = os.fstat

        def root_owned_fstat(
            fd: int,
        ) -> os.stat_result:
            info = self._real_fstat(fd)

            values = list(info)

            #
            # stat_result:
            #
            # 0 = mode
            # 4 = uid
            # 5 = gid
            #

            values[4] = 0
            values[5] = 0

            return os.stat_result(
                values,
            )

        #
        # Production correctly requires the credential store
        # and its parent directory to be root-owned.
        #
        # Tests run as the development user, so mock only the
        # ownership checks.
        #

        self.parent_validation_patch = patch.object(
            guardian_auth,
            "_validate_parent",
        )

        self.fstat_patch = patch.object(
            guardian_auth.os,
            "fstat",
            side_effect=root_owned_fstat,
        )

        self.parent_validation_patch.start()
        self.fstat_patch.start()

        guardian_auth._current_path = None

    def tearDown(self) -> None:
        guardian_auth._current_path = None

        self.fstat_patch.stop()
        self.parent_validation_patch.stop()

        self.temp_dir.cleanup()

    def initialize(self, *, configure=True):
        guardian_auth.initialize_guardian_auth(self.auth_path)
        if configure:
            guardian_auth.setup_guardian_pin("4826", "4826")

    def test_fresh_store_has_no_pin_and_rejects_the_old_default(self):
        self.initialize(configure=False)
        self.assertFalse(guardian_auth.guardian_pin_configured())
        self.assertFalse(guardian_auth.verify_guardian_pin("1234"))
        self.assertFalse(guardian_auth.verify_guardian_pin("4826"))
        self.assertEqual(json.loads(self.auth_path.read_text()),
                         {"version": 2, "configured": False})
        guardian_auth.initialize_guardian_auth(self.auth_path)
        self.assertFalse(guardian_auth.guardian_pin_configured())

    def test_setup_requires_matching_valid_pins_and_is_one_time(self):
        self.initialize(configure=False)
        original = self.auth_path.read_bytes()
        for new, repeated in (("", ""), ("12", "12"), ("abcd", "abcd"),
                              ("１２３４", "１２３４"), ("1"*33, "1"*33),
                              ("4826", "5678"), ("4826", None)):
            with self.subTest(new=new):
                with self.assertRaises(ValueError):
                    guardian_auth.setup_guardian_pin(new, repeated)
                self.assertEqual(self.auth_path.read_bytes(), original)
        guardian_auth.setup_guardian_pin("4826", "4826")
        self.assertTrue(guardian_auth.guardian_pin_configured())
        with self.assertRaisesRegex(ValueError, "already configured"):
            guardian_auth.setup_guardian_pin("5678", "5678")
        self.assertTrue(guardian_auth.verify_guardian_pin("4826"))

    def test_pin_is_hashed_and_auth_file_is_private(self):
        self.initialize()
        contents = self.auth_path.read_text()
        self.assertNotIn('"4826"', contents)
        data = json.loads(contents)
        self.assertEqual(data["algorithm"], "scrypt")
        self.assertTrue(data["configured"])
        self.assertIn("salt", data)
        self.assertIn("hash", data)
        self.assertEqual(self.auth_path.stat().st_mode & 0o777, 0o600)

    def test_wrong_pin_is_rejected(self):
        self.initialize()
        self.assertFalse(guardian_auth.verify_guardian_pin("9999"))
        self.assertFalse(guardian_auth.verify_guardian_pin("1234"))

    def test_change_pin_requires_current_pin_and_survives_restart(self):
        self.initialize()
        before = self.auth_path.read_bytes()
        with self.assertRaises(ValueError):
            guardian_auth.change_guardian_pin("9999", "5678")
        self.assertEqual(self.auth_path.read_bytes(), before)
        guardian_auth.change_guardian_pin("4826", "5678")
        guardian_auth._current_path = None
        guardian_auth.initialize_guardian_auth(self.auth_path, require_configured=True)
        self.assertTrue(guardian_auth.verify_guardian_pin("5678"))
        self.assertFalse(guardian_auth.verify_guardian_pin("4826"))

    def test_unconfigured_store_cannot_change_pin(self):
        self.initialize(configure=False)
        with self.assertRaises(ValueError):
            guardian_auth.change_guardian_pin("1234", "5678")
        self.assertFalse(guardian_auth.guardian_pin_configured())

    def test_invalid_pin_format_is_rejected(self):
        self.initialize()
        for pin in ("", "12", "abcd", "12ab", "１２３４", "1"*33):
            with self.subTest(pin=pin):
                with self.assertRaises(ValueError):
                    guardian_auth.verify_guardian_pin(pin)

    def test_legacy_hashes_are_preserved_including_an_old_default(self):
        for pin in ("5678", "1234"):
            with self.subTest(pin=pin):
                legacy = guardian_auth._build_auth_data(pin)
                legacy["version"] = 1
                del legacy["configured"]
                self.auth_path.write_text(json.dumps(legacy))
                self.auth_path.chmod(0o600)
                before = self.auth_path.read_bytes()
                guardian_auth.initialize_guardian_auth(self.auth_path, require_configured=True)
                self.assertTrue(guardian_auth.guardian_pin_configured())
                self.assertTrue(guardian_auth.verify_guardian_pin(pin))
                self.assertEqual(self.auth_path.read_bytes(), before)
                with self.assertRaises(ValueError):
                    guardian_auth.setup_guardian_pin("9876", "9876")

    def test_locked_startup_rejects_missing_or_unset_credentials(self):
        with self.assertRaisesRegex(ValueError, "existing configured PIN"):
            guardian_auth.initialize_guardian_auth(self.auth_path, require_configured=True)
        self.assertFalse(self.auth_path.exists())
        self.initialize(configure=False)
        before = self.auth_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "existing configured PIN"):
            guardian_auth.initialize_guardian_auth(self.auth_path, require_configured=True)
        self.assertEqual(self.auth_path.read_bytes(), before)

    def test_corrupt_and_dangling_symlink_stores_are_not_replaced(self):
        self.auth_path.write_text('{"version":2,"configured":"false"}')
        self.auth_path.chmod(0o600)
        before = self.auth_path.read_bytes()
        with self.assertRaises(ValueError):
            guardian_auth.initialize_guardian_auth(self.auth_path)
        self.assertEqual(self.auth_path.read_bytes(), before)
        self.auth_path.unlink()
        missing = self.auth_path.parent / 'missing.json'
        self.auth_path.symlink_to(missing)
        with self.assertRaises(ValueError):
            guardian_auth.initialize_guardian_auth(self.auth_path)
        self.assertTrue(self.auth_path.is_symlink())
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
