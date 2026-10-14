"""Tests for Sentinel's central authorization policy."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sentinel.daemon.core.lock_state import (
    LockMode,
    LockState,
    STATE_VERSION,
)
from sentinel.daemon.security import policy


class PolicyClassificationTests(unittest.TestCase):
    """
    Test classification of requested protection changes.
    """

    def test_boolean_false_to_true_strengthens(
        self,
    ) -> None:
        change = policy.classify_boolean_change(
            old=False,
            new=True,
        )

        self.assertIs(
            change,
            policy.ChangeKind.STRENGTHEN,
        )

    def test_boolean_true_to_false_weakens(
        self,
    ) -> None:
        change = policy.classify_boolean_change(
            old=True,
            new=False,
        )

        self.assertIs(
            change,
            policy.ChangeKind.WEAKEN,
        )

    def test_boolean_same_value_maintains(
        self,
    ) -> None:
        for value in (
            False,
            True,
        ):
            with self.subTest(value=value):
                change = policy.classify_boolean_change(
                    old=value,
                    new=value,
                )

                self.assertIs(
                    change,
                    policy.ChangeKind.MAINTAIN,
                )

    def test_ranked_increase_strengthens(
        self,
    ) -> None:
        change = policy.classify_ranked_change(
            old=1,
            new=2,
        )

        self.assertIs(
            change,
            policy.ChangeKind.STRENGTHEN,
        )

    def test_ranked_decrease_weakens(
        self,
    ) -> None:
        change = policy.classify_ranked_change(
            old=2,
            new=1,
        )

        self.assertIs(
            change,
            policy.ChangeKind.WEAKEN,
        )

    def test_ranked_same_value_maintains(
        self,
    ) -> None:
        change = policy.classify_ranked_change(
            old=2,
            new=2,
        )

        self.assertIs(
            change,
            policy.ChangeKind.MAINTAIN,
        )


class PolicyAuthorizationTests(unittest.TestCase):
    """
    Test what each Sentinel lock mode permits.
    """

    def _require(
        self,
        mode: LockMode,
        change: policy.ChangeKind,
        *,
        guardian_authenticated: bool = False,
    ) -> None:
        """
        Run require_policy() as though Sentinel were in `mode`.

        This mocks only get_lock_state(), keeping these tests focused
        specifically on authorization policy.
        """

        state = LockState(
            version=STATE_VERSION,
            mode=mode,
        )

        with patch.object(
            policy,
            "get_lock_state",
            return_value=state,
        ):
            policy.require_policy(
                "test.action",
                change,
                guardian_authenticated=guardian_authenticated,
            )

    def test_unlocked_allows_every_change_kind(
        self,
    ) -> None:
        for change in policy.ChangeKind:
            with self.subTest(change=change):
                self._require(
                    LockMode.UNLOCKED,
                    change,
                )

    def test_self_lock_allows_read(
        self,
    ) -> None:
        self._require(
            LockMode.SELF_LOCK,
            policy.ChangeKind.READ,
        )

    def test_self_lock_allows_maintain(
        self,
    ) -> None:
        self._require(
            LockMode.SELF_LOCK,
            policy.ChangeKind.MAINTAIN,
        )

    def test_self_lock_allows_strengthen(
        self,
    ) -> None:
        self._require(
            LockMode.SELF_LOCK,
            policy.ChangeKind.STRENGTHEN,
        )

    def test_self_lock_rejects_weaken(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            policy.PolicyDeniedError,
            "would weaken Sentinel protection",
        ):
            self._require(
                LockMode.SELF_LOCK,
                policy.ChangeKind.WEAKEN,
            )

    def test_guardian_lock_allows_read_without_authentication(
        self,
    ) -> None:
        self._require(
            LockMode.GUARDIAN_LOCK,
            policy.ChangeKind.READ,
        )

    def test_guardian_lock_rejects_mutations_without_authentication(
        self,
    ) -> None:
        mutation_kinds = (
            policy.ChangeKind.MAINTAIN,
            policy.ChangeKind.STRENGTHEN,
            policy.ChangeKind.WEAKEN,
        )

        for change in mutation_kinds:
            with self.subTest(change=change):
                with self.assertRaisesRegex(
                    policy.PolicyDeniedError,
                    "requires guardian authentication",
                ):
                    self._require(
                        LockMode.GUARDIAN_LOCK,
                        change,
                    )

    def test_guardian_lock_allows_mutations_with_authentication(
        self,
    ) -> None:
        mutation_kinds = (
            policy.ChangeKind.MAINTAIN,
            policy.ChangeKind.STRENGTHEN,
            policy.ChangeKind.WEAKEN,
        )

        for change in mutation_kinds:
            with self.subTest(change=change):
                self._require(
                    LockMode.GUARDIAN_LOCK,
                    change,
                    guardian_authenticated=True,
                )

    def test_unknown_lock_mode_fails_closed(
        self,
    ) -> None:
        """
        Defensive test: an unexpected state must never default to allow.
        """

        invalid_state = SimpleNamespace(
            mode=object(),
        )

        with patch.object(
            policy,
            "get_lock_state",
            return_value=invalid_state,
        ):
            with self.assertRaisesRegex(
                policy.PolicyDeniedError,
                "unknown lock state",
            ):
                policy.require_policy(
                    "test.action",
                    policy.ChangeKind.READ,
                )


if __name__ == "__main__":
    unittest.main()
