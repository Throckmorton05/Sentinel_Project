"""Regression tests for sentineld scheduled block windows."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sentinel.daemon.core import lock_state
from sentinel.daemon.operations import block_window_ops
from sentinel.daemon.security.policy import PolicyDeniedError


UID = 1000
NOW = 1_800_000_000.0


class BlockWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.lock_path = root / "lock-state.json"
        self.windows_path = root / "block-windows.json"

        self.real_fstat = os.fstat

        def root_owned_fstat(fd: int) -> os.stat_result:
            info = self.real_fstat(fd)
            values = list(info)
            values[4] = 0
            values[5] = 0
            return os.stat_result(values)

        self.enterContext(patch.object(lock_state, "_validate_parent"))
        self.enterContext(patch.object(block_window_ops, "_validate_parent"))
        self.enterContext(patch.object(lock_state.os, "fstat", side_effect=root_owned_fstat))
        # lock_state.os and block_window_ops.os are the same imported module, so
        # one os.fstat patch covers both readers.
        self.clock = self.enterContext(patch.object(block_window_ops.time, "time", return_value=NOW))
        self.reconcile = self.enterContext(
            patch.object(
                block_window_ops,
                "_reconcile_effective_policy",
                return_value={"app_blocker": {"status": "active"}, "dns": {"status": "applied"}},
            )
        )

        lock_state._current_state = None
        lock_state._current_path = None
        block_window_ops._current_path = None
        lock_state.initialize_lock_state(self.lock_path)
        block_window_ops.initialize_block_windows(self.windows_path)

    def tearDown(self) -> None:
        lock_state._current_state = None
        lock_state._current_path = None
        block_window_ops._current_path = None
        self.temp_dir.cleanup()

    @staticmethod
    def flatpak(key: str = "opera", app_id: str = "com.opera.Opera") -> dict[str, str]:
        return {
            "key": key,
            "name": key.title(),
            "type": "flatpak",
            "flatpak_id": app_id,
        }

    def create(self, *, lock_type: str = "unlocked", **kwargs):
        return block_window_ops.create_block_window(
            name=kwargs.pop("name", "Focus period"),
            start_at=kwargs.pop("start_at", NOW - 60),
            end_at=kwargs.pop("end_at", NOW + 3600),
            timezone_name=kwargs.pop("timezone_name", "America/Chicago"),
            lock_type=lock_type,
            apps=kwargs.pop("apps", [self.flatpak()]),
            domains=kwargs.pop("domains", ["reddit.com"]),
            enabled=kwargs.pop("enabled", True),
            notes=kwargs.pop("notes", "test"),
            guardian_authenticated=kwargs.pop("guardian_authenticated", False),
            actor_uid=UID,
            **kwargs,
        )

    def test_unlocked_and_self_lock_are_creation_choices_when_not_guardian(self):
        policy = block_window_ops.get_policy_info()
        self.assertEqual(policy["system_lock_mode"], "unlocked")
        self.assertEqual(policy["creation"]["allowed_lock_types"], ["unlocked", "self_lock"])

        unlocked = self.create(lock_type="unlocked")
        self.assertEqual(unlocked["window"]["lock_type"], "unlocked")

        self.clock.return_value = NOW + 1
        self_locked = self.create(lock_type="self_lock", name="Locked focus")
        self.assertEqual(self_locked["window"]["lock_type"], "self_lock")

        with self.assertRaisesRegex(ValueError, "Guardian-locked"):
            self.create(lock_type="guardian_lock", name="Not allowed")

    def test_snap_and_steam_schedule_persists_and_only_blocks_within_window(self):
        from sentinel.app_blocker.process_monitor.policy import build_process_policy
        self.enterContext(patch('sentinel.guardian_apps.desktop.protected_path', return_value=True))
        self.enterContext(patch('sentinel.app_blocker.process_monitor.policy.resolve_executable', return_value='/usr/games/steam'))
        target = {'key': 'firefox', 'name': 'Firefox', 'type': 'snap', 'snap_instance': 'firefox'}
        steam = {'key': 'steam', 'name': 'Steam', 'type': 'native', 'executable': '/usr/games/steam'}
        result = self.create(apps=[target, steam], domains=[], start_at=NOW+60, end_at=NOW+120)
        self.assertEqual(result['window']['targets']['apps'], [target, steam])
        self.assertFalse(build_process_policy(block_window_ops.get_active_apps())[0].snap_instances)
        block_window_ops.initialize_block_windows(self.windows_path)
        self.clock.return_value = NOW+60
        self.assertEqual(build_process_policy(block_window_ops.get_active_apps())[0].snap_instances, frozenset({'firefox'}))
        self.assertEqual(build_process_policy(block_window_ops.get_active_apps())[0].steam_launchers, frozenset({'/usr/games/steam'}))
        self.clock.return_value = NOW+120
        self.assertFalse(build_process_policy(block_window_ops.get_active_apps())[0].snap_instances)
        self.assertFalse(build_process_policy(block_window_ops.get_active_apps())[0].steam_launchers)

    def test_self_locked_snap_window_cannot_remove_snap_target(self):
        from sentinel.daemon.security.policy import PolicyDeniedError
        target = {'key': 'firefox', 'name': 'Firefox', 'type': 'snap', 'snap_instance': 'firefox'}
        result = self.create(apps=[target], lock_type='self_lock')
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.update_block_window(result['window']['id'], {'apps': []})

    def test_guardian_mode_forces_guardian_lock_and_requires_authentication(self):
        lock_state.enter_guardian_lock()

        with self.assertRaises(PolicyDeniedError):
            self.create(lock_type="unlocked")

        created = self.create(lock_type="unlocked", guardian_authenticated=True)
        self.assertEqual(created["window"]["lock_type"], "guardian_lock")
        self.assertTrue(created["window"]["permissions"]["guardian_pin_required"])

    def test_self_locked_machine_can_still_delete_an_unlocked_window(self):
        created = self.create(lock_type="unlocked")
        lock_state.enter_self_lock()

        result = block_window_ops.delete_block_window(created["window"]["id"])
        self.assertTrue(result["deleted"])
        self.assertEqual(block_window_ops.list_block_windows()["total"], 0)

    def test_self_locked_window_can_strengthen_but_not_weaken_before_end(self):
        created = self.create(lock_type="self_lock")
        window_id = created["window"]["id"]

        strengthened = block_window_ops.update_block_window(
            window_id,
            {
                "end_at": NOW + 7200,
                "domains": ["reddit.com", "example.com"],
            },
        )
        self.assertEqual(strengthened["change"], "strengthen")
        self.assertEqual(strengthened["window"]["targets"]["domain_count"], 2)

        with self.assertRaises(PolicyDeniedError):
            block_window_ops.update_block_window(
                window_id,
                {"domains": ["reddit.com"]},
            )

        with self.assertRaises(PolicyDeniedError):
            block_window_ops.delete_block_window(window_id)

    def test_self_lock_commitment_is_removable_after_end(self):
        created = self.create(lock_type="self_lock", end_at=NOW + 10)
        self.clock.return_value = NOW + 20

        result = block_window_ops.delete_block_window(created["window"]["id"])
        self.assertTrue(result["deleted"])

    def test_effective_policy_contains_only_active_windows(self):
        active = self.create(lock_type="unlocked", name="Active")
        self.create(
            lock_type="unlocked",
            name="Future",
            start_at=NOW + 1000,
            end_at=NOW + 2000,
            apps=[self.flatpak("discord", "com.discordapp.Discord")],
            domains=["example.org"],
        )

        effective = block_window_ops.get_effective_policy()
        self.assertEqual(effective["active_window_ids"], [active["window"]["id"]])
        self.assertEqual(effective["domains"], ["reddit.com"])
        self.assertEqual(effective["app_count"], 1)
        self.assertEqual(effective["apps"][0]["flatpak_id"], "com.opera.Opera")
        self.assertEqual(effective["next_transition_at"], NOW + 1000)

    def test_calendar_list_has_rich_metadata_and_range_filtering(self):
        created = self.create(lock_type="unlocked")
        listed = block_window_ops.list_block_windows(
            range_start=NOW - 120,
            range_end=NOW + 120,
            include_targets=False,
        )
        self.assertEqual(listed["total"], 1)
        window = listed["windows"][0]
        self.assertEqual(window["id"], created["window"]["id"])
        self.assertEqual(window["state"], "active")
        self.assertTrue(window["active"])
        self.assertEqual(window["calendar"]["timezone"], "America/Chicago")
        self.assertIn("start_local", window)
        self.assertIn("end_local", window)
        self.assertFalse(window["targets"]["included"])
        self.assertNotIn("apps", window["targets"])
        self.assertEqual(window["targets"]["total_count"], 2)
        self.assertIn("permissions", window)

        outside = block_window_ops.list_block_windows(
            range_start=NOW + 10_000,
            range_end=NOW + 20_000,
        )
        self.assertEqual(outside["total"], 0)

    def test_guardian_owned_window_stays_guardian_owned_after_machine_unlock(self):
        lock_state.enter_guardian_lock()
        created = self.create(lock_type="unlocked", guardian_authenticated=True)
        lock_state.exit_guardian_lock()

        with self.assertRaises(PolicyDeniedError):
            block_window_ops.update_block_window(
                created["window"]["id"],
                {"name": "Renamed"},
            )

        updated = block_window_ops.update_block_window(
            created["window"]["id"],
            {"name": "Renamed"},
            guardian_authenticated=True,
        )
        self.assertEqual(updated["window"]["name"], "Renamed")


    def screen_window(self, **kwargs):
        return self.create(block_type='screen_time', apps=[], domains=[], **kwargs)['window']

    def test_screen_time_needs_no_targets_and_rejects_mixed_or_unknown_types(self):
        window = self.screen_window()
        self.assertTrue(block_window_ops.get_screen_time_status()['blocking'])
        self.assertEqual(window['targets']['total_count'], 0)
        for changes in ({'block_type': 'bad'}, {'block_type': 'screen_time'}, {'recurrence': 'yearly'}):
            with self.assertRaises(ValueError):
                self.create(**changes)

    def test_self_extension_is_persistent_once_and_allows_only_its_schedule_disable(self):
        window = self.screen_window(lock_type='self_lock')
        normal = self.create(lock_type='self_lock')['window']
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.update_block_window(window['id'], {'enabled': False})
        block_window_ops.bypass_screen_time(window['id'])
        self.assertFalse(block_window_ops.get_screen_time_status()['blocking'])
        self.assertTrue(block_window_ops.get_block_window(window['id'])['window']['permissions']['screen_time_disable_grace'])
        block_window_ops.initialize_block_windows(self.windows_path)
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.bypass_screen_time(window['id'])
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.update_block_window(window['id'], {'end_at': NOW+50})
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.delete_block_window(normal['id'])
        block_window_ops.update_block_window(window['id'], {'enabled': False})
        self.clock.return_value = NOW+121
        self.assertFalse(block_window_ops.get_screen_time_status()['blocking'])

    def test_extension_expires_at_exact_boundary_without_renewal(self):
        window = self.screen_window(lock_type='self_lock')
        block_window_ops.bypass_screen_time(window['id'])
        self.clock.return_value = NOW+119.9
        self.assertFalse(block_window_ops.get_screen_time_status()['blocking'])
        self.clock.return_value = NOW+120
        self.assertTrue(block_window_ops.get_screen_time_status()['blocking'])
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.update_block_window(window['id'], {'enabled': False})
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.bypass_screen_time(window['id'])

    def test_delete_in_grace_and_full_portal_disable_payload(self):
        window = self.screen_window(lock_type='self_lock')
        block_window_ops.bypass_screen_time(window['id'])
        payload = {key: window[key] for key in ('name', 'notes', 'lock_type', 'start_at', 'end_at', 'timezone', 'block_type', 'recurrence')}
        payload.update(enabled=False, apps=[], domains=[])
        block_window_ops.update_block_window(window['id'], payload)
        other = self.screen_window(lock_type='self_lock')
        block_window_ops.bypass_screen_time(other['id'])
        self.assertTrue(block_window_ops.delete_block_window(other['id'])['deleted'])

    def test_guardian_bypass_rejects_no_auth_and_preserves_next_repeat_after_restart(self):
        lock_state.enter_guardian_lock()
        window = self.screen_window(guardian_authenticated=True, recurrence='daily')
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.bypass_screen_time(window['id'])
        block_window_ops.bypass_screen_time(window['id'], guardian_authenticated=True)
        block_window_ops.initialize_block_windows(self.windows_path)
        self.assertFalse(block_window_ops.get_screen_time_status()['blocking'])
        self.clock.return_value = NOW + 86400
        self.assertTrue(block_window_ops.get_screen_time_status()['blocking'])
        lock_state.exit_guardian_lock()
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.bypass_screen_time(window['id'])

    def test_overlapping_blocks_release_independently(self):
        first, second = self.screen_window(), self.screen_window()
        block_window_ops.bypass_screen_time(first['id'])
        self.assertTrue(block_window_ops.get_screen_time_status()['blocking'])
        block_window_ops.bypass_screen_time(second['id'])
        self.assertFalse(block_window_ops.get_screen_time_status()['blocking'])

    def test_guardian_machine_overrides_a_self_lock_grace(self):
        window = self.screen_window(lock_type='self_lock')
        block_window_ops.bypass_screen_time(window['id'])
        lock_state.enter_guardian_lock()
        self.assertTrue(block_window_ops.get_screen_time_status()['blocking'])
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.update_block_window(window['id'], {'enabled': False})

    def test_normal_daily_apps_and_dns_recur_and_remain_locked_between_occurrences(self):
        window = self.create(recurrence='daily', lock_type='self_lock')['window']
        self.clock.return_value = NOW+4000
        self.assertFalse(block_window_ops.get_active_domains())
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.delete_block_window(window['id'])
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.update_block_window(window['id'], {'recurrence': 'weekly'})
        with self.assertRaises(PolicyDeniedError):
            block_window_ops.update_block_window(window['id'], {'start_at': NOW-120})
        self.clock.return_value = NOW+86400
        self.assertEqual(block_window_ops.get_active_domains(), frozenset({'reddit.com'}))
        self.assertTrue(block_window_ops.get_active_apps())
        self.assertEqual(block_window_ops.get_block_window(window['id'])['window']['state'], 'active')

    def test_recurring_calendar_expands_occurrences_and_get_retains_anchor(self):
        window = self.create(recurrence='daily')['window']
        page = block_window_ops.list_block_windows(range_start=NOW-60, range_end=NOW+3*86400-60)
        self.assertEqual(page['total'], 3)
        self.assertEqual(len({item['occurrence_id'] for item in page['windows']}), 3)
        self.clock.return_value = NOW+86400
        self.assertEqual(block_window_ops.get_block_window(window['id'])['window']['start_at'], NOW-60)
        for params in ({'range_start': NOW, 'range_end': NOW+367*86400},):
            with self.assertRaises(ValueError):
                block_window_ops.list_block_windows(**params)

    def test_recurring_calendar_pagination_fits_transport_with_long_unicode_notes(self):
        from sentinel.daemon.rpc.protocol import encode_message
        self.create(recurrence='daily', notes='界'*2000)
        offset, count = 0, 0
        while offset is not None:
            result = block_window_ops.list_block_windows(offset=offset, limit=25,
                range_start=NOW-60, range_end=NOW+30*86400-60)
            encode_message(result)
            count += len(result['windows'])
            offset = result['next_offset']
        self.assertEqual(count, 30)

    def test_legacy_state_without_new_fields_loads_and_enforces(self):
        import json
        self.create()
        data = json.loads(self.windows_path.read_text())
        for window in data['windows']:
            for key in ('block_type', 'recurrence', 'screen_state'):
                window.pop(key)
        self.windows_path.write_text(json.dumps(data))
        block_window_ops.initialize_block_windows(self.windows_path)
        self.assertEqual(block_window_ops.get_active_domains(), frozenset({'reddit.com'}))
        self.assertFalse(block_window_ops.get_screen_time_status()['blocking'])

    def test_screen_status_is_bounded_without_releasing_hidden_blocks(self):
        from sentinel.daemon.rpc.protocol import encode_message
        for _ in range(30):
            self.screen_window(name='界'*120)
        status = block_window_ops.get_screen_time_status()
        self.assertTrue(status['windows_truncated'])
        self.assertEqual(status['window_count'], 30)
        encode_message(status)
        for window in status['windows']:
            block_window_ops.bypass_screen_time(window['id'])
        self.assertTrue(block_window_ops.get_screen_time_status()['blocking'])


if __name__ == "__main__":
    unittest.main()
