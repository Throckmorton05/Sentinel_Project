"""Flat APT acquisition and GitHub publication sequencing; no GitHub writes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_repository import RepositoryTests, fixture_deb
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common
import github_repository
import repository_config
import upload_github_channel


class GithubChannelTests(RepositoryTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.github_config = cls.root / 'github-config'
        repository_config.configure(None, cls.fingerprint, cls.github_config,
                                     'owner/project', 'apt-beta')
        cls.channel = cls.root / 'github-channel'
        github_repository.build_channel(cls.debs, cls.platform, cls.github_config, cls.channel)

    def test_github_configuration_has_exact_flat_source(self):
        metadata = repository_config.verify_configuration(self.github_config)
        self.assertEqual(metadata['url'], 'https://github.com/owner/project/releases/download/apt-beta')
        source = (self.github_config / 'sentinel.sources').read_text()
        self.assertIn('Suites: ./\n', source)
        self.assertIn('By-Hash: no\n', source)
        self.assertNotIn('Components:', source)

    def test_flat_apt_verifies_indexes_and_downloads_packages(self):
        command = self.apt_root('apt-flat', self.channel)
        source_option = next(value for value in command if value.startswith('Dir::Etc::sourcelist='))
        path = Path(source_option.split('=', 1)[1])
        path.write_text(f'deb [arch=amd64 signed-by={self.github_config / "sentinel-archive-keyring.gpg"}] file:{self.channel}/ ./\n')
        subprocess.run([*command, 'update'], check=True, capture_output=True)
        # This fetches fixture archives into isolated cache; it cannot install.
        result = subprocess.run([*command, '--download-only', '-y', 'install', 'sentinel-platform', 'sentinel'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.returncode, 0)
        index = (self.channel / 'Packages').read_text()
        self.assertNotIn('Filename: ./', index)
        self.assertIn('Filename: sentinel-platform_', index)

    def test_flat_previous_snapshot_retained_and_tamper_rejected(self):
        second = self.root / 'github-second'
        github_repository.build_channel(self.debs, self.platform, self.github_config, second, self.channel)
        self.assertEqual({p.name for p in self.channel.glob('*.deb')}, {p.name for p in second.glob('*.deb')})
        path = next(second.glob('*.deb'))
        with path.open('ab') as stream:
            stream.write(b'tamper')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            github_repository.build_channel(self.debs, self.platform, self.github_config,
                                            self.root / 'github-never-created', second)

    def test_upload_packages_first_inrelease_last(self):
        calls, assets = [], {}
        def state(repo, tag):
            return {'tag_name': tag, 'immutable': False, 'draft': False, 'assets': list(assets.values())}
        def fake_gh(*args, **kwargs):
            self.assertEqual(args[:2], ('release', 'upload'))
            path = Path(args[3]); calls.append((path.name, '--clobber' in args))
            assets[path.name] = {'name': path.name, 'state': 'uploaded',
                                 'size': path.stat().st_size, 'digest': 'sha256:' + common.digest(path)}
        with patch.object(upload_github_channel, 'release', side_effect=state), \
             patch.object(upload_github_channel, 'gh', side_effect=fake_gh):
            upload_github_channel.upload(self.channel, 'owner/project', 'apt-beta', self.fingerprint)
        self.assertTrue(all(name.endswith('.deb') and not clobber for name, clobber in calls[:6]))
        self.assertEqual(calls[-1], ('InRelease', True))

    def test_remote_package_name_collision_prevents_all_uploads(self):
        package = next(self.channel.glob('*.deb'))
        existing = {'name': package.name, 'state': 'uploaded', 'size': package.stat().st_size,
                    'digest': 'sha256:' + '0' * 64}
        with patch.object(upload_github_channel, 'release', return_value={'assets': [existing]}), \
             patch.object(upload_github_channel, 'gh') as gh:
            with self.assertRaisesRegex(ValueError, 'bump the version'):
                upload_github_channel.upload(self.channel, 'owner/project', 'apt-beta', self.fingerprint)
        gh.assert_not_called()

    def test_immutable_channel_refused(self):
        with patch.object(upload_github_channel, 'gh', return_value=json.dumps(
            {'tag_name': 'apt-beta', 'immutable': True})):
            with self.assertRaisesRegex(ValueError, 'mutable'):
                upload_github_channel.release('owner/project', 'apt-beta')


class GithubReleaseLookupTests(unittest.TestCase):
    def test_draft_release_lookup_uses_authenticated_list_fallback(self):
        draft = {'tag_name': 'apt-beta', 'immutable': False, 'draft': True, 'assets': []}
        with patch.object(upload_github_channel, 'gh', side_effect=[
            subprocess.CalledProcessError(1, ['gh', 'api'], stderr='gh: Not Found (HTTP 404)'),
            ' \n' + json.dumps([{'tag_name': 'other'}]) + '\n' + json.dumps([draft]) + '\n'
        ]) as gh:
            self.assertTrue(upload_github_channel.release('owner/project', 'apt-beta')['draft'])
        self.assertEqual(gh.call_args.args,
                         ('api', '--paginate', 'repos/owner/project/releases?per_page=100'))

    def test_duplicate_drafts_across_pages_are_rejected(self):
        draft = {'tag_name': 'apt-beta', 'immutable': False}
        with patch.object(upload_github_channel, 'gh', side_effect=[
            subprocess.CalledProcessError(1, ['gh', 'api'], stderr='gh: Not Found (HTTP 404)'),
            json.dumps([draft]) + json.dumps([draft])
        ]):
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                upload_github_channel.release('owner/project', 'apt-beta')

    def test_authentication_error_does_not_trigger_draft_fallback(self):
        error = subprocess.CalledProcessError(1, ['gh', 'api'], stderr='gh: Bad credentials (HTTP 401)')
        with patch.object(upload_github_channel, 'gh', side_effect=error) as gh:
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                upload_github_channel.release('owner/project', 'apt-beta')
        self.assertIs(raised.exception, error)
        gh.assert_called_once()
