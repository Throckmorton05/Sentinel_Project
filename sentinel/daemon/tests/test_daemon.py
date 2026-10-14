"""Unprivileged regression tests: no live logind, KWin, signals or package calls."""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


import tomllib

from sentinel.daemon import server
from sentinel.daemon.core import config, session
from sentinel.daemon.rpc import request_handler
from sentinel.daemon.supervision import readiness, screen_monitor
from sentinel.daemon.core.constants import MONITOR_RESTART_GRACE
from sentinel.daemon.supervision.state import (
    ScreenMonitorWatchdogState,
    _monitor_state,
    _stop_event,
)

from sentinel.daemon.core.lock_state import (
    LockMode,
    LockState,
    STATE_VERSION,
)
from sentinel.daemon.operations import settings_ops
from sentinel.daemon.security import policy


from sentinel.daemon.security.policy import (
    ChangeKind,
    PolicyDeniedError,
)


UID = 1000
PID = 222


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        fresh = ScreenMonitorWatchdogState()
        for field in dataclasses.fields(fresh):
            setattr(_monitor_state, field.name, getattr(fresh, field.name))
        _stop_event.clear()
        self.clock = self.enterContext(
            patch.object(screen_monitor.time, "monotonic", return_value=100.0)
        )
        self.session = self.enterContext(
            patch.object(screen_monitor, "_get_graphical_session_id", return_value="2")
        )
        self.ready = self.enterContext(
            patch.object(screen_monitor, "_wayland_session_ready", return_value=True)
        )
        self.apis_ready = self.enterContext(
            patch.object(screen_monitor, "_inference_apis_ready", return_value=True)
        )
        self.matches = self.enterContext(
            patch.object(screen_monitor, "_monitor_process_matches", return_value=True)
        )
        self.launch = self.enterContext(
            patch.object(screen_monitor, "_launch_screen_monitor")
        )
        self.terminate = self.enterContext(
            patch.object(screen_monitor, "_terminate_screen_monitor")
        )
        self.terminate_child = self.enterContext(
            patch.object(screen_monitor, "_terminate_monitor_child")
        )
        self.terminate_blocker = self.enterContext(
            patch.object(screen_monitor, "_terminate_tamper_blocker")
        )
        self.blackout = self.enterContext(
            patch.object(screen_monitor, "_enter_tamper_cooldown")
        )
        self.maintain = self.enterContext(
            patch.object(screen_monitor, "_maintain_tamper_cooldown")
        )
        screen_monitor.initialize_screen_monitor(UID)

    def test_boot_waits_then_login_launches_without_prior_registration(self):
        self.session.return_value = None
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_not_called()
        self.assertTrue(_monitor_state.enabled)
        self.assertEqual(_monitor_state.uid, UID)
        self.session.return_value = "2"
        self.ready.return_value = False
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_not_called()
        self.ready.return_value = True
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_called_once_with(UID)
        self.assertEqual(len(_monitor_state.failures), 0)
        screen_monitor._register_screen_monitor(UID, PID)
        screen_monitor._screen_monitor_heartbeat(UID, PID)
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_called_once_with(UID)

    def test_initial_daemon_adoption_grace_does_not_count_as_tampering(self):
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_not_called()
        self.clock.return_value = 107
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_called_once_with(UID)
        self.assertFalse(_monitor_state.failures)

    def test_daemon_restart_adopts_existing_monitor(self):
        screen_monitor._register_screen_monitor(UID, PID)
        self.clock.return_value = 110
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_not_called()
        self.assertEqual(_monitor_state.pid, PID)

    def test_model_loading_delays_first_launch_without_tamper_penalty(self):
        _monitor_state.awaiting_registration_until = None
        self.apis_ready.return_value = False
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_not_called()
        self.assertFalse(_monitor_state.failures)
        self.apis_ready.return_value = True
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_called_once_with(UID)

    def test_first_other_user_cannot_choose_the_protected_account(self):
        with self.assertRaisesRegex(ValueError, "different user"):
            screen_monitor._register_screen_monitor(1001, PID)
        self.assertEqual(_monitor_state.uid, UID)
        self.assertIsNone(_monitor_state.pid)

    def test_registration_requires_bootstrap_and_expected_process(self):
        _monitor_state.enabled = False
        with self.assertRaisesRegex(ValueError, "configured"):
            screen_monitor._register_screen_monitor(UID, PID)
        _monitor_state.enabled = True
        self.matches.return_value = False
        with self.assertRaisesRegex(ValueError, "registration"):
            screen_monitor._register_screen_monitor(UID, PID)

    def test_duplicate_monitor_and_foreign_heartbeat_rejected(self):
        screen_monitor._register_screen_monitor(UID, PID)
        with self.assertRaisesRegex(ValueError, "already registered"):
            screen_monitor._register_screen_monitor(UID, PID + 1)
        with self.assertRaisesRegex(ValueError, "UID"):
            screen_monitor._screen_monitor_heartbeat(1001, PID)
        with self.assertRaisesRegex(ValueError, "PID"):
            screen_monitor._screen_monitor_heartbeat(UID, PID + 1)

    def test_missing_monitor_restarts_and_three_failures_trigger_blackout(self):
        _monitor_state.awaiting_registration_until = None
        for index in range(3):
            screen_monitor._register_screen_monitor(UID, PID + index)
            self.matches.return_value = False
            screen_monitor._screen_monitor_watchdog_tick()
            self.matches.return_value = True
        self.assertEqual(self.launch.call_count, 3)
        self.blackout.assert_called_once_with(UID)

    def test_heartbeat_timeout_restarts(self):
        screen_monitor._register_screen_monitor(UID, PID)
        self.clock.return_value = 116
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_called_once_with(UID)
        self.terminate.assert_called_once_with(PID, UID)
        self.assertEqual(len(_monitor_state.failures), 1)

    def test_healthy_monitor_is_left_running(self):
        screen_monitor._register_screen_monitor(UID, PID)
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_not_called()
        self.terminate.assert_not_called()

    def test_failed_launch_retries_only_after_registration_grace(self):
        _monitor_state.awaiting_registration_until = None
        self.launch.side_effect = RuntimeError("missing launcher")
        screen_monitor._screen_monitor_watchdog_tick()
        self.assertTrue(_monitor_state.launch_attempted)
        self.clock.return_value = 120
        screen_monitor._screen_monitor_watchdog_tick()
        self.assertEqual(self.launch.call_count, 1)
        self.clock.return_value = 100 + MONITOR_RESTART_GRACE
        screen_monitor._screen_monitor_watchdog_tick()
        self.assertEqual(self.launch.call_count, 2)
        self.assertEqual(len(_monitor_state.failures), 1)

    def test_logout_clears_cooldown_and_login_relaunches(self):
        screen_monitor._register_screen_monitor(UID, PID)
        _monitor_state.session_id = "2"
        _monitor_state.failures.append(90)
        _monitor_state.tamper_cooldown_until = 130
        _monitor_state.tamper_blocker_pid = 333
        _monitor_state.overview_was_loaded = True
        self.session.return_value = None
        screen_monitor._screen_monitor_watchdog_tick()
        self.assertEqual(_monitor_state.uid, UID)
        self.assertTrue(_monitor_state.enabled)
        self.assertIsNone(_monitor_state.tamper_cooldown_until)
        self.assertFalse(_monitor_state.failures)
        self.terminate.assert_called_once_with(PID, UID)
        self.terminate_blocker.assert_called_once_with(333, UID)
        self.blackout.assert_not_called()
        self.session.return_value = "3"
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_called_once_with(UID)

    def test_new_session_does_not_reuse_old_monitor(self):
        screen_monitor._register_screen_monitor(UID, PID)
        _monitor_state.session_id = "1"
        screen_monitor._screen_monitor_watchdog_tick()
        self.terminate.assert_called_once_with(PID, UID)
        self.assertFalse(_monitor_state.failures)
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_called_once_with(UID)

    def test_registration_during_readiness_query_does_not_spawn_duplicate(self):
        _monitor_state.awaiting_registration_until = None

        def register_during_query(uid):
            screen_monitor._register_screen_monitor(uid, PID)
            return True

        self.ready.side_effect = register_during_query
        screen_monitor._screen_monitor_watchdog_tick()
        self.launch.assert_not_called()
        self.assertEqual(_monitor_state.pid, PID)

    def test_watchdog_recovers_after_query_exception(self):
        self.enterContext(
            patch.object(_stop_event, "wait", side_effect=[False, False, True])
        )
        tick = self.enterContext(
            patch.object(
                screen_monitor,
                "_screen_monitor_watchdog_tick",
                side_effect=[OSError("logind"), None],
            )
        )
        screen_monitor._screen_monitor_watchdog_loop()
        self.assertEqual(tick.call_count, 2)

    def test_cooldown_failures_do_not_extend_or_count_again(self):
        _monitor_state.tamper_cooldown_until = 130
        screen_monitor._record_monitor_failure(UID, "killed")
        self.assertEqual(_monitor_state.tamper_cooldown_until, 130)
        self.assertFalse(_monitor_state.failures)
        self.blackout.assert_not_called()


class SessionTests(unittest.TestCase):
    def result(self, text, code=0):
        return subprocess.CompletedProcess([], code, text, "")

    def test_logind_selects_user_wayland_session_over_tty_and_closing(self):
        with patch.object(
            session.subprocess,
            "run",
            side_effect=[
                self.result("1 2 3 4"),
                self.result("User=1000\nType=tty\nClass=user\nState=active\n"),
                self.result("User=1000\nType=wayland\nClass=greeter\nState=active\n"),
                self.result("User=1000\nType=wayland\nClass=user\nState=closing\n"),
                self.result("User=1000\nType=wayland\nClass=user\nState=active\n"),
            ],
        ):
            self.assertEqual(session._get_graphical_session_id(UID), "4")

    def test_wrong_uid_and_x11_are_not_sessions_to_protect(self):
        with patch.object(
            session.subprocess,
            "run",
            side_effect=[
                self.result("1 2"),
                self.result("User=1001\nType=wayland\nClass=user\nState=active\n"),
                self.result("User=1000\nType=x11\nClass=user\nState=active\n"),
            ],
        ):
            self.assertIsNone(session._get_graphical_session_id(UID))

    def test_locked_or_inactive_online_wayland_session_is_still_supervised(self):
        with patch.object(
            session.subprocess,
            "run",
            side_effect=[
                self.result("2"),
                self.result("User=1000\nType=wayland\nClass=user\nState=online\n"),
            ],
        ):
            self.assertEqual(session._get_graphical_session_id(UID), "2")

    def test_wayland_launch_gets_explicit_user_environment(self):
        with (
            patch.object(
                session,
                "_peer_user",
                return_value=SimpleNamespace(pw_name="desktop", pw_dir="/home/desktop"),
            ),
            patch.object(
                Path,
                "stat",
                return_value=SimpleNamespace(st_uid=UID, st_mode=stat.S_IFSOCK | 0o600),
            ),
            patch.object(session, "_find_wayland_display", return_value="wayland-1"),
        ):
            command = session._user_session_command(UID, "/bin/test", wayland=True)
        self.assertEqual(
            command[:6],
            ["/usr/sbin/runuser", "-u", "desktop", "--", "/usr/bin/env", "-i"],
        )
        self.assertIn("WAYLAND_DISPLAY=wayland-1", command)
        self.assertIn("QT_QPA_PLATFORM=wayland", command)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus", command)
        self.assertEqual(command[-1], "/bin/test")

    def test_kwin_not_ready_delays_launch(self):
        with (
            patch.object(session, "_user_session_command", return_value=["probe"]),
            patch.object(session.subprocess, "run", return_value=self.result("false")),
        ):
            self.assertFalse(session._wayland_session_ready(UID))
        with patch.object(
            session, "_user_session_command", side_effect=RuntimeError("no bus")
        ):
            self.assertFalse(session._wayland_session_ready(UID))


class ApiReadinessTests(unittest.TestCase):
    def response(self, data, status=200):
        response = Mock(status=status)
        response.read.return_value = data
        manager = Mock()
        manager.__enter__ = Mock(return_value=response)
        manager.__exit__ = Mock(return_value=False)
        return manager

    def test_all_four_loaded_models_are_required(self):
        with patch.object(
            readiness._health_opener,
            "open",
            side_effect=[
                self.response(b'{"status":"ok","model_loaded":true}') for _ in range(4)
            ],
        ) as probe:
            self.assertTrue(readiness._inference_apis_ready())
        self.assertEqual(
            [call.args[0] for call in probe.call_args_list],
            list(readiness.INFERENCE_HEALTH_URLS),
        )

    def test_unloaded_missing_or_malformed_api_is_not_ready(self):
        for data in (b'{"status":"ok","model_loaded":false}', b"not json", b"[]"):
            with patch.object(
                readiness._health_opener, "open", return_value=self.response(data)
            ):
                self.assertFalse(readiness._inference_apis_ready())
        with patch.object(
            readiness._health_opener, "open", side_effect=OSError("refused")
        ):
            self.assertFalse(readiness._inference_apis_ready())


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / "daemon.toml"
        self.path.write_text('protected_user = "desktop"\n')
        # Metadata is mocked so these tests can run as a non-root desktop user.
        self.parent = self.enterContext(
            patch.object(
                Path,
                "stat",
                return_value=SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755),
            )
        )
        self.info = self.enterContext(
            patch.object(
                config.os,
                "fstat",
                return_value=SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o644),
            )
        )
        self.user = self.enterContext(
            patch.object(
                config.pwd,
                "getpwnam",
                return_value=SimpleNamespace(pw_name="desktop", pw_uid=UID),
            )
        )
        self.authorized = self.enterContext(
            patch.object(config, "_uid_is_authorized", return_value=True)
        )

    def test_config_resolves_user_without_any_monitor(self):
        self.assertEqual(config.load_protected_uid(self.path), UID)

    def test_missing_config_is_an_actionable_startup_error(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "Configure protected_user"):
            config.load_protected_uid(self.path)

    def test_unprotected_file_or_parent_rejected(self):
        for owner, mode in [(1000, 0o644), (0, 0o664), (0, 0o666)]:
            self.info.return_value = SimpleNamespace(
                st_uid=owner, st_mode=stat.S_IFREG | mode
            )
            with self.assertRaisesRegex(ValueError, "protected regular file"):
                config.load_protected_uid(self.path)
        self.parent.return_value = SimpleNamespace(
            st_uid=0, st_mode=stat.S_IFDIR | 0o777
        )
        with self.assertRaisesRegex(ValueError, "not writable"):
            config.load_protected_uid(self.path)

    def test_root_unknown_and_unauthorized_user_rejected(self):
        self.user.return_value = SimpleNamespace(pw_uid=0, pw_name="root")
        with self.assertRaisesRegex(ValueError, "must not be root"):
            config.load_protected_uid(self.path)
        self.user.side_effect = KeyError()
        with self.assertRaisesRegex(ValueError, "does not exist"):
            config.load_protected_uid(self.path)
        self.user.side_effect = None
        self.user.return_value = SimpleNamespace(pw_uid=UID, pw_name="desktop")
        self.authorized.return_value = False
        with self.assertRaisesRegex(ValueError, "sentinel group"):
            config.load_protected_uid(self.path)

    def test_missing_name_malformed_toml_and_symlink_rejected(self):
        self.path.write_text("protected_user = 1000\n")
        with self.assertRaisesRegex(ValueError, "must contain"):
            config.load_protected_uid(self.path)
        self.path.write_text("protected_user = [\n")
        with self.assertRaisesRegex(ValueError, "Cannot load"):
            config.load_protected_uid(self.path)
        link = Path(self.temp) / "link.toml"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "Cannot load"):
            config.load_protected_uid(link)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("sentinel.daemon.operations.browser_ops.apply_browser_policy"))
        self.enterContext(patch("sentinel.daemon.operations.dns_ops.apply_dns"))

    def test_daemon_bootstraps_before_creating_socket(self):
        def make_socket():
            self.assertTrue(_monitor_state.enabled)
            self.assertEqual(_monitor_state.uid, UID)
            _stop_event.set()
            return Mock()

        with (
            patch.object(server.os, "geteuid", return_value=0),
            patch.object(server, "instance_lock"),
            patch.object(server, "load_protected_uid", return_value=UID),
            patch.object(server, "initialize_activity") as activity_init,
            patch("sentinel.daemon.operations.daily_screen_time_ops.initialize") as daily_init,
            patch.object(server.image_filter_ops, "apply_image_filtering",
                         side_effect=lambda: screen_monitor.set_screen_monitor_enabled(True)),
            patch.object(server.block_window_ops, "initialize_block_windows"),
            patch.object(server.dns_ops, "initialize_dns"),
            patch("sentinel.daemon.operations.boot_media_ops.apply_boot_media_policy"),
            patch.object(server.vm_ops, "apply_vm_policy") as vm_init,
            patch.object(server.browser_ops, "initialize_browser_policy"),
            patch.object(server.app_blocker_ops, "initialize_app_blocker"),
            patch.object(server.guardian_app_ops, "initialize_guardian_apps") as guardian_init,
            patch.object(server, "start_extension_server", return_value=(Mock(), Mock())),
            patch.object(
                server,
                "initialize_lock_state",
            ),
            patch.object(
                server,
                "initialize_guardian_auth",
            ),
            patch.object(server.signal, "signal"),
            patch.object(server, "_prepare_socket", side_effect=make_socket),
            patch.object(server.threading, "Thread"),
            patch.object(server, "SENTINELD_SOCKET"),
        ):
            server.main()
            activity_init.assert_called_once_with()
            vm_init.assert_called_once_with()
            daily_init.assert_called_once_with()
            guardian_init.assert_called_once_with(UID)

    def test_self_lock_rejects_weakening_through_socket_and_preserves_setting(
        self,
    ) -> None:
        """
        A weakening request through the real RPC/socket path must be
        denied in Self Lock, and the setting must remain unchanged.
        """

        with tempfile.TemporaryDirectory() as temp:
            settings_path = Path(temp) / "settings.toml"

            settings_path.write_text(
                "safe_browsing = true\n",
                encoding="utf-8",
            )

            state = LockState(
                version=STATE_VERSION,
                mode=LockMode.SELF_LOCK,
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

            conn, client = socket.socketpair()

            with (
                conn,
                client,
                patch.object(
                    server,
                    "_uid_is_authorized",
                    return_value=True,
                ),
                patch.object(
                    policy,
                    "get_lock_state",
                    return_value=state,
                ),
                patch.object(
                    request_handler,
                    "get_lock_state",
                    return_value=state,
                ),
                patch.object(
                    request_handler,
                    "_set_setting",
                    side_effect=set_temp_setting,
                ),
            ):
                payload = {
                    "version": 1,
                    "request_id": "self-lock-weaken",
                    "action": "settings.set",
                    "params": {
                        "name": "safe_browsing",
                        "value": False,
                    },
                }

                client.sendall(json.dumps(payload).encode() + b"\n")

                server._handle_connection(
                    conn,
                )

                response = json.loads(
                    client.recv(4096),
                )

            self.assertFalse(
                response["ok"],
            )

            self.assertEqual(
                response["error"]["code"],
                "permission_denied",
            )

            #
            # Most importantly, the denied RPC must not have
            # changed the setting on disk.
            #
            with settings_path.open(
                "rb",
            ) as stream:
                settings = tomllib.load(
                    stream,
                )

            self.assertTrue(
                settings["safe_browsing"],
            )

    def test_self_lock_allows_strengthening_through_socket(
        self,
    ) -> None:
        """
        Self Lock must still allow the user to enable additional
        protection through the real RPC/socket path.
        """

        with tempfile.TemporaryDirectory() as temp:
            settings_path = Path(temp) / "settings.toml"

            settings_path.write_text(
                "safe_browsing = false\n",
                encoding="utf-8",
            )

            state = LockState(
                version=STATE_VERSION,
                mode=LockMode.SELF_LOCK,
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

            conn, client = socket.socketpair()

            with (
                conn,
                client,
                patch.object(
                    server,
                    "_uid_is_authorized",
                    return_value=True,
                ),
                patch.object(
                    policy,
                    "get_lock_state",
                    return_value=state,
                ),
                patch.object(
                    request_handler,
                    "get_lock_state",
                    return_value=state,
                ),
                patch.object(
                    request_handler,
                    "_set_setting",
                    side_effect=set_temp_setting,
                ),
            ):
                payload = {
                    "version": 1,
                    "request_id": "self-lock-strengthen",
                    "action": "settings.set",
                    "params": {
                        "name": "safe_browsing",
                        "value": True,
                    },
                }

                client.sendall(json.dumps(payload).encode() + b"\n")

                server._handle_connection(
                    conn,
                )

                response = json.loads(
                    client.recv(4096),
                )

            self.assertTrue(
                response["ok"],
            )

            self.assertEqual(
                response["result"]["change"],
                "strengthen",
            )

            self.assertEqual(
                response["result"]["name"],
                "safe_browsing",
            )

            self.assertTrue(
                response["result"]["value"],
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

    def test_invalid_configuration_stops_before_socket(self):
        with (
            patch.object(server.os, "geteuid", return_value=0),
            patch.object(server, "instance_lock"),
            patch.object(
                server, "load_protected_uid", side_effect=ValueError("missing config")
            ),
            patch.object(server, "_prepare_socket") as create,
        ):
            with self.assertRaisesRegex(SystemExit, "missing config"):
                server.main()
        create.assert_not_called()

    def test_socket_ping_and_peer_authorization(self):
        conn, client = socket.socketpair()
        with (
            conn,
            client,
            patch.object(server, "_uid_is_authorized", return_value=True),
        ):
            payload = {
                "version": 1,
                "request_id": "test",
                "action": "ping",
                "params": {},
            }
            client.sendall(json.dumps(payload).encode() + b"\n")
            server._handle_connection(conn)
            response = json.loads(client.recv(4096))
            self.assertTrue(response["ok"])
            self.assertEqual(response["result"]["peer_uid"], os.getuid())
            self.assertEqual(response["result"]["peer_pid"], os.getpid())
        conn, client = socket.socketpair()
        with (
            conn,
            client,
            patch.object(server, "_uid_is_authorized", return_value=False),
        ):
            server._handle_connection(conn)
            self.assertEqual(
                json.loads(client.recv(4096))["error"]["code"], "permission_denied"
            )

    def test_dispatch_uses_authenticated_pid_and_uid(self):
        with patch.object(request_handler, "_register_screen_monitor") as register:
            response = request_handler._handle_request(
                {
                    "request_id": "x",
                    "action": "monitor.register",
                    "params": {"uid": 0, "pid": 1},
                },
                uid=UID,
                pid=PID,
            )
        self.assertTrue(response["ok"])
        register.assert_called_once_with(UID, PID)

    def test_settings_get_dispatches_to_settings_ops(
        self,
    ) -> None:
        """
        settings.get must read through the trusted settings operation.
        """

        with patch.object(
            request_handler,
            "_get_setting",
            return_value=True,
        ) as get_setting:
            response = request_handler._handle_request(
                {
                    "request_id": "x",
                    "action": "settings.get",
                    "params": {
                        "name": "safe_browsing",
                    },
                },
                uid=UID,
                pid=PID,
            )

        self.assertTrue(
            response["ok"],
        )

        self.assertEqual(
            response["result"],
            {
                "name": "safe_browsing",
                "value": True,
            },
        )

        get_setting.assert_called_once_with(
            "safe_browsing",
        )

    def test_settings_set_dispatches_to_settings_ops(
        self,
    ) -> None:
        """
        settings.set must pass only the setting name and requested value
        into the trusted settings operation.
        """

        state = LockState(
            version=STATE_VERSION,
            mode=LockMode.UNLOCKED,
        )

        with (
            patch.object(
                request_handler,
                "get_lock_state",
                return_value=state,
            ),
            patch.object(
                request_handler,
                "_set_setting",
                return_value=ChangeKind.STRENGTHEN,
            ) as set_setting,
        ):
            response = request_handler._handle_request(
                {
                    "request_id": "x",
                    "action": "settings.set",
                    "params": {
                        "name": "safe_browsing",
                        "value": True,
                    },
                },
                uid=UID,
                pid=PID,
            )

        self.assertTrue(
            response["ok"],
        )

        self.assertEqual(
            response["result"],
            {
                "status": "complete",
                "action": "settings.set",
                "name": "safe_browsing",
                "value": True,
                "change": "strengthen",
                "application": request_handler.browser_ops.application_status(),
                "dns_application": request_handler.dns_ops.application_status(),
            },
        )

        set_setting.assert_called_once_with(
            "safe_browsing",
            True,
            guardian_authenticated=False,
        )

    def test_settings_set_requires_value(
        self,
    ) -> None:
        """
        settings.set must reject requests that omit the desired value.
        """

        with patch.object(
            request_handler,
            "_set_setting",
        ) as set_setting:
            response = request_handler._handle_request(
                {
                    "request_id": "x",
                    "action": "settings.set",
                    "params": {
                        "name": "safe_browsing",
                    },
                },
                uid=UID,
                pid=PID,
            )

        self.assertFalse(
            response["ok"],
        )

        self.assertEqual(
            response["error"]["code"],
            "operation_failed",
        )

        set_setting.assert_not_called()

    def test_settings_actions_require_valid_name(
        self,
    ) -> None:
        """
        Settings RPCs must reject missing or non-string setting names.
        """

        for action in (
            "settings.get",
            "settings.set",
        ):
            with self.subTest(action=action):
                params = {}

                if action == "settings.set":
                    params["value"] = True

                response = request_handler._handle_request(
                    {
                        "request_id": "x",
                        "action": action,
                        "params": params,
                    },
                    uid=UID,
                    pid=PID,
                )

                self.assertFalse(
                    response["ok"],
                )

                self.assertEqual(
                    response["error"]["code"],
                    "operation_failed",
                )

    def test_settings_policy_denial_returns_permission_denied(
        self,
    ) -> None:
        """
        PolicyDeniedError must cross the RPC boundary as permission_denied.
        """

        state = LockState(
            version=STATE_VERSION,
            mode=LockMode.UNLOCKED,
        )

        with (
            patch.object(
                request_handler,
                "get_lock_state",
                return_value=state,
            ),
            patch.object(
                request_handler,
                "_set_setting",
                side_effect=PolicyDeniedError(
                    "settings.safe_browsing.set would weaken Sentinel protection."
                ),
            ),
        ):
            response = request_handler._handle_request(
                {
                    "request_id": "x",
                    "action": "settings.set",
                    "params": {
                        "name": "safe_browsing",
                        "value": False,
                    },
                },
                uid=UID,
                pid=PID,
            )

        self.assertFalse(
            response["ok"],
        )

        self.assertEqual(
            response["error"]["code"],
            "permission_denied",
        )

    def test_invalid_package_and_unknown_action_do_not_run_commands(self):
        with patch.object(request_handler, "_install_package") as install:
            response = request_handler._handle_request(
                {
                    "request_id": "x",
                    "action": "package.install",
                    "params": {"name": "--help"},
                },
                uid=UID,
                pid=PID,
            )
        self.assertFalse(response["ok"])
        install.assert_not_called()
        response = request_handler._handle_request(
            {"request_id": "x", "action": "arbitrary"}, uid=UID, pid=PID
        )
        self.assertEqual(response["error"]["code"], "unknown_action")


class ChildProcessTests(unittest.TestCase):
    def test_unregisterable_launch_is_terminated_and_reaped(self):
        child = Mock(pid=444)
        child.poll.return_value = None
        _monitor_state.monitor_child = child
        with patch.object(screen_monitor.os, "killpg") as terminate:
            screen_monitor._terminate_monitor_child()
        self.assertIsNone(_monitor_state.monitor_child)
        terminate.assert_called_once_with(444, screen_monitor.signal.SIGTERM)
        child.wait.assert_called_once_with(timeout=3)

    def test_monitor_launch_preserves_authorized_launcher_and_module(self):
        with (
            patch.object(
                screen_monitor, "SCREEN_MONITOR_EXECUTABLE", Path("/authorized/python")
            ),
            patch.object(Path, "is_file", return_value=True),
            patch.object(
                screen_monitor, "_user_session_command", return_value=["launch"]
            ) as command,
            patch.object(screen_monitor.subprocess, "Popen") as spawn,
        ):
            screen_monitor._launch_screen_monitor(UID)
        command.assert_called_once_with(
            UID,
            "/authorized/python",
            "-I",
            "-B",
            "-m",
            screen_monitor.SCREEN_MONITOR_MODULE,
            wayland=True,
        )
        spawn.assert_called_once_with(["launch"], start_new_session=True)
        self.assertIs(_monitor_state.monitor_child, spawn.return_value)


if __name__ == "__main__":
    unittest.main()
