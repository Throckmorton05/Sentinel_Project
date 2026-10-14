"""Tests for Sentinel's persistent lock-mode state."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sentinel.daemon.core import lock_state


class LockStateTests(unittest.TestCase):
    def setUp(self) -> None:
        """
        Give every test its own temporary state file.

        The real /var/lib/sentinel/lock-state.json is never touched.
        """

        self.temp_dir = tempfile.TemporaryDirectory()

        self.state_path = Path(self.temp_dir.name) / "lock-state.json"

        #
        # Production correctly requires /var/lib/sentinel and the
        # state file to be root-owned.
        #
        # These tests run as the development user, so mock only the
        # ownership checks while keeping the actual file I/O,
        # persistence, JSON validation, and permissions behavior.
        #

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

            return os.stat_result(values)

        self.parent_validation_patch = patch.object(
            lock_state,
            "_validate_parent",
        )

        self.fstat_patch = patch.object(
            lock_state.os,
            "fstat",
            side_effect=root_owned_fstat,
        )

        self.parent_validation_patch.start()
        self.fstat_patch.start()

        #
        # Make sure no state from another test survives in memory.
        #

        lock_state._current_state = None
        lock_state._current_path = None

    def tearDown(self) -> None:
        lock_state._current_state = None
        lock_state._current_path = None

        self.fstat_patch.stop()
        self.parent_validation_patch.stop()

        self.temp_dir.cleanup()

    def test_fresh_state_starts_unlocked(
        self,
    ) -> None:
        state = lock_state.initialize_lock_state(self.state_path)

        self.assertEqual(
            state.mode,
            lock_state.LockMode.UNLOCKED,
        )

        self.assertTrue(self.state_path.exists())

        data = json.loads(self.state_path.read_text(encoding="utf-8"))

        self.assertEqual(
            data,
            {
                "mode": "unlocked",
                "version": 1,
            },
        )

    def test_state_file_is_private(
        self,
    ) -> None:
        lock_state.initialize_lock_state(self.state_path)

        permissions = self.state_path.stat().st_mode & 0o777

        self.assertEqual(
            permissions,
            0o600,
        )

    def test_enter_self_lock(
        self,
    ) -> None:
        lock_state.initialize_lock_state(self.state_path)

        state = lock_state.enter_self_lock()

        self.assertEqual(
            state.mode,
            lock_state.LockMode.SELF_LOCK,
        )

        data = json.loads(self.state_path.read_text(encoding="utf-8"))

        self.assertEqual(
            data,
            {
                "mode": "self_lock",
                "version": 1,
            },
        )

    def test_self_lock_survives_restart(
        self,
    ) -> None:
        lock_state.initialize_lock_state(self.state_path)

        lock_state.enter_self_lock()

        #
        # Simulate sentineld exiting.
        #
        # This removes the in-memory state while leaving the
        # temporary persistent state file intact.
        #

        lock_state._current_state = None
        lock_state._current_path = None

        #
        # Simulate sentineld starting again.
        #

        state = lock_state.initialize_lock_state(self.state_path)

        self.assertEqual(
            state.mode,
            lock_state.LockMode.SELF_LOCK,
        )

    def test_self_lock_cannot_be_entered_twice(
        self,
    ) -> None:
        lock_state.initialize_lock_state(self.state_path)

        lock_state.enter_self_lock()

        with self.assertRaises(ValueError):
            lock_state.enter_self_lock()

    def test_invalid_existing_state_fails_closed(
        self,
    ) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "mode": "not-a-real-mode",
                }
            ),
            encoding="utf-8",
        )

        self.state_path.chmod(0o600)

        with self.assertRaises(ValueError):
            lock_state.initialize_lock_state(self.state_path)

    def test_no_self_unlock_function_exists(
        self,
    ) -> None:
        self.assertFalse(
            hasattr(
                lock_state,
                "exit_self_lock",
            )
        )

        self.assertFalse(
            hasattr(
                lock_state,
                "unlock_self_lock",
            )
        )

    def test_enter_guardian_lock(
        self,
    ) -> None:
        lock_state.initialize_lock_state(
            self.state_path,
        )

        state = lock_state.enter_guardian_lock()

        self.assertEqual(
            state.mode,
            lock_state.LockMode.GUARDIAN_LOCK,
        )

        data = json.loads(
            self.state_path.read_text(
                encoding="utf-8",
            )
        )

        self.assertEqual(
            data,
            {
                "mode": "guardian_lock",
                "version": 1,
            },
        )

    def test_guardian_must_exit_before_entering_self_lock(self):
        lock_state.initialize_lock_state(self.state_path)
        lock_state.enter_guardian_lock()
        before = self.state_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "must be unlocked"):
            lock_state.enter_self_lock()
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(lock_state.get_lock_state().mode, lock_state.LockMode.GUARDIAN_LOCK)
        lock_state.exit_guardian_lock()
        self.assertEqual(lock_state.enter_self_lock().mode, lock_state.LockMode.SELF_LOCK)

    def test_exit_guardian_lock(
        self,
    ) -> None:
        lock_state.initialize_lock_state(
            self.state_path,
        )

        lock_state.enter_guardian_lock()

        state = lock_state.exit_guardian_lock()

        self.assertEqual(
            state.mode,
            lock_state.LockMode.UNLOCKED,
        )

        data = json.loads(
            self.state_path.read_text(
                encoding="utf-8",
            )
        )

        self.assertEqual(
            data,
            {
                "mode": "unlocked",
                "version": 1,
            },
        )

    def test_guardian_lock_survives_restart(
        self,
    ) -> None:
        lock_state.initialize_lock_state(
            self.state_path,
        )

        lock_state.enter_guardian_lock()

        lock_state._current_state = None
        lock_state._current_path = None

        state = lock_state.initialize_lock_state(
            self.state_path,
        )

        self.assertEqual(
            state.mode,
            lock_state.LockMode.GUARDIAN_LOCK,
        )

    def test_cannot_exit_guardian_lock_when_unlocked(
        self,
    ) -> None:
        lock_state.initialize_lock_state(
            self.state_path,
        )

        with self.assertRaises(ValueError):
            lock_state.exit_guardian_lock()


if __name__ == "__main__":
    unittest.main()
