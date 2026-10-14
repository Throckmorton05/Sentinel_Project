"""Tests for policy-enforced Sentinel settings operations."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from sentinel.daemon.core.lock_state import (
    LockMode,
    LockState,
    STATE_VERSION,
)
from sentinel.daemon.operations import settings_ops
from sentinel.daemon.security import policy


class SettingsOpsTests(unittest.TestCase):
    """
    Test policy enforcement around settings.toml mutations.
    """

    def setUp(
        self,
    ) -> None:
        """
        Create an isolated temporary settings.toml for every test.

        The real /etc/sentinel/settings.toml is never touched.
        """

        self.enterContext(patch("sentinel.daemon.operations.browser_ops.apply_browser_policy"))
        self.enterContext(patch("sentinel.daemon.operations.dns_ops.apply_dns"))
        self.temp_dir = tempfile.TemporaryDirectory()

        self.settings_path = Path(self.temp_dir.name) / "settings.toml"

    def tearDown(
        self,
    ) -> None:
        self.temp_dir.cleanup()

    def _write_safe_browsing(
        self,
        enabled: bool,
    ) -> None:
        """
        Write a minimal Sentinel settings file.
        """

        value = "true" if enabled else "false"

        self.settings_path.write_text(
            f"safe_browsing = {value}\n",
            encoding="utf-8",
        )

    def _read_safe_browsing(
        self,
    ) -> bool:
        """
        Read safe_browsing from the temporary settings file.
        """

        with self.settings_path.open(
            "rb",
        ) as stream:
            settings = tomllib.load(
                stream,
            )

        return settings["safe_browsing"]

    def _state(
        self,
        mode: LockMode,
    ) -> LockState:
        return LockState(
            version=STATE_VERSION,
            mode=mode,
        )

    def test_get_setting_reads_registered_setting(
        self,
    ) -> None:
        """
        Registered settings can be read through the generic interface.
        """

        self._write_safe_browsing(
            True,
        )

        with patch.object(
            policy,
            "get_lock_state",
            return_value=self._state(
                LockMode.SELF_LOCK,
            ),
        ):
            value = settings_ops.get_setting(
                "safe_browsing",
                path=self.settings_path,
            )

        self.assertTrue(
            value,
        )

    def test_unknown_setting_is_rejected(
        self,
    ) -> None:
        """
        Arbitrary settings keys must not be writable.
        """

        self._write_safe_browsing(
            True,
        )

        with self.assertRaisesRegex(
            ValueError,
            "Unknown Sentinel setting",
        ):
            settings_ops.set_setting(
                "definitely_not_a_real_setting",
                True,
                path=self.settings_path,
            )

    def test_unlocked_allows_strengthen(
        self,
    ) -> None:
        """
        Unlocked mode may enable a protection.
        """

        self._write_safe_browsing(
            False,
        )

        with patch.object(
            policy,
            "get_lock_state",
            return_value=self._state(
                LockMode.UNLOCKED,
            ),
        ):
            change = settings_ops.set_setting(
                "safe_browsing",
                True,
                path=self.settings_path,
            )

        self.assertIs(
            change,
            policy.ChangeKind.STRENGTHEN,
        )

        self.assertTrue(
            self._read_safe_browsing(),
        )

    def test_unlocked_allows_weaken(
        self,
    ) -> None:
        """
        Unlocked mode may disable a protection.
        """

        self._write_safe_browsing(
            True,
        )

        with patch.object(
            policy,
            "get_lock_state",
            return_value=self._state(
                LockMode.UNLOCKED,
            ),
        ):
            change = settings_ops.set_setting(
                "safe_browsing",
                False,
                path=self.settings_path,
            )

        self.assertIs(
            change,
            policy.ChangeKind.WEAKEN,
        )

        self.assertFalse(
            self._read_safe_browsing(),
        )

    def test_self_lock_allows_strengthen(
        self,
    ) -> None:
        """
        Self Lock may make Sentinel more restrictive.
        """

        self._write_safe_browsing(
            False,
        )

        with patch.object(
            policy,
            "get_lock_state",
            return_value=self._state(
                LockMode.SELF_LOCK,
            ),
        ):
            change = settings_ops.set_setting(
                "safe_browsing",
                True,
                path=self.settings_path,
            )

        self.assertIs(
            change,
            policy.ChangeKind.STRENGTHEN,
        )

        self.assertTrue(
            self._read_safe_browsing(),
        )

    def test_self_lock_rejects_weaken_and_preserves_file(
        self,
    ) -> None:
        """
        Self Lock must reject protection reductions.

        More importantly, the settings file must remain unchanged.
        """

        self._write_safe_browsing(
            True,
        )

        with patch.object(
            policy,
            "get_lock_state",
            return_value=self._state(
                LockMode.SELF_LOCK,
            ),
        ):
            with self.assertRaises(
                policy.PolicyDeniedError,
            ):
                settings_ops.set_setting(
                    "safe_browsing",
                    False,
                    path=self.settings_path,
                )

        #
        # The denied operation must not have modified the file.
        #
        self.assertTrue(
            self._read_safe_browsing(),
        )

    def test_self_lock_allows_maintain(
        self,
    ) -> None:
        """
        Setting a protection to its current value is harmless.
        """

        self._write_safe_browsing(
            True,
        )

        with patch.object(
            policy,
            "get_lock_state",
            return_value=self._state(
                LockMode.SELF_LOCK,
            ),
        ):
            change = settings_ops.set_setting(
                "safe_browsing",
                True,
                path=self.settings_path,
            )

        self.assertIs(
            change,
            policy.ChangeKind.MAINTAIN,
        )

        self.assertTrue(
            self._read_safe_browsing(),
        )

    def test_guardian_lock_rejects_change_without_authentication(
        self,
    ) -> None:
        """
        Guardian Lock must reject mutations without guardian auth.
        """

        self._write_safe_browsing(
            True,
        )

        with patch.object(
            policy,
            "get_lock_state",
            return_value=self._state(
                LockMode.GUARDIAN_LOCK,
            ),
        ):
            with self.assertRaises(
                policy.PolicyDeniedError,
            ):
                settings_ops.set_setting(
                    "safe_browsing",
                    False,
                    path=self.settings_path,
                )

        #
        # Denial must leave the setting unchanged.
        #
        self.assertTrue(
            self._read_safe_browsing(),
        )

    def test_guardian_lock_allows_change_with_authentication(
        self,
    ) -> None:
        """
        An authenticated guardian may change protections.
        """

        self._write_safe_browsing(
            True,
        )

        with patch.object(
            policy,
            "get_lock_state",
            return_value=self._state(
                LockMode.GUARDIAN_LOCK,
            ),
        ):
            change = settings_ops.set_setting(
                "safe_browsing",
                False,
                guardian_authenticated=True,
                path=self.settings_path,
            )

        self.assertIs(
            change,
            policy.ChangeKind.WEAKEN,
        )

        self.assertFalse(
            self._read_safe_browsing(),
        )


if __name__ == "__main__":
    unittest.main()
