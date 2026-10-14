"""CLI-to-RPC coverage for scheduling, target replacement, and PIN handling."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from sen.cli import sen
from sentinel.daemon import client


class SenBlockWindowTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.mode = "unlocked"
        self.window_lock = "unlocked"
        self.calls = []
        self.enforcement = {"app_blocker": {"status": "active"}, "dns": {"status": "applied"}}
        self.secret = self.enterContext(patch.object(sen.getpass, "getpass", return_value="4826"))
        self.enterContext(patch.object(client, "request", side_effect=self.request))

    def window(self, window_id="test-id"):
        return {"id": window_id, "name": "Focus", "state": "scheduled",
                "lock_type": self.window_lock, "start_local": "2026-10-07T10:00:00-05:00",
                "end_local": "2026-10-07T11:00:00-05:00",
                "targets": {"apps": [], "domains": ["youtube.com"]},
                "permissions": {"guardian_pin_required": self.mode == "guardian_lock"
                                or self.window_lock == "guardian_lock"}}

    def request(self, action, params=None, **kwargs):
        params = params or {}
        self.calls.append((action, params, kwargs))
        if action == "state.get":
            return {"mode": self.mode}
        if action == "blockwindow.policy":
            return {"system_lock_mode": self.mode}
        if action == "blockwindow.effective":
            return {"active_window_ids": [], "domains": [], "apps": []}
        if action == "blockwindow.list":
            offset = params["offset"]
            return {"windows": [self.window(str(offset))], "total": 2, "offset": offset,
                    "limit": params["limit"], "next_offset": 1 if offset == 0 else None}
        if action == "blockwindow.get":
            return {"window": self.window(params["id"])}
        result = {"change": "strengthen", "enforcement": self.enforcement}
        if action == "blockwindow.delete":
            result.update(deleted=True, id=params["id"])
        else:
            result["window"] = self.window()
        return result

    def run_cli(self, *args):
        result = self.runner.invoke(sen.app, ["block-window", *args])
        self.assertEqual(result.exit_code, 0, result.output + repr(result.exception))
        self.assertNotIn("4826", result.output)
        return result

    def test_all_read_operations_are_pin_free_in_guardian_mode(self):
        self.mode = "guardian_lock"
        for args in (("policy",), ("effective",), ("get", "test-id"), ("list",)):
            self.run_cli(*args)
        self.secret.assert_not_called()
        self.assertEqual([call[0] for call in self.calls],
                         ["blockwindow.policy", "blockwindow.effective", "blockwindow.get", "blockwindow.list"])

    def test_create_screen_time_with_weekly_repeat_without_targets(self):
        self.run_cli('create', 'Bedtime', '--start', '2026-10-09T21:00', '--end', '2026-10-09T22:00',
                     '--timezone', 'America/Chicago', '--screen-time', '--repeat', 'weekly')
        _, params, _ = self.calls[-1]
        self.assertEqual(params['block_type'], 'screen_time')
        self.assertEqual(params['recurrence'], 'weekly')
        self.assertEqual(params['apps'], [])
        self.secret.assert_not_called()

    def test_screen_time_bypass_prompts_for_guardian_owned_window(self):
        self.window_lock = 'guardian_lock'
        self.run_cli('bypass', 'test-id')
        action, params, kwargs = self.calls[-1]
        self.assertEqual(action, 'screenblock.bypass')
        self.assertEqual(params, {'id': 'test-id'})
        self.assertEqual(kwargs['guardian_pin'], '4826')

    def test_create_payload_targets_times_and_guardian_pin(self):
        self.mode = "guardian_lock"
        self.run_cli("create", "Study", "--start", "2026-10-07T10:00:00",
                     "--end", "2026-10-08T10:00:00", "--timezone", "America/Chicago",
                     "--lock", "self_lock", "--domain", "youtube.com", "--domain", "reddit.com",
                     "--native", "kate=/usr/bin/kate", "--flatpak", "game=com.example.Game",
                     "--disabled", "--notes", "Tomorrow")
        action, params, kwargs = self.calls[-1]
        self.assertEqual(action, "blockwindow.create")
        self.assertEqual(params["apps"], [
            {"key": "kate", "name": "kate", "type": "native", "executable": "/usr/bin/kate"},
            {"key": "game", "name": "game", "type": "flatpak", "flatpak_id": "com.example.Game"}])
        self.assertEqual(params["domains"], ["youtube.com", "reddit.com"])
        self.assertEqual(params["timezone"], "America/Chicago")
        self.assertEqual(params["lock_type"], "self_lock")
        self.assertEqual(params["notes"], "Tomorrow")
        self.assertFalse(params["enabled"])
        self.assertEqual(kwargs["guardian_pin"], "4826")
        self.secret.assert_called_once_with("Enter Guardian PIN: ", echo_char="*")

    def test_create_defaults_and_unix_seconds(self):
        self.run_cli("create", "Quick test", "--start", "1800000000", "--end", "1800000120.5",
                     "--domain", "youtube.com")
        params = self.calls[-1][1]
        self.assertEqual(params["start_at"], 1800000000.0)
        self.assertEqual(params["end_at"], 1800000120.5)
        self.assertEqual(params["lock_type"], "unlocked")
        self.assertEqual(params["timezone"], "UTC")
        self.assertTrue(params["enabled"])
        self.secret.assert_not_called()

    def test_update_sends_only_changed_fields_including_false_and_empty_notes(self):
        self.run_cli("update", "test-id", "--name", "New", "--disabled", "--notes", "",
                     "--start", "1800000000", "--end", "1800000600",
                     "--timezone", "America/Chicago", "--lock", "self_lock")
        self.assertEqual(self.calls[-1][1], {"id": "test-id", "name": "New", "enabled": False,
                         "notes": "", "start_at": 1800000000.0, "end_at": 1800000600.0,
                         "timezone": "America/Chicago", "lock_type": "self_lock"})

    def test_target_updates_replace_supplied_arrays_and_preserve_omitted_arrays(self):
        self.run_cli("update", "test-id", "--domain", "example.com")
        self.assertEqual(self.calls[-1][1], {"id": "test-id", "domains": ["example.com"]})
        self.run_cli("update", "test-id", "--clear-domains", "--native", "kate=kate")
        self.assertEqual(self.calls[-1][1]["domains"], [])
        self.assertEqual(self.calls[-1][1]["apps"][0]["executable"], "kate")
        self.run_cli("update", "test-id", "--clear-apps")
        self.assertEqual(self.calls[-1][1], {"id": "test-id", "apps": []})

    def test_targets_file_preserves_app_display_names(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "targets.json"
            targets = {"apps": [{"key": "kate", "name": "Kate Editor", "type": "native",
                                 "executable": "/usr/bin/kate"}], "domains": ["youtube.com"]}
            path.write_text(json.dumps(targets))
            self.run_cli("update", "test-id", "--targets-file", str(path), "--domain", "reddit.com")
        self.assertEqual(self.calls[-1][1]["apps"], targets["apps"])
        self.assertEqual(self.calls[-1][1]["domains"], ["youtube.com", "reddit.com"])

    def test_guardian_owned_windows_prompt_even_when_machine_is_unlocked(self):
        for mode in ("unlocked", "self_lock", "guardian_lock"):
            for args in (("update", "test-id", "--name", "New"), ("enable", "test-id"),
                         ("disable", "test-id"), ("delete", "test-id")):
                with self.subTest(mode=mode, args=args):
                    self.mode = mode
                    self.window_lock = "guardian_lock"
                    self.secret.reset_mock()
                    self.run_cli(*args)
                    self.assertEqual(self.calls[-1][2]["guardian_pin"], "4826")
                    self.secret.assert_called_once_with("Enter Guardian PIN: ", echo_char="*")

    def test_enable_disable_delete_send_expected_operations_without_pin(self):
        for command, action, fields in (
            ("enable", "blockwindow.update", {"enabled": True}),
            ("disable", "blockwindow.update", {"enabled": False}),
            ("delete", "blockwindow.delete", {}),
        ):
            self.run_cli(command, "test-id")
            self.assertEqual(self.calls[-1][0], action)
            self.assertEqual(self.calls[-1][1], {"id": "test-id", **fields})
        self.secret.assert_not_called()

    def test_paginated_target_listing_and_json(self):
        result = self.run_cli("list", "--all", "--include-targets", "--exclude-ended",
                              "--range-start", "1800000000", "--range-end", "2026-10-08T10:00:00Z", "--json")
        self.assertEqual(len(json.loads(result.output)["windows"]), 2)
        self.assertIsNone(json.loads(result.output)["next_offset"])
        self.assertEqual([call[1]["offset"] for call in self.calls], [0, 1])
        for _, params, _ in self.calls:
            self.assertEqual(params["limit"], 1)
            self.assertTrue(params["include_targets"])
            self.assertFalse(params["include_ended"])
            self.assertEqual(params["range_start"], 1800000000.0)

    def test_invalid_options_do_not_make_requests(self):
        for args in (("update", "test-id"), ("update", "test-id", "--native", "bad"),
                     ("update", "test-id", "--clear-apps", "--native", "kate=kate"),
                     ("update", "test-id", "--clear-domains", "--domain", "youtube.com"),
                     ("list", "--include-targets", "--limit", "20"), ("list", "--limit", "26"),
                     ("list", "--offset", "-1"), ("delete", "test-id", "--pin", "4826")):
            with self.subTest(args=args):
                result = self.runner.invoke(sen.app, ["block-window", *args])
                self.assertNotEqual(result.exit_code, 0)
                self.assertFalse(self.calls)
        self.secret.assert_not_called()

    def test_pending_enforcement_reports_saved_policy_and_exits_nonzero(self):
        self.enforcement["app_blocker"] = {"status": "pending", "unresolved": {"kate": "Not installed"}}
        self.enforcement["dns"] = {"status": "pending", "error": "reload failed"}
        result = self.runner.invoke(sen.app, ["block-window", "disable", "test-id", "--json"])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(json.loads(result.stdout)["window"]["id"], "test-id")
        self.assertIn("Schedule saved", result.stderr)
        self.assertIn("Not installed", result.stderr)
        self.assertIn("reload failed", result.stderr)

    def test_daemon_denial_and_failed_reads_stop_cleanly(self):
        for function in ("get_block_window", "delete_block_window"):
            with patch.object(client, function, side_effect=client.SentineldError("Policy denied")):
                result = self.runner.invoke(sen.app, ["block-window", "delete", "test-id"])
                self.assertEqual(result.exit_code, 1)
                self.assertIn("Policy denied", result.output)
        self.secret.assert_not_called()


if __name__ == "__main__":
    unittest.main()
