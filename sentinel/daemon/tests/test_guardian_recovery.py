"""Recovery uses real temporary state/PIN hashes and Unix sockets, never services."""
import json
import os
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from sentinel.daemon import server
from sentinel.daemon.core import instance, lock_state
from sentinel.daemon.security import guardian_auth
from sentinel.daemon.tests import test_guardian_apps as fixtures
from sentinel.guardian_apps import recovery


class GuardianRecoveryTests(unittest.TestCase):
    setUp = fixtures.GuardianAppTests.setUp
    launcher = fixtures.GuardianAppTests.launcher

    def prepare(self):
        self.auth = self.root / 'guardian-auth.json'
        self.state_file = self.root / 'lock-state.json'
        self.attempts = self.root / 'managed/recovery-attempts.json'
        self.auth.write_text(json.dumps(guardian_auth._build_auth_data('5678')))
        self.auth.chmod(0o600)
        self.state_file.write_text('{"version":1,"mode":"guardian_lock"}')
        # Model the protected production file regardless of the developer's
        # umask (e.g. 0002 would otherwise create a group-writable fixture).
        self.state_file.chmod(0o600)
        for name, value in [('GUARDIAN_AUTH_FILE', self.auth), ('LOCK_STATE_FILE', self.state_file),
                            ('GUARDIAN_APPS_FILE', self.path), ('ATTEMPTS_FILE', self.attempts)]:
            self.enterContext(patch.object(recovery, name, value))
        self.enterContext(patch.object(recovery, 'load_protected_uid', return_value=1000))
        self.enterContext(patch.object(recovery.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(instance, 'INSTANCE_LOCK', self.root / 'daemon.lock'))
        # The enclosing fixture already models root ownership of opened inodes.
        # Temp directories may live below /tmp; production requires every parent
        # to be root-owned and protected. Those checks have separate tests below.
        self.enterContext(patch.object(instance, 'protected_directory'))
        self.enterContext(patch.object(recovery, 'protected_directory'))
        self.enterContext(patch.object(lock_state, '_current_state', None))
        self.enterContext(patch.object(lock_state, '_current_path', None))
        from sentinel.daemon.operations import guardian_app_ops as ops
        ops.approve('kate.desktop', guardian_authenticated=True)
        ops.set_enabled(True, guardian_authenticated=True)
        # Model a fresh helper interpreter; use the real lock-state getter so
        # recovery must install both validated in-memory state and its path.
        self.enterContext(patch.object(ops, 'get_lock_state', new=lock_state.get_lock_state))

    def state_bytes(self):
        return {p: p.read_bytes() for p in (self.path, self.auth, self.state_file, self.kde)}

    def test_correct_existing_pin_disables_and_cleans_up_preserving_other_state(self):
        self.prepare()
        before = self.state_bytes()
        saved = json.loads(before[self.path])['apps']
        unrelated = self.root / 'settings.toml'
        unrelated.write_text('safe_browsing=true\n')
        result = recovery.disable_offline('5678', 1000)
        self.assertFalse(result['enabled'])
        self.assertEqual(result['errors'], [])
        self.assertFalse(json.loads(self.path.read_text())['enabled'])
        self.assertEqual(json.loads(self.path.read_text())['apps'], saved)
        self.assertEqual(self.auth.read_bytes(), before[self.auth])
        self.assertEqual(self.state_file.read_bytes(), before[self.state_file])
        self.assertEqual(list(self.overrides.glob('*.desktop')), [])
        self.assertNotIn('BEGIN Sentinel', self.kde.read_text())
        self.assertEqual(unrelated.read_text(), 'safe_browsing=true\n')

    def test_wrong_pin_leaves_policy_and_desktop_state_unchanged(self):
        self.prepare()
        before = self.state_bytes()
        overrides = {p: p.read_bytes() for p in self.overrides.glob('*')}
        # Default PIN must not work when the stored guardian changed it.
        with self.assertRaisesRegex(recovery.RecoveryError, 'Incorrect'):
            recovery.disable_offline('1234', 1000)
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual({p: p.read_bytes() for p in self.overrides.glob('*')}, overrides)

    def test_unset_credential_store_never_authorizes_offline_recovery(self):
        self.prepare()
        self.auth.write_text('{"version":2,"configured":false}')
        before = self.state_bytes()
        with self.assertRaisesRegex(recovery.RecoveryError, 'No Guardian PIN'):
            recovery.disable_offline('1234', 1000)
        self.assertEqual(self.state_bytes(), before)

    def test_attempt_limit_persists_between_helper_calls(self):
        self.prepare()
        with patch.object(recovery.time, 'time', return_value=1000):
            for _ in range(5):
                with self.assertRaisesRegex(recovery.RecoveryError, 'Incorrect'):
                    recovery.disable_offline('0000', 1000)
            with self.assertRaisesRegex(recovery.RecoveryError, 'five minutes'):
                recovery.disable_offline('5678', 1000)
        with patch.object(recovery.time, 'time', return_value=1301):
            self.assertFalse(recovery.disable_offline('5678', 1000)['enabled'])

    def test_self_lock_and_unlocked_cannot_use_recovery(self):
        self.prepare()
        for mode in ('self_lock', 'unlocked'):
            self.state_file.write_text(json.dumps({'version': 1, 'mode': mode}))
            before = self.state_bytes()
            with self.assertRaisesRegex(recovery.RecoveryError, 'only in Guardian Lock'):
                recovery.disable_offline('5678', 1000)
            self.assertEqual(self.state_bytes(), before)

    def test_foreign_peer_cannot_mutate_even_with_correct_pin(self):
        self.prepare()
        before = self.state_bytes()
        with self.assertRaisesRegex(recovery.RecoveryError, 'This user'):
            recovery.disable_offline('5678', 1001)
        self.assertEqual(self.state_bytes(), before)

    def test_missing_auth_never_creates_default_credentials_or_changes_policy(self):
        self.prepare()
        self.auth.unlink()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            recovery.disable_offline('1234', 1000)
        self.assertFalse(self.auth.exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_symlink_or_writable_selection_fails_closed(self):
        self.prepare()
        saved = self.path.read_bytes()
        original = self.root / 'original.json'
        original.write_bytes(saved)
        self.path.unlink()
        self.path.symlink_to(original)
        with self.assertRaises(OSError):
            recovery.disable_offline('5678', 1000)
        self.assertEqual(original.read_bytes(), saved)
        self.path.unlink()
        self.path.write_bytes(saved)
        self.path.chmod(0o666)
        with self.assertRaisesRegex(recovery.RecoveryError, 'protected'):
            recovery.disable_offline('5678', 1000)
        self.assertEqual(self.path.read_bytes(), saved)

    def test_daemon_lifetime_lock_prevents_offline_writer_and_duplicate_daemon(self):
        self.prepare()
        before = self.state_bytes()
        with instance.instance_lock():
            with self.assertRaises(instance.InstanceBusyError):
                recovery.disable_offline('5678', 1000)
            with patch.object(server, '_run_daemon') as run:
                with self.assertRaisesRegex(SystemExit, 'running'):
                    server.main()
                run.assert_not_called()
        self.assertEqual(self.state_bytes(), before)
        self.assertFalse(recovery.disable_offline('5678', 1000)['enabled'])

    def test_real_socket_request_uses_kernel_peer_and_disables(self):
        self.prepare()
        # socketpair exercises the real Unix protocol/SO_PEERCRED without
        # binding a host socket or requiring a live systemd installation.
        conn, peer = socket.socketpair()
        proxy = MagicMock()
        proxy.__enter__.return_value = proxy
        proxy.sendall.side_effect = peer.sendall
        proxy.recv.side_effect = peer.recv
        proxy.settimeout.side_effect = peer.settimeout
        with conn, peer, patch.object(recovery.socket, 'socket', return_value=proxy), \
             patch.object(recovery, 'load_protected_uid', return_value=os.getuid()):
            def serve():
                recovery.handle_connection(conn)
            worker = threading.Thread(target=serve)
            worker.start()
            try:
                result = recovery.request_disable('5678')
                self.assertFalse(result['enabled'])
                proxy.connect.assert_called_once_with(str(recovery.RECOVERY_SOCKET))
            finally:
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def test_extra_command_or_client_supplied_uid_is_rejected(self):
        self.prepare()
        before = self.state_bytes()
        for extra in ({'uid': 0}, {'action': 'system.shutdown'}, {'path': '/tmp/evil'}):
            conn, peer = socket.socketpair()
            with conn, peer:
                peer.sendall(json.dumps({'version': 1, 'guardian_pin': '5678', **extra}).encode() + b'\n')
                with patch.object(recovery, '_get_peer_credentials', return_value=(10, 1000, 1000)):
                    recovery.handle_connection(conn)
                self.assertFalse(json.loads(peer.recv(4096))['ok'])
        self.assertEqual(self.state_bytes(), before)

    def test_helper_entry_point_consumes_socket_passed_as_stdin(self):
        self.prepare()
        conn, peer = socket.socketpair()
        duplicate = os.dup
        with conn, peer:
            peer.sendall(b'{"version":1,"guardian_pin":"5678"}\n')
            with patch.object(recovery.os, 'dup', side_effect=lambda fd: duplicate(conn.fileno()) if fd == 0 else duplicate(fd)), \
                 patch.object(recovery, '_get_peer_credentials', return_value=(10, 1000, 1000)):
                recovery.main()
            self.assertTrue(json.loads(peer.recv(4096))['ok'])

    def test_oversized_request_is_rejected_without_policy_write(self):
        self.prepare()
        before = self.state_bytes()
        conn, peer = socket.socketpair()
        with conn, peer:
            peer.sendall(b'x' * (recovery.MAX_BYTES + 1))
            with patch.object(recovery, '_get_peer_credentials', return_value=(10, 1000, 1000)):
                recovery.handle_connection(conn)
            self.assertIn('too large', json.loads(peer.recv(4096))['error'])
        self.assertEqual(self.state_bytes(), before)


class ProtectedInstanceDirectoryTests(unittest.TestCase):
    def test_user_writable_or_symlink_directory_is_rejected(self):
        for mode in (0o40777, 0o120755):
            info = Mock(st_mode=mode, st_uid=0)
            with patch.object(Path, 'lstat', return_value=info):
                with self.assertRaises(ValueError):
                    instance.protected_directory(Path('/run/sentinel'))

    def test_non_root_directory_is_rejected(self):
        with patch.object(Path, 'lstat', return_value=Mock(st_mode=0o40755, st_uid=1000)):
            with self.assertRaises(ValueError):
                instance.protected_directory(Path('/run/sentinel'))


if __name__ == '__main__':
    unittest.main()
