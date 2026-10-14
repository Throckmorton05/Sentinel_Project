"""CLI regression tests. Credentials are only passed in socket requests."""

import unittest
import warnings
from unittest.mock import patch

from typer.testing import CliRunner

from sen.cli import sen
from sentinel.daemon import client


class SenCliTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.mode = "unlocked"
        self.pin_configured = False
        self.calls = []

        def request(action, params=None, **kwargs):
            self.calls.append((action, params or {}, kwargs))
            if action == "guardian.pin_status":
                return {"configured": self.pin_configured, "mode": self.mode}
            if action == "state.get":
                return {"mode": self.mode, "version": 1}
            if action.startswith("state."):
                return {"mode": "unlocked", "version": 1}
            if action.startswith("settings."):
                return {
                    "value": (params or {}).get("value", True),
                    "change": "strengthen",
                }
            if action == "dns.status":
                return {"application": {"status": "applied"}}
            if action.endswith(".list") and action.startswith("dns."):
                return {"entries": [], "total": 0, "next_offset": None}
            if action.startswith("dns."):
                return {"domain": (params or {}).get("domain"), "change": "strengthen",
                        "application": {"status": "applied"}}
            return {"status": "complete"}

        self.enterContext(patch.object(client, "request", side_effect=request))
        self.secret = self.enterContext(
            patch.object(sen.getpass, "getpass", return_value="4826")
        )

    def run_cli(self, args, **kwargs):
        result = self.runner.invoke(sen.app, args, **kwargs)
        self.assertEqual(result.exit_code, 0, result.output + repr(result.exception))
        self.assertNotIn("4826", result.output)
        return result

    def test_state_and_setting_reads_never_prompt(self):
        self.mode = "guardian_lock"
        self.assertIn("guardian_lock", self.run_cli(["state"]).output)
        self.run_cli(["setting", "get", "safe_browsing"])
        self.secret.assert_not_called()
        self.assertEqual([c[0] for c in self.calls], ["state.get", "settings.get"])

    def test_first_guardian_entry_prompts_for_new_pin_and_confirmation_only(self):
        self.secret.side_effect = ["4826", "4826"]
        self.run_cli(["guardian-lock"])
        self.assertEqual([c[0] for c in self.calls], ["guardian.pin_status", "state.enter_guardian_lock"])
        self.assertEqual(self.calls[-1][1], {"new_pin": "4826", "pin_confirmation": "4826"})
        self.assertIsNone(self.calls[-1][2]["guardian_pin"])
        self.assertEqual(self.secret.call_count, 2)
        self.secret.assert_called_with("Confirm Guardian PIN: ", echo_char="*")

    def test_saved_guardian_pin_requires_new_confirmation_from_unlocked(self):
        self.pin_configured = True
        self.run_cli(["guardian-lock"])
        self.assertEqual(self.secret.call_count, 2)
        self.assertEqual(self.calls[-1][0], "state.enter_guardian_lock")
        self.assertEqual(self.calls[-1][1], {"new_pin": "4826", "pin_confirmation": "4826"})
        self.assertIsNone(self.calls[-1][2]["guardian_pin"])

    def test_mismatched_or_cancelled_setup_sends_no_mutation(self):
        for answers in (["4826", "5678"], [EOFError], ["4826", EOFError]):
            self.calls.clear()
            self.secret.side_effect = answers
            result = self.runner.invoke(sen.app, ["guardian-lock"])
            self.assertEqual(result.exit_code, 1)
            self.assertEqual([c[0] for c in self.calls], ["guardian.pin_status"])
            self.assertNotIn("4826", result.output)

    def test_self_lock_does_not_prompt_or_attempt_guardian_setup(self):
        self.mode = "self_lock"
        result = self.runner.invoke(sen.app, ["guardian-lock"])
        self.assertEqual(result.exit_code, 1)
        self.secret.assert_not_called()
        self.assertEqual([c[0] for c in self.calls], ["guardian.pin_status"])

    def test_install_mode_matrix(self):
        for mode in ("unlocked", "self_lock", "guardian_lock"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.calls.clear()
                self.secret.reset_mock()
                self.run_cli(["install", "some-package"])
                self.assertEqual(
                    [c[0] for c in self.calls], ["state.get", "package.install"]
                )
                self.assertEqual(self.calls[-1][1], {"name": "some-package"})
                if mode == "guardian_lock":
                    self.secret.assert_called_once_with("Enter Guardian PIN: ", echo_char="*")
                    self.assertEqual(self.calls[-1][2]["guardian_pin"], "4826")
                else:
                    self.secret.assert_not_called()

    def test_sentinel_update_uses_its_own_rpc_and_masked_guardian_pin(self):
        for mode in ('unlocked', 'self_lock', 'guardian_lock'):
            with self.subTest(mode=mode):
                self.mode = mode
                self.calls.clear(); self.secret.reset_mock()
                self.run_cli(['update-sen'])
                self.assertEqual([call[0] for call in self.calls], ['state.get', 'sentinel.update'])
                self.assertEqual(self.calls[-1][2]['timeout'], 4500.0)
                self.assertEqual(self.secret.call_count, int(mode == 'guardian_lock'))


    def test_neovim_update_uses_daemon_rpc_and_guardian_pin(self):
        for mode in ("unlocked", "self_lock", "guardian_lock"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.calls.clear()
                self.secret.reset_mock()
                self.run_cli(["update", "neovim"])
                self.assertEqual([c[0] for c in self.calls], ["state.get", "package.update"])
                self.assertEqual(self.calls[-1][1], {"name": "neovim"})
                if mode == "guardian_lock":
                    self.secret.assert_called_once_with("Enter Guardian PIN: ", echo_char="*")
                else:
                    self.secret.assert_not_called()
        self.calls.clear()
        self.assertEqual(self.runner.invoke(sen.app, ["update", "git"]).exit_code, 1)
        self.assertFalse(self.calls)

    def test_reaper_update_uses_daemon_rpc_and_guardian_pin(self):
        for mode in ("unlocked", "self_lock", "guardian_lock"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.calls.clear()
                self.secret.reset_mock()
                self.run_cli(["update", "reaper"])
                self.assertEqual([c[0] for c in self.calls], ["state.get", "package.update"])
                self.assertEqual(self.calls[-1][1], {"name": "reaper"})
                if mode == "guardian_lock":
                    self.secret.assert_called_once_with("Enter Guardian PIN: ", echo_char="*")
                else:
                    self.secret.assert_not_called()

    def test_guardian_transitions_prompt_and_self_lock_confirms(self):
        self.mode = "guardian_lock"
        for command in (["guardian-lock"], ["guardian-unlock"]):
            self.run_cli(command)
            self.assertEqual(self.calls[-1][2]["guardian_pin"], "4826")
        self.calls.clear()
        self.mode = "unlocked"
        self.run_cli(["self-lock"], input="n\n")
        self.assertEqual([c[0] for c in self.calls], ["state.get"])

    def test_self_lock_denied_while_locked_without_prompt_or_mutation(self):
        for mode in ("guardian_lock", "self_lock"):
            for args in (["self-lock"], ["self-lock", "--yes"]):
                with self.subTest(mode=mode, args=args), patch.object(sen.typer, "confirm") as confirm:
                    self.mode = mode
                    self.calls.clear()
                    result = self.runner.invoke(sen.app, args)
                    self.assertEqual(result.exit_code, 1)
                    if mode == "guardian_lock":
                        self.assertIn("Exit Guardian Lock first", result.output)
                    self.assertEqual([c[0] for c in self.calls], ["state.get"])
                    self.secret.assert_not_called()
                    confirm.assert_not_called()

    def test_unlocked_self_lock_requires_confirmation_unless_yes(self):
        for args, answer in ((["self-lock"], "y\n"), (["self-lock", "--yes"], "")):
            self.calls.clear()
            self.run_cli(args, input=answer)
            self.assertEqual([c[0] for c in self.calls], ["state.get", "state.enter_self_lock"])
            self.assertEqual(self.calls[-1][1], {})
            self.assertIsNone(self.calls[-1][2]["guardian_pin"])
            self.secret.assert_not_called()

    def test_change_pin_confirmation_and_mismatch(self):
        self.secret.side_effect = ["4826", "7319", "7319"]
        result = self.run_cli(["guardian-change-pin"])
        self.assertNotIn("7319", result.output)
        self.assertEqual(self.calls[-1][0], "guardian.change_pin")
        self.assertEqual(self.calls[-1][1], {"new_pin": "7319"})
        self.assertEqual(self.calls[-1][2]["guardian_pin"], "4826")
        self.assertEqual(self.secret.call_args_list[0].kwargs, {"echo_char": "*"})
        self.assertEqual(self.secret.call_args_list[1].kwargs, {"echo_char": "*"})
        self.assertEqual(self.secret.call_args_list[2].kwargs, {"echo_char": "*"})
        self.calls.clear()
        self.secret.side_effect = ["4826", "7319", "7318"]
        result = self.runner.invoke(sen.app, ["guardian-change-pin"])
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(self.calls)
        self.assertNotIn("7319", result.output)

    def test_setting_parses_boolean_and_sends_pin(self):
        self.mode = "guardian_lock"
        self.run_cli(["setting", "set", "safe_browsing", "false"])
        self.assertIs(self.calls[-1][1]["value"], False)
        self.assertEqual(self.calls[-1][2]["guardian_pin"], "4826")

    def test_invalid_setting_value_does_not_send_request(self):
        result = self.runner.invoke(
            sen.app, ["setting", "set", "safe_browsing", "not-a-value"]
        )
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(self.calls)

    def test_bundle_prompts_once_but_attaches_pin_to_every_install(self):
        self.mode = "guardian_lock"
        self.enterContext(
            patch.object(sen, "PACKAGE_BUNDLES", {"test-bundle": ["git", "curl"]})
        )
        self.run_cli(["install", "test-bundle"], input="y\n")
        self.secret.assert_called_once()
        installs = [call for call in self.calls if call[0] == "package.install"]
        self.assertEqual(len(installs), 2)
        self.assertTrue(all(call[2]["guardian_pin"] == "4826" for call in installs))

    def test_legacy_configuration_commands_use_rpc_with_pin(self):
        self.mode = "guardian_lock"
        for args, action in (
            (["autostart-add", "firefox"], "autostart.add"),
            (["autostart-remove", "firefox"], "autostart.remove"),
            (["autologin-disable"], "autologin.disable"),
            (["auto-update-enable", "--weekly"], "updates.enable"),
            (["auto-update-disable"], "updates.disable"),
            (["auto-restart-enable"], "updates.restart_enable"),
            (["auto-restart-disable"], "updates.restart_disable"),
            (["stop-service", "test"], "service.stop"),
            (["uninstall", "git"], "package.uninstall"),
        ):
            with self.subTest(args=args):
                self.run_cli(args)
                self.assertEqual(self.calls[-1][0], action)
                self.assertEqual(self.calls[-1][2]["guardian_pin"], "4826")

    def test_pin_arguments_are_not_supported(self):
        for command in ("guardian-unlock", "guardian-change-pin", "guardian-lock"):
            result = self.runner.invoke(sen.app, [command, "--pin", "4826"])
            self.assertNotEqual(result.exit_code, 0)
        self.secret.assert_not_called()
        self.assertFalse(self.calls)

    def test_getpass_echo_fallback_and_cancellation_are_refused(self):
        def fallback(prompt, **kwargs):
            warnings.warn("No terminal", sen.getpass.GetPassWarning)
            return "4826"

        for effect in (fallback, EOFError, KeyboardInterrupt):
            self.secret.side_effect = effect
            result = self.runner.invoke(sen.app, ["guardian-unlock"])
            self.assertEqual(result.exit_code, 1)
            self.assertNotIn("4826", result.output)
            self.assertFalse(self.calls)

    def test_desktop_password_remains_hidden(self):
        self.assertEqual(sen._prompt_secret("Desktop user password: "), "4826")
        self.secret.assert_called_once_with("Desktop user password: ", echo_char=None)

    def test_state_query_failure_does_not_attempt_mutation(self):
        with patch.object(
            client, "get_lock_state", side_effect=client.SentineldError("not running")
        ):
            result = self.runner.invoke(sen.app, ["install", "git"])
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(self.calls)

    def test_dns_reads_never_prompt_and_guardian_mutations_use_masked_pin(self):
        self.mode = "guardian_lock"
        for args in (["dns", "status"], ["dns", "block", "list"], ["dns", "allow", "list"]):
            self.run_cli(args)
        self.secret.assert_not_called()
        for args in (["dns", "block", "add", "example.com"],
                     ["dns", "block", "remove", "example.com"],
                     ["dns", "allow", "add", "example.com"],
                     ["dns", "allow", "remove", "example.com"],
                     ["dns", "regenerate", "--update-sources"]):
            self.run_cli(args)
            self.assertEqual(self.calls[-1][2]["guardian_pin"], "4826")
        self.secret.assert_called_with("Enter Guardian PIN: ", echo_char="*")

    def test_pending_dns_application_is_visible_and_exits_nonzero(self):
        with patch.object(client, "add_dns_domain", return_value={
            "domain": "example.com", "change": "strengthen",
            "application": {"status": "pending", "error": "backend offline"},
        }):
            result = self.runner.invoke(sen.app, ["dns", "block", "add", "example.com"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("saved", result.output)
        self.assertIn("pending", result.output)
        self.assertNotIn("DNS rules applied", result.output)




class GuardianAppsCliTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.enterContext(patch.object(sen.daemon, 'get_lock_state', return_value={'mode': 'guardian_lock'}))
        self.pin = self.enterContext(patch.object(sen, '_prompt_secret', return_value='9876'))
        self.request = self.enterContext(patch.object(sen.daemon, 'request', return_value={'apps': []}))

    def test_guardian_writes_prompt_and_pass_pin_only_to_daemon(self):
        for args, action in [(['enable'], 'enable'), (['disable'], 'enable'),
                             (['approve', 'org.kde.kate.desktop'], 'approve'),
                             (['revoke', 'org.kde.kate.desktop'], 'revoke')]:
            self.pin.reset_mock()
            result = self.runner.invoke(sen.app, ['guardian-apps', *args])
            self.assertEqual(result.exit_code, 0, result.output)
            self.pin.assert_called_once_with('Enter Guardian PIN: ', echo_char='*')
            self.assertEqual(self.request.call_args.args[0], 'guardian.apps.' + action)
            self.assertEqual(self.request.call_args.kwargs['guardian_pin'], '9876')
            self.assertNotIn('9876', result.output)

    def test_reads_do_not_prompt_for_pin(self):
        for command in ('list', 'status'):
            result = self.runner.invoke(sen.app, ['guardian-apps', command])
            self.assertEqual(result.exit_code, 0, result.output)
        self.pin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
