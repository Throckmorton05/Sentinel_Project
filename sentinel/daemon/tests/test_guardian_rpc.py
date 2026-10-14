"""RPC integration tests for Sentinel Guardian Lock."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from sentinel.daemon.core.lock_state import (
    STATE_VERSION,
    LockMode,
    LockState,
)
from sentinel.daemon.operations import settings_ops
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.security import policy

UID = 1000
PID = 222


class GuardianRpcTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch("sentinel.daemon.operations.browser_ops.apply_browser_policy"))
        self.enterContext(patch("sentinel.daemon.operations.dns_ops.apply_dns"))
        self.guardian_state = LockState(
            version=STATE_VERSION,
            mode=LockMode.GUARDIAN_LOCK,
        )

        self.unlocked_state = LockState(
            version=STATE_VERSION,
            mode=LockMode.UNLOCKED,
        )
        self.enterContext(patch.object(request_handler, "get_lock_state", return_value=self.unlocked_state))

    def _request(
        self,
        action: str,
        params: dict | None = None,
    ) -> dict:
        return request_handler._handle_request(
            {
                "request_id": "guardian-test",
                "action": action,
                "params": params or {},
            },
            uid=UID,
            pid=PID,
        )

    def test_correct_pin_allows_setting_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings_path = Path(temp) / "settings.toml"

            settings_path.write_text(
                "safe_browsing = true\n",
                encoding="utf-8",
            )

            def set_temp_setting(
                name: str,
                value: object,
                *,
                guardian_authenticated: bool = False,
            ):
                return settings_ops.set_setting(
                    name,
                    value,
                    guardian_authenticated=guardian_authenticated,
                    path=settings_path,
                )

            with (
                patch.object(
                    request_handler,
                    "get_lock_state",
                    return_value=self.guardian_state,
                ),
                patch.object(
                    policy,
                    "get_lock_state",
                    return_value=self.guardian_state,
                ),
                patch.object(
                    request_handler,
                    "verify_guardian_pin",
                    return_value=True,
                ) as verify,
                patch.object(
                    request_handler,
                    "_set_setting",
                    side_effect=set_temp_setting,
                ),
            ):
                response = self._request(
                    "settings.set",
                    {
                        "name": "safe_browsing",
                        "value": False,
                        "guardian_pin": "1234",
                    },
                )

            self.assertTrue(
                response["ok"],
            )

            self.assertEqual(
                response["result"]["change"],
                "weaken",
            )

            verify.assert_called_once_with(
                "1234",
            )

            with settings_path.open(
                "rb",
            ) as stream:
                settings = tomllib.load(
                    stream,
                )

            self.assertFalse(
                settings["safe_browsing"],
            )

    def test_wrong_pin_rejects_setting_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings_path = Path(temp) / "settings.toml"

            settings_path.write_text(
                "safe_browsing = true\n",
                encoding="utf-8",
            )

            def set_temp_setting(
                name: str,
                value: object,
                *,
                guardian_authenticated: bool = False,
            ):
                return settings_ops.set_setting(
                    name,
                    value,
                    guardian_authenticated=guardian_authenticated,
                    path=settings_path,
                )

            with (
                patch.object(
                    request_handler,
                    "get_lock_state",
                    return_value=self.guardian_state,
                ),
                patch.object(
                    policy,
                    "get_lock_state",
                    return_value=self.guardian_state,
                ),
                patch.object(
                    request_handler,
                    "verify_guardian_pin",
                    return_value=False,
                ),
                patch.object(
                    request_handler,
                    "_set_setting",
                    side_effect=set_temp_setting,
                ),
            ):
                response = self._request(
                    "settings.set",
                    {
                        "name": "safe_browsing",
                        "value": False,
                        "guardian_pin": "9999",
                    },
                )

            self.assertFalse(
                response["ok"],
            )

            self.assertEqual(
                response["error"]["code"],
                "permission_denied",
            )

            with settings_path.open(
                "rb",
            ) as stream:
                settings = tomllib.load(
                    stream,
                )

            self.assertTrue(
                settings["safe_browsing"],
            )

    def test_missing_pin_rejects_setting_change(
        self,
    ) -> None:
        with (
            patch.object(
                request_handler,
                "get_lock_state",
                return_value=self.guardian_state,
            ),
            patch.object(
                request_handler,
                "verify_guardian_pin",
            ) as verify,
            patch.object(
                request_handler,
                "_set_setting",
                side_effect=policy.PolicyDeniedError(
                    "Guardian authentication required."
                ),
            ),
        ):
            response = self._request(
                "settings.set",
                {
                    "name": "safe_browsing",
                    "value": False,
                },
            )

        self.assertFalse(
            response["ok"],
        )

        self.assertEqual(
            response["error"]["code"],
            "permission_denied",
        )

        verify.assert_not_called()

    def test_client_cannot_spoof_guardian_authenticated(
        self,
    ) -> None:
        """
        A client-provided guardian_authenticated=true must have
        absolutely no authority.
        """

        with tempfile.TemporaryDirectory() as temp:
            settings_path = Path(temp) / "settings.toml"

            settings_path.write_text(
                "safe_browsing = true\n",
                encoding="utf-8",
            )

            def set_temp_setting(
                name: str,
                value: object,
                *,
                guardian_authenticated: bool = False,
            ):
                return settings_ops.set_setting(
                    name,
                    value,
                    guardian_authenticated=guardian_authenticated,
                    path=settings_path,
                )

            with (
                patch.object(
                    request_handler,
                    "get_lock_state",
                    return_value=self.guardian_state,
                ),
                patch.object(
                    policy,
                    "get_lock_state",
                    return_value=self.guardian_state,
                ),
                patch.object(
                    request_handler,
                    "verify_guardian_pin",
                ) as verify,
                patch.object(
                    request_handler,
                    "_set_setting",
                    side_effect=set_temp_setting,
                ),
            ):
                response = self._request(
                    "settings.set",
                    {
                        "name": "safe_browsing",
                        "value": False,
                        #
                        # Malicious client attempt.
                        #
                        "guardian_authenticated": True,
                    },
                )

            self.assertFalse(
                response["ok"],
            )

            self.assertEqual(
                response["error"]["code"],
                "permission_denied",
            )

            verify.assert_not_called()

            with settings_path.open(
                "rb",
            ) as stream:
                settings = tomllib.load(
                    stream,
                )

            self.assertTrue(
                settings["safe_browsing"],
            )

    def test_correct_pin_allows_exit_guardian_lock(
        self,
    ) -> None:
        with (
            patch.object(
                request_handler,
                "verify_guardian_pin",
                return_value=True,
            ),
            patch.object(
                request_handler,
                "exit_guardian_lock",
                return_value=self.unlocked_state,
            ) as exit_lock,
        ):
            response = self._request(
                "state.exit_guardian_lock",
                {
                    "guardian_pin": "1234",
                },
            )

        self.assertTrue(
            response["ok"],
        )

        self.assertEqual(
            response["result"]["mode"],
            "unlocked",
        )

        exit_lock.assert_called_once_with()

    def test_wrong_pin_cannot_exit_guardian_lock(
        self,
    ) -> None:
        with (
            patch.object(
                request_handler,
                "verify_guardian_pin",
                return_value=False,
            ),
            patch.object(
                request_handler,
                "exit_guardian_lock",
            ) as exit_lock,
        ):
            response = self._request(
                "state.exit_guardian_lock",
                {
                    "guardian_pin": "9999",
                },
            )

        self.assertFalse(
            response["ok"],
        )

        self.assertEqual(
            response["error"]["code"],
            "permission_denied",
        )

        exit_lock.assert_not_called()

    def test_enter_guardian_lock_rpc(
        self,
    ) -> None:
        with patch.object(request_handler, "setup_guardian_pin") as setup, patch.object(request_handler, "get_lock_state", return_value=self.unlocked_state), patch.object(
            request_handler,
            "enter_guardian_lock",
            return_value=self.guardian_state,
        ) as enter_lock:
            response = self._request(
                "state.enter_guardian_lock",
                {"new_pin": "4826", "pin_confirmation": "4826"},
            )

        self.assertTrue(
            response["ok"],
        )

        self.assertEqual(
            response["result"]["mode"],
            "guardian_lock",
        )

        enter_lock.assert_called_once_with()
        setup.assert_called_once_with("4826", "4826", replace_existing=True)


if __name__ == "__main__":
    unittest.main()
