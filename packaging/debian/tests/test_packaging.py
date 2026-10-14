"""Packaging regressions without root mutations, network access, or Qt imports."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

CERTIFICATE_PATHS = (
    'usr/lib/sentinel/venv/lib/python3.14/site-packages/certifi/cacert.pem',
    'usr/lib/sentinel/venv/lib/python3.14/site-packages/pip/_vendor/certifi/cacert.pem',
)

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import stage
import package_lifecycle as lifecycle
import build

spec = importlib.util.spec_from_file_location('sentinel_package_setup', SCRIPTS / 'setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class PackagingTests(unittest.TestCase):
    def setUp(self):
        # These tests mock protected-directory checks where only content behavior
        # is relevant; no production absolute path is written or chmodded.
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def file(self, name, content='test'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def directory_check(self, path, entries, *, syslog_gid=104):
        def metadata(candidate):
            mode, uid, gid = entries.get(str(candidate), (stat.S_IFDIR | 0o755, 0, 0))
            return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)
        lookup = (KeyError('syslog') if syslog_gid is None
                  else SimpleNamespace(gr_gid=syslog_gid))
        with patch.object(Path, 'exists', return_value=True), \
             patch.object(Path, 'lstat', autospec=True, side_effect=metadata), \
             patch.object(lifecycle.grp, 'getgrnam', side_effect=(
                 lookup if isinstance(lookup, KeyError) else None), return_value=lookup), \
             patch.object(Path, 'mkdir') as mkdir, patch.object(Path, 'chmod') as chmod:
            lifecycle.protected_directory(Path(path))
            mkdir.assert_not_called()
            chmod.assert_not_called()

    def test_ubuntu_syslog_parent_allows_strict_sentinel_log_directory(self):
        self.directory_check('/var/log/sentinel', {
            '/var/log': (stat.S_IFDIR | 0o775, 0, 104),
            '/var/log/sentinel': (stat.S_IFDIR | 0o755, 0, 0),
        })

    def test_root_only_log_parent_does_not_require_syslog_group(self):
        self.directory_check('/var/log/sentinel', {}, syslog_gid=None)

    def test_fresh_sentinel_log_directory_is_created_under_syslog_parent(self):
        target = Path('/var/log/sentinel')
        def metadata(candidate):
            mode, gid = (0o775, 104) if candidate == target.parent else (0o755, 0)
            return SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=0, st_gid=gid)
        with patch.object(Path, 'exists', autospec=True, side_effect=lambda path: path != target), \
             patch.object(Path, 'is_symlink', return_value=False), \
             patch.object(Path, 'lstat', autospec=True, side_effect=metadata), \
             patch.object(lifecycle.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=104)), \
             patch.object(Path, 'mkdir', autospec=True) as mkdir:
            lifecycle.protected_directory(target)
        mkdir.assert_called_once_with(target, mode=0o755)

    def test_log_parent_rejects_world_write_wrong_group_owner_and_symlink(self):
        for mode, uid, gid in (
            (stat.S_IFDIR | 0o777, 0, 104),
            (stat.S_IFDIR | 0o775, 0, 1000),
            (stat.S_IFDIR | 0o775, 1000, 104),
            (stat.S_IFLNK | 0o777, 0, 104),
        ):
            with self.subTest(mode=mode, uid=uid, gid=gid):
                with self.assertRaisesRegex(ValueError, 'unprotected directory: /var/log$'):
                    self.directory_check('/var/log/sentinel', {'/var/log': (mode, uid, gid)})

    def test_group_writable_log_parent_rejected_without_syslog_group(self):
        with self.assertRaisesRegex(ValueError, 'unprotected directory: /var/log$'):
            self.directory_check('/var/log/sentinel', {
                '/var/log': (stat.S_IFDIR | 0o775, 0, 104),
            }, syslog_gid=None)

    def test_syslog_exception_does_not_allow_writable_sentinel_directories(self):
        for path in ('/var/log/sentinel', '/etc/sentinel', '/var/lib/sentinel'):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, 'unprotected directory: ' + path + '$'):
                    self.directory_check(path, {
                        '/var/log': (stat.S_IFDIR | 0o775, 0, 104),
                        path: (stat.S_IFDIR | 0o775, 0, 104),
                    })

    def test_log_exception_does_not_allow_writable_other_ancestors(self):
        with self.assertRaisesRegex(ValueError, 'unprotected directory: /var$'):
            self.directory_check('/var/log/sentinel', {
                '/var': (stat.S_IFDIR | 0o775, 0, 104),
                '/var/log': (stat.S_IFDIR | 0o775, 0, 104),
            })

    def test_models_reject_lfs_pointer_and_truncated_inputs(self):
        with self.assertRaisesRegex(ValueError, 'Git LFS pointer'):
            stage.validate_model(self.file('pointer.onnx', 'version https://git-lfs.github.com/spec/v1\n'))
        with self.assertRaisesRegex(ValueError, 'truncated'):
            stage.validate_model(self.file('small.onnx', b'\x08\x09'))

    def test_model_input_cannot_be_symlink(self):
        actual = self.file('actual.onnx', b'x' * 1_000_001)
        link = self.root / 'link.onnx'
        link.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, 'symlinked'):
            stage.validate_model(link)

    def test_existing_configuration_is_never_replaced(self):
        source = self.file('default', 'new defaults')
        current = self.file('config', 'user settings and schedules')
        with patch.object(lifecycle, 'protected_directory'), patch.object(lifecycle, 'protected_file'):
            self.assertFalse(lifecycle.create_once(source, current))
        self.assertEqual(current.read_text(), 'user settings and schedules')

    def test_seed_is_created_only_once(self):
        source = self.file('default', 'defaults')
        destination = self.root / 'created'
        with patch.object(lifecycle, 'protected_directory'), patch.object(lifecycle, 'protected_file'):
            self.assertTrue(lifecycle.create_once(source, destination, 0o600))
            source.write_text('changed package defaults')
            self.assertFalse(lifecycle.create_once(source, destination, 0o600))
        self.assertEqual(destination.read_text(), 'defaults')
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_seed_rejects_symlink_even_when_target_does_not_exist(self):
        destination = self.root / 'link'
        destination.symlink_to(self.root / 'absent')
        with patch.object(lifecycle, 'protected_directory'):
            with self.assertRaisesRegex(ValueError, 'unprotected file'):
                lifecycle.create_once(self.file('default'), destination)

    def test_generated_owned_file_cleanup_is_conservative(self):
        path = self.file('policy', 'administrator policy')
        with patch.object(lifecycle, 'protected_file'):
            with self.assertRaisesRegex(ValueError, 'retained'):
                lifecycle.remove_owned(path, '# Sentinel')
            self.assertTrue(path.exists())
            path.write_text('# Sentinel\nowned rule')
            lifecycle.remove_owned(path, '# Sentinel')
            lifecycle.remove_owned(path, '# Sentinel')
        self.assertFalse(path.exists())

    def test_source_tree_rejects_private_material(self):
        source = self.root / 'source'
        source.mkdir()
        self.file('source/browser_extension.pem', 'private key')
        with self.assertRaisesRegex(ValueError, 'Private signing key'):
            stage.install_tree(source, self.root / 'destination')

    def test_wheel_rejects_signing_keys(self):
        path = self.root / 'sen.whl'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('sentinel/browser_distribution/key.pem', 'private')
        with self.assertRaisesRegex(ValueError, 'private'):
            stage.validate_wheel(path)

    def public_bundle(self):
        # Build prerequisites include ca-certificates; use real X.509 material
        # so this regression also exercises OpenSSL parsing, not mocked labels.
        cafile = ssl.get_default_verify_paths().cafile
        if not cafile:
            self.skipTest('No system CA bundle available')
        return Path(cafile).read_bytes()

    def test_certifi_public_ca_bundle_is_allowed(self):
        for name in CERTIFICATE_PATHS:
            with self.subTest(name=name):
                build.inspect_key_material(self.file(name, self.public_bundle()), self.root)

    def test_private_key_cannot_hide_in_certifi_bundle(self):
        public = self.public_bundle()
        for name in CERTIFICATE_PATHS:
            for label in ('PRIVATE KEY', 'RSA PRIVATE KEY', 'EC PRIVATE KEY',
                          'DSA PRIVATE KEY', 'ENCRYPTED PRIVATE KEY', 'OPENSSH PRIVATE KEY'):
                with self.subTest(name=name, label=label):
                    path = self.file(name, public +
                                     f'\n-----BEGIN {label}-----\nsecret\n-----END {label}-----\n'.encode())
                    with self.assertRaisesRegex(ValueError, 'Private or unexpected PEM'):
                        build.inspect_key_material(path, self.root)

    def test_other_pem_and_key_files_still_rejected(self):
        for filename in ('browser_extension.pem', 'browser_extension.key',
                         'usr/lib/sentinel/venv/lib/python3.14/site-packages/other/cacert.pem'):
            with self.subTest(filename=filename):
                with self.assertRaisesRegex(ValueError, 'Private signing material'):
                    build.inspect_key_material(self.file(filename, self.public_bundle()), self.root)

    def test_certifi_bundle_must_contain_valid_certificates(self):
        for name in CERTIFICATE_PATHS:
            for content in (b'no certificates', b'-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----\n'):
                with self.subTest(name=name, content=content):
                    with self.assertRaises(ValueError):
                        build.inspect_key_material(self.file(name, content), self.root)

    def test_certifi_bundle_cannot_be_a_symlink(self):
        public = self.file('public-ca', self.public_bundle())
        for name in CERTIFICATE_PATHS:
            with self.subTest(name=name):
                link = self.root / name
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(public)
                with self.assertRaisesRegex(ValueError, 'Private signing material'):
                    build.inspect_key_material(link, self.root)

    def completed_build(self):
        for name, content in {
            'work/sentinel.deb': b'package bytes', 'work/sentinel.changes': b'changes',
            'work/sentinel.buildinfo': b'build info', 'work/temporary.log': b'not an output',
            'work/source/poetry.lock': b'lock', 'inputs/manifest.json': b'{}',
            'inputs/requirements.txt': b'locked requirements',
            'inputs/wheelhouse/dependency.whl': b'dependency',
            'inputs/project-wheel/sen.whl': b'project',
        }.items():
            self.file(name, content)
        return self.root / 'work/source', self.root / 'work', self.root / 'inputs', self.root / 'output'

    def test_failed_audit_retains_unvalidated_build_outputs(self):
        source, work, inputs, final = self.completed_build()
        with patch.object(build, 'inspect_packages', side_effect=subprocess.CalledProcessError(1, 'lintian')):
            with self.assertRaisesRegex(ValueError, 'Unvalidated build artifacts retained at:'):
                build.audit_and_retain(source, work, inputs, final)
        unchecked = final.with_name('output-unchecked')
        self.assertFalse(final.exists())
        self.assertIn('Do not install', (unchecked / 'VALIDATION_FAILED.txt').read_text())
        self.assertEqual((unchecked / 'sentinel.deb').read_bytes(), b'package bytes')
        self.assertEqual((unchecked / 'wheelhouse/dependency.whl').read_bytes(), b'dependency')
        self.assertFalse((unchecked / 'temporary.log').exists())
        subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=unchecked, check=True,
                       stdout=subprocess.DEVNULL)

    def test_passed_audit_publishes_checked_output(self):
        source, work, inputs, final = self.completed_build()
        with patch.object(build, 'inspect_packages', return_value=[work / 'sentinel.deb']) as inspect:
            self.assertEqual(build.audit_and_retain(source, work, inputs, final), [work / 'sentinel.deb'])
        inspect.assert_called_once_with(source, work)
        self.assertFalse(final.with_name('output-unchecked').exists())
        self.assertFalse((final / 'VALIDATION_FAILED.txt').exists())
        self.assertEqual((final / 'project-wheel/sen.whl').read_bytes(), b'project')
        subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=final, check=True,
                       stdout=subprocess.DEVNULL)

    def test_retained_audit_rejects_modified_or_extra_archives(self):
        source, work, inputs, final = self.completed_build()
        build.retain_artifacts(source, work, inputs, final)
        build.verify_retained_artifacts(final)
        archive = final / 'sentinel.deb'
        archive.write_bytes(b'modified package')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            build.audit_retained(final)
        archive.write_bytes(b'package bytes')
        (final / 'extra.deb').write_bytes(b'unlisted package')
        with self.assertRaisesRegex(ValueError, 'do not match SHA256SUMS'):
            build.audit_retained(final)

    def retained_real_packages(self):
        source, work, inputs, final = self.completed_build()
        (work / 'sentinel.deb').unlink()
        for name in stage.PACKAGES:
            control = self.file(f'payload/{name}/DEBIAN/control',
                                f'Package: {name}\nVersion: 0.1.0-1\nArchitecture: all\n'
                                'Maintainer: Sentinel Test <test@example.invalid>\n'
                                'Description: disposable audit fixture\n')
            if name == 'sentinel-core':
                for certificate in CERTIFICATE_PATHS:
                    self.file(f'payload/{name}/{certificate}', self.public_bundle())
            subprocess.run(['dpkg-deb', '--root-owner-group', '-b', str(control.parent.parent),
                            str(work / f'{name}.deb')], check=True, stdout=subprocess.DEVNULL)
        unchecked = final.with_name('output-unchecked')
        build.retain_artifacts(source, work, inputs, unchecked)
        (unchecked / 'VALIDATION_FAILED.txt').write_text('Previous certificate audit failed')
        return unchecked, final

    def test_retained_audit_extracts_real_archives_and_checks_both_bundles(self):
        unchecked, final = self.retained_real_packages()
        original_run = build.run
        def run_without_lintian(*args, **kwargs):
            if args[0] != 'lintian':
                original_run(*args, **kwargs)
        with patch.object(build, 'run', side_effect=run_without_lintian) as run:
            build.audit_retained(unchecked)
        self.assertEqual(sum(call.args[0] == 'dpkg-deb' for call in run.call_args_list), 5)
        lintian = [call for call in run.call_args_list if call.args[0] == 'lintian']
        self.assertEqual(len(lintian), 1)
        self.assertEqual(lintian[0].args[:3], ('lintian', '--fail-on', 'error'))
        self.assertTrue(final.is_dir())
        self.assertFalse(unchecked.exists())
        self.assertFalse((final / 'VALIDATION_FAILED.txt').exists())

    def test_retained_audit_keeps_output_unchecked_on_lintian_failure(self):
        unchecked, final = self.retained_real_packages()
        original_run = build.run
        def fail_lintian(*args, **kwargs):
            if args[0] == 'lintian':
                raise subprocess.CalledProcessError(1, 'lintian')
            original_run(*args, **kwargs)
        with patch.object(build, 'run', side_effect=fail_lintian):
            with self.assertRaisesRegex(ValueError, 'Unvalidated build artifacts retained at:'):
                build.audit_retained(unchecked)
        self.assertFalse(final.exists())
        self.assertIn('lintian', (unchecked / 'VALIDATION_FAILED.txt').read_text())
        self.assertIn('Do not install', (unchecked / 'VALIDATION_FAILED.txt').read_text())
        build.verify_retained_artifacts(unchecked)

    def test_audit_mode_does_not_prepare_a_new_build(self):
        with patch.object(sys, 'argv', ['build.py', '--audit', str(self.root)]), \
             patch.object(build.os, 'geteuid', return_value=1000), \
             patch.object(build, 'audit_retained') as audit, \
             patch.object(build, 'check_sources') as check, \
             patch.object(build, 'target_environment') as environment:
            build.main()
        audit.assert_called_once_with(self.root.resolve())
        check.assert_not_called()
        environment.assert_not_called()

    def test_wheel_rejects_path_traversal(self):
        path = self.root / 'sen.whl'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('../outside.py', 'unsafe')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            stage.validate_wheel(path)

    def test_wheel_must_include_native_steam_and_qml_assets(self):
        path = self.root / 'sen.whl'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('sen/cli/sen.py', 'safe')
        with self.assertRaisesRegex(ValueError, 'lacks required'):
            stage.validate_wheel(path)

    def test_venv_shebangs_and_trampolines_are_relocated(self):
        venv = self.root / 'venv'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'lib').mkdir()
        (venv / 'bin/python').symlink_to('/usr/bin/python3.14')
        for name, content in {
            'sen': '#!/tmp/build/venv/bin/python\nprint("hello")\n',
            'longpath': '#!/bin/sh\n\'\'\'exec\' /tmp/python "$0" "$@"\n\' \'\'\'\nprint("hello")\n',
            'activate': 'temporary path', 'pip': '#!/tmp/build/venv/bin/python\n',
        }.items():
            (venv / 'bin' / name).write_text(content)
        (venv / 'lib/direct_url.json').write_text('{}')
        stage.relocate_venv(venv, '/usr/lib/sentinel/venv', Path('/usr/bin/python3.14'))
        for name in ('sen', 'longpath'):
            self.assertEqual((venv / 'bin' / name).read_text(),
                             '#!/usr/lib/sentinel/venv/bin/python -IB\nprint("hello")\n')
        self.assertFalse((venv / 'bin/activate').exists())
        self.assertFalse((venv / 'bin/pip').exists())
        self.assertFalse((venv / 'lib/direct_url.json').exists())
        self.assertNotIn(str(self.root), (venv / 'pyvenv.cfg').read_text())

    def native_fixture(self, search_path, old_rpath=False):
        if not shutil.which('gcc') or not shutil.which('patchelf'):
            self.skipTest('gcc and patchelf are required build prerequisites')
        venv = self.root / 'venv'
        for name in ('bin', 'lib', 'libexec'):
            (venv / name).mkdir(parents=True)
        library_source = self.file('answer.c', 'int answer(void) { return 42; }\n')
        program_source = self.file('program.c',
                                   '#include <stdio.h>\nextern int answer(void);\n'
                                   'int main(void) { printf("%d\\n", answer()); return 0; }\n')
        library = venv / 'lib/libanswer.so'
        program = venv / 'libexec/helper'
        subprocess.run(['gcc', '-g', '-shared', '-fPIC', '-Wl,-soname,libanswer.so',
                        str(library_source), '-o', str(library)], check=True)
        command = ['gcc', '-g', str(program_source), '-L' + str(library.parent), '-lanswer',
                   '-Wl,-rpath,' + search_path, '-o', str(program)]
        if old_rpath:
            command.insert(1, '-Wl,--disable-new-dtags')
        subprocess.run(command, check=True)
        return venv, library, program

    def test_native_relocation_and_stripping_preserve_loading_and_modes(self):
        for old_rpath in (False, True):
            with self.subTest(old_rpath=old_rpath):
                if (self.root / 'venv').exists():
                    shutil.rmtree(self.root / 'venv')
                venv, library, program = self.native_fixture(
                    ':$ORIGIN/../lib:/opt/_internal/build/lib:', old_rpath)
                source = self.file('venv/lib/module.py', '#!/usr/bin/python3\npass\n')
                source.chmod(0o755)
                library.chmod(0o755)
                executable_dso = library.parent / 'libversion-report.so.6'
                shutil.copyfile(program, executable_dso)
                executable_dso.chmod(0o755)
                self.file('venv/.gitignore', '*')
                before_needed, _ = stage.elf_dynamic(program)
                changes = stage.normalize_elf_search_paths(venv)
                self.assertEqual({change['file'] for change in changes},
                                 {'lib/libversion-report.so.6', 'libexec/helper'})
                self.assertEqual(stage.elf_dynamic(program),
                                 (before_needed, [('RPATH' if old_rpath else 'RUNPATH', '$ORIGIN/../lib')]))
                stage.relocate_venv(venv, '/usr/lib/sentinel/venv', Path('/usr/bin/python3.14'))
                self.assertEqual(library.stat().st_mode & 0o777, 0o644)
                self.assertEqual(executable_dso.stat().st_mode & 0o777, 0o644)
                self.assertEqual(source.stat().st_mode & 0o777, 0o644)
                self.assertEqual(program.stat().st_mode & 0o777, 0o755)
                self.assertFalse((venv / '.gitignore').exists())
                self.assertEqual(subprocess.check_output([str(program)], text=True), '42\n')
                subprocess.run(['strip', '--strip-unneeded', str(library), str(program)], check=True)
                self.assertEqual(subprocess.check_output([str(program)], text=True), '42\n')
                self.assertEqual(stage.elf_dynamic(program)[0], before_needed)

    def test_unknown_absolute_native_search_path_is_not_silenced(self):
        venv, _, program = self.native_fixture('/unexpected/private-library')
        with self.assertRaisesRegex(ValueError, 'Unexpected ELF search path'):
            stage.normalize_elf_search_paths(venv)
        self.assertEqual(stage.elf_dynamic(program)[1], [('RUNPATH', '/unexpected/private-library')])

    def test_native_origin_path_cannot_escape_environment(self):
        venv, _, _ = self.native_fixture('$ORIGIN/../../outside')
        with self.assertRaisesRegex(ValueError, 'escapes private environment'):
            stage.normalize_elf_search_paths(venv)

    def test_rebuild_reuses_only_verified_dependency_wheels(self):
        source, work, inputs, retained = self.completed_build()
        build.retain_artifacts(source, work, inputs, retained)
        wheelhouse = self.root / 'new-wheelhouse'
        wheelhouse.mkdir()
        build.reuse_wheelhouse(retained, source, inputs / 'requirements.txt', wheelhouse)
        self.assertEqual([path.name for path in wheelhouse.iterdir()], ['dependency.whl'])
        self.assertEqual((wheelhouse / 'dependency.whl').read_bytes(), b'dependency')

    def test_reused_dependencies_must_match_current_lock_and_export(self):
        source, work, inputs, retained = self.completed_build()
        build.retain_artifacts(source, work, inputs, retained)
        wheelhouse = self.root / 'new-wheelhouse'
        wheelhouse.mkdir()
        (source / 'poetry.lock').write_bytes(b'changed lock')
        with self.assertRaisesRegex(ValueError, 'different poetry.lock'):
            build.reuse_wheelhouse(retained, source, inputs / 'requirements.txt', wheelhouse)
        (source / 'poetry.lock').write_bytes(b'lock')
        (inputs / 'requirements.txt').write_bytes(b'changed export')
        with self.assertRaisesRegex(ValueError, 'different requirement export'):
            build.reuse_wheelhouse(retained, source, inputs / 'requirements.txt', wheelhouse)
        self.assertEqual(list(wheelhouse.iterdir()), [])

    def test_modified_saved_dependency_is_rejected_before_reuse(self):
        source, work, inputs, retained = self.completed_build()
        build.retain_artifacts(source, work, inputs, retained)
        (retained / 'wheelhouse/dependency.whl').write_bytes(b'modified wheel')
        wheelhouse = self.root / 'new-wheelhouse'
        wheelhouse.mkdir()
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            build.reuse_wheelhouse(retained, source, inputs / 'requirements.txt', wheelhouse)
        self.assertEqual(list(wheelhouse.iterdir()), [])

    def test_restart_worker_does_not_run_in_chroot_or_unconfigured_install(self):
        with patch.object(lifecycle, 'systemd_available', return_value=False), patch.object(lifecycle, 'run') as run:
            lifecycle.schedule_restart()
            run.assert_not_called()

    def test_restart_does_not_enable_disabled_model_services(self):
        with patch.object(lifecycle, 'package_manager_idle', return_value=True), \
             patch.object(lifecycle.subprocess, 'check_output', return_value='install ok installed'), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(lifecycle, 'sync_monitor_interpreter'), patch.object(lifecycle, 'run') as run:
            lifecycle.restart_when_idle()
        self.assertEqual(run.call_args.args[0:2], ('deb-systemd-invoke', 'try-restart'))
        self.assertIn('sentineld.service', run.call_args.args)

    def test_restart_does_not_resurrect_removed_package(self):
        with patch.object(lifecycle, 'package_manager_idle', return_value=True), \
             patch.object(lifecycle.subprocess, 'check_output', return_value='deinstall ok config-files'), \
             patch.object(lifecycle, 'run') as run:
            lifecycle.restart_when_idle()
            run.assert_not_called()

    def test_real_posix_package_lock_is_detected(self):
        path = self.file('dpkg-lock', '')
        code = ('import fcntl, sys; f=open(sys.argv[1], "r+"); '
                'fcntl.lockf(f, fcntl.LOCK_EX); print("ready", flush=True); sys.stdin.readline()')
        child = subprocess.Popen([sys.executable, '-c', code, str(path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            self.assertFalse(lifecycle.package_manager_idle((path,)))
            child.stdin.write('release\n')
            child.stdin.flush()
            child.wait(timeout=5)
            self.assertTrue(lifecycle.package_manager_idle((path,)))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdin.close()
            child.stdout.close()

    def test_setup_does_not_reassign_an_existing_protected_account(self):
        config = self.file('daemon.toml', 'protected_user = "existing"\n')
        user = type('User', (), {'pw_uid': 1000, 'pw_name': 'other', 'pw_shell': '/bin/bash'})()
        with patch.object(setup.pwd, 'getpwnam', return_value=user), patch.object(setup, 'protected_file'):
            with self.assertRaisesRegex(ValueError, 'will not replace'):
                setup.account_for_setup('other', config)

    def test_setup_rejects_root_account(self):
        with patch.object(setup.pwd, 'getpwnam', return_value=type('User', (), {'pw_uid': 0})()):
            with self.assertRaisesRegex(ValueError, 'non-root'):
                setup.account_for_setup('root', self.root / 'missing')

    def test_adguard_credentials_are_not_sent_to_remote_urls(self):
        path = self.file('adguard.toml', '[adguard]\nurl="https://example.com"\nusername="u"\npassword="p"\n')
        path.chmod(0o600)
        with patch.object(setup, 'protected_file'), patch.object(setup.urllib.request, 'build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'local'):
                setup.check_adguard(path)
            opener.assert_not_called()

    def test_target_release_is_explicit(self):
        with patch.object(build.platform, 'freedesktop_os_release', return_value={'ID': 'ubuntu', 'VERSION_ID': '24.04'}):
            with self.assertRaisesRegex(ValueError, '26.04'):
                build.target_environment()

    def test_complete_public_payloads_are_staged_and_private_keys_are_excluded(self):
        root = stage.ROOT
        # Use the supplied public assets. Substitute a model byte fixture only
        # for the deliberately omitted large Hentai model, inside this temp tree.
        with patch.dict(stage.MODEL_FILES, {'assets/Sentinel_Shield.svg': 'hentai-vit/model.onnx'}, clear=True):
            packages = stage.install_payloads(root, self.root / 'payloads')
        self.assertEqual(set(packages), set(stage.PACKAGES))
        for name in stage.PACKAGES:
            doc = packages[name] / 'usr/share/doc' / name
            for notice in ('LICENSE', 'NOTICE.md', 'THIRD_PARTY.md'):
                self.assertEqual((doc / notice).read_bytes(), (root / notice).read_bytes())
        desktop = packages['sentinel-desktop']
        launchers = desktop / 'usr/share/applications'
        self.assertTrue((launchers / 'sentinel-portal.desktop').is_file())
        guide = (launchers / 'sentinel-getting-started.desktop').read_text()
        self.assertIn('-I -B -m sentinel.portal.main --guide\n', guide)
        self.assertIn('Name=Start Here — Sentinel\n', guide)
        recovery = (launchers / 'sentinel-guardian-access.desktop').read_text()
        self.assertIn('NoDisplay=true\n', recovery)
        self.assertIn('Name=Sentinel recovery\n', recovery)
        self.assertFalse((launchers / 'sentinel-guardian-websites.desktop').exists())
        scripts = stage.tomllib.loads((root / 'pyproject.toml').read_text())['project']['scripts']
        self.assertNotIn('sentinel-guardian-access', scripts)
        self.assertNotIn('sentinel-guardian-websites', scripts)
        permission = (desktop / 'usr/share/applications/sentinel-screen-monitor.desktop').read_text()
        self.assertIn('sentinel-screen-monitor-python -I -B -m', permission)
        self.assertIn('X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2\n', permission)
        self.assertNotIn('org.kde.KWin.ScreenShot2;', permission)
        assets = packages['sentinel-assets'] / 'usr/share/sentinel'
        self.assertEqual((assets / 'assets/sentinel-portal.svg').read_bytes(),
                         (root / 'sentinel/portal/assets/Sentinel_Shield.svg').read_bytes())
        self.assertEqual((assets / 'defaults/blocked_apps.toml').read_text(),
                         (root / 'packaging/debian/defaults/blocked_apps.toml').read_text())
        self.assertNotIn('password', (assets / 'info.toml').read_text())
        self.assertFalse(any(path.suffix in {'.pem', '.key'} for path in self.root.rglob('*')))
        self.assertTrue((desktop / 'usr/share/kwin/scripts/sentinel-window-bridge/metadata.json').is_file())

    def test_monitor_interpreter_refresh_is_atomic_and_keeps_old_open_inode(self):
        source = self.file('new-python', b'new interpreter')
        prefix = self.root / 'prefix'
        target = self.file('prefix/venv/bin/sentinel-screen-monitor-python', b'old interpreter')
        with target.open('rb') as running, patch.object(lifecycle, 'PREFIX', prefix), \
             patch.object(lifecycle, 'Path', side_effect=lambda name: source if str(name) == '/usr/bin/python3.14' else Path(name)), \
             patch.object(lifecycle, 'protected_file'):
            lifecycle.sync_monitor_interpreter()
            self.assertEqual(running.read(), b'old interpreter')
        self.assertEqual(target.read_bytes(), b'new interpreter')
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)


if __name__ == '__main__':
    unittest.main()
