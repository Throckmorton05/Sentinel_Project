"""Real signing, real APT index acquisition, and disposable package metadata only."""
import json
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common
import publish_repository
import repository_config
import platform_package
import platform_postinst


def fixture_deb(parent, name, version='0.1.0-8', arch='all', content='fixture'):
    root = parent / (name + '-root')
    (root / 'DEBIAN').mkdir(parents=True, exist_ok=True)
    (root / 'usr/share/test').mkdir(parents=True, exist_ok=True)
    (root / 'usr/share/test' / name).write_text(content)
    (root / 'DEBIAN/control').write_text(
        f'Package: {name}\nVersion: {version}\nArchitecture: {arch}\n'
        'Maintainer: Test <test@example.invalid>\nDescription: disposable fixture\n')
    target = parent / f'{name}_{version}_{arch}.deb'
    subprocess.run(['dpkg-deb', '--build', str(root), str(target)], check=True, stdout=subprocess.DEVNULL)
    return target


@unittest.skipUnless(all(shutil.which(c) for c in ('gpg', 'gpgv', 'dpkg-deb', 'apt-ftparchive', 'apt-get')), 'APT/GPG build tools required')
class RepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.gpg = cls.root / 'gpg'
        cls.gpg.mkdir(mode=0o700)
        cls.environment = patch.dict(os.environ, {'GNUPGHOME': str(cls.gpg)})
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        try:
            subprocess.run(['gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase', '',
                            '--quick-generate-key', 'Sentinel disposable test <test@example.invalid>',
                            'rsa2048', 'sign', '1d'], check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            if b'No agent running' in (exc.stderr or b''):
                raise unittest.SkipTest('GPG agent sockets unavailable; run signing tests on the build host.') from exc
            raise
        listing = subprocess.check_output(['gpg', '--batch', '--with-colons', '--list-keys'], stderr=subprocess.DEVNULL).decode()
        cls.fingerprint = next(line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:'))
        cls.config = cls.root / 'updates'
        repository_config.configure('https://updates.example.invalid/sentinel', cls.fingerprint, cls.config)
        cls.debs = cls.root / 'debs'
        cls.debs.mkdir()
        packages = [fixture_deb(cls.debs, name) for name in sorted(common.PACKAGES)]
        (cls.debs / 'SHA256SUMS').write_text(''.join(common.digest(p) + '  ' + p.name + '\n' for p in packages))
        cls.platform = fixture_deb(cls.root, 'sentinel-platform', arch='amd64')
        cls.repo = cls.root / 'repo'
        publish_repository.build_repository(cls.debs, cls.platform, cls.fingerprint, cls.repo)

    def test_public_key_pinning_and_source_tamper_rejected(self):
        metadata = repository_config.verify_configuration(self.config, self.fingerprint)
        self.assertEqual(metadata['suite'], 'sentinel-beta')
        with self.assertRaisesRegex(ValueError, 'independently'):
            repository_config.verify_configuration(self.config, '0' * 40)
        bad = self.root / 'bad-config'
        shutil.copytree(self.config, bad)
        with (bad / 'sentinel.sources').open('a') as stream:
            stream.write('Trusted: yes\n')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            repository_config.verify_configuration(bad)

    def test_https_only_repository_configuration(self):
        for url in ('http://example.invalid', 'https://user:password@example.invalid',
                    'https://example.invalid/?override=1', 'https://example.invalid/\nTrusted: yes'):
            with self.assertRaises(ValueError):
                repository_config.validate_url(url)

    def apt_root(self, name, repo):
        root = self.root / name
        for directory in ('lists/partial', 'archives/partial', 'sourceparts', 'preferencesparts'):
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / 'status').touch()
        # file: is test-only, not accepted by production repository_config.
        key = self.config / 'sentinel-archive-keyring.gpg'
        (root / 'sources.list').write_text(f'deb [arch=amd64 signed-by={key}] file:{repo} sentinel-beta main\n')
        (root / 'preferences').write_text(repository_config.preference_text())
        # Prevent host APT hooks and configuration from being loaded.
        apt_config = root / 'apt.conf'
        apt_config.write_text(f'Dir::Etc::parts "{root / "sourceparts"}";\nDir::Etc::main "{root / "no-main"}";\n')
        self.enterContext(patch.dict(os.environ, {'APT_CONFIG': str(apt_config)}))
        return ['apt-get', '-o', 'APT::Sandbox::User=root', '-o', 'APT::Update::Error-Mode=any',
                '-o', 'Dir::Etc::sourcelist=' + str(root / 'sources.list'),
                '-o', 'Dir::Etc::sourceparts=' + str(root / 'sourceparts'),
                '-o', 'Dir::Etc::preferences=' + str(root / 'preferences'),
                '-o', 'Dir::Etc::preferencesparts=' + str(root / 'preferencesparts'),
                '-o', 'Dir::State::status=' + str(root / 'status'),
                '-o', 'Dir::State::lists=' + str(root / 'lists'),
                '-o', 'Dir::Cache::archives=' + str(root / 'archives'),
                '-o', 'Dir::Cache::pkgcache=', '-o', 'Dir::Cache::srcpkgcache=',
                '-o', 'APT::Get::List-Cleanup=0']

    def test_apt_accepts_signed_indexes_and_exposes_candidates(self):
        command = self.apt_root('apt-valid', self.repo)
        subprocess.run([*command, 'update'], check=True, capture_output=True)
        # A simulation resolves all six fixture packages without installing anything.
        simulation = subprocess.run([*command, '--simulate', 'install', 'sentinel-platform', 'sentinel'],
                                    text=True, capture_output=True, check=True)
        self.assertIn('0.1.0-8', simulation.stdout)
        self.assertIn('sentinel-platform', simulation.stdout)
        suite = self.repo / 'dists/sentinel-beta'
        release = (suite / 'Release').read_text()
        self.assertIn('Valid-Until:', release)
        self.assertIn('Acquire-By-Hash: yes', release)
        for name in ('Packages', 'Packages.gz'):
            path = suite / 'main/binary-amd64' / name
            self.assertEqual(path.read_bytes(), (path.parent / 'by-hash/SHA256' / common.digest(path)).read_bytes())

    def test_apt_rejects_tampered_signed_metadata(self):
        bad = self.root / 'tampered-repo'
        shutil.copytree(self.repo, bad)
        path = bad / 'dists/sentinel-beta/InRelease'
        path.write_bytes(path.read_bytes().replace(b'Origin: Sentinel', b'Origin: Tampered'))
        result = subprocess.run([*self.apt_root('apt-invalid', bad), 'update'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_repository_retains_previous_pool_and_rejects_version_reuse(self):
        second = self.root / 'repo-second'
        publish_repository.build_repository(self.debs, self.platform, self.fingerprint, second, self.repo)
        self.assertEqual(sorted(p.name for p in (self.repo / 'pool/main/s/sentinel').glob('*.deb')),
                         sorted(p.name for p in (second / 'pool/main/s/sentinel').glob('*.deb')))
        changed = self.root / 'changed'
        changed.mkdir()
        altered = fixture_deb(changed, 'sentinel-platform', arch='amd64', content='different')
        with self.assertRaisesRegex(ValueError, 'Never replace'):
            publish_repository.build_repository(self.debs, altered, self.fingerprint,
                                                 self.root / 'repo-conflict', self.repo)

    def test_wrong_platform_version_is_rejected(self):
        wrong = self.root / 'wrong'
        wrong.mkdir()
        package = fixture_deb(wrong, 'sentinel-platform', version='0.1.0-9', arch='amd64')
        with self.assertRaisesRegex(ValueError, 'match'):
            publish_repository.build_repository(self.debs, package, self.fingerprint, self.root / 'never-created')
        self.assertFalse((self.root / 'never-created').exists())

    def test_platform_payload_has_no_installed_credentials_or_user_state(self):
        project = Path(__file__).resolve().parents[3]
        source = self.root / 'project'
        source.mkdir()
        for name in ('packaging', 'sen', 'sentinel', 'assets'):
            (source / name).symlink_to(project / name, target_is_directory=True)
        for name in ('LICENSE', 'NOTICE.md', 'THIRD_PARTY.md'):
            shutil.copy2(project / name, source / name)
        shutil.copytree(self.config, source / 'configs/updates')
        kit = self.root / 'fixture-kit'
        # A complete manifest fixture, not a production vendor download.
        for name, content in (('vendor/AdGuardHome', b'fixture'), ('vendor/starship', b'fixture'),
                              ('vendor/AdGuardHome-LICENSE.txt', b'license'),
                              ('vendor/starship-LICENSE.txt', b'license'),
                              ('vendor/fonts/test.ttf', b'font')):
            path = kit / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
        shutil.copytree(project / 'packaging/iso/systemd', kit / 'packaging/iso/systemd')
        for _, (folder, prefix) in common.WALLPAPERS.items():
            for size in common.RESOLUTIONS:
                path = kit / 'assets/Screensavers' / folder / f'{prefix}-{size}.png'
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture')
        manifest = {'format_version': 1, 'target': 'kubuntu-26.04-amd64',
                    'sha256': {p.relative_to(kit).as_posix(): common.digest(p) for p in kit.rglob('*') if p.is_file()}}
        (kit / 'kit-manifest.json').write_text(json.dumps(manifest))
        previous_umask = os.umask(0o002)
        try:
            package = platform_package.build_platform(source, kit, self.debs, self.root / 'platform-output')
        finally:
            os.umask(previous_umask)
        payload = subprocess.check_output(['dpkg-deb', '--fsys-tarfile', str(package)])
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            for member in archive:
                self.assertEqual((member.uid, member.gid), (0, 0), member.name)
                if member.isdir():
                    self.assertEqual(member.mode & 0o7777, 0o755, member.name)
                else:
                    self.assertEqual(member.mode & 0o022, 0, member.name)
        extracted = self.root / 'platform-payload'
        subprocess.run(['dpkg-deb', '--extract', str(package), str(extracted)], check=True)
        for name in ('LICENSE', 'NOTICE.md', 'THIRD_PARTY.md'):
            self.assertEqual((extracted / 'usr/share/doc/sentinel-platform' / name).read_bytes(),
                             (project / name).read_bytes())
        self.assertIn('GPL-3.0-only',
                      (extracted / 'usr/share/doc/sentinel-platform/copyright').read_text())
        self.assertTrue((extracted / 'usr/lib/systemd/system/sentinel-dns.service').is_file())
        self.assertTrue((extracted / 'usr/share/keyrings/sentinel-archive-keyring.gpg').is_file())
        self.assertTrue((extracted / 'usr/share/calamares/branding/sentinel/branding.desc').is_file())
        self.assertFalse((extracted / 'etc/sentinel').exists())
        self.assertFalse((extracted / 'etc/skel').exists())
        self.assertFalse((extracted / 'home').exists())
        self.assertIn('Font=JetBrainsMono Nerd Font,11,',
                      (extracted / 'usr/share/sentinel/desktop/Terminal/Konsole/Sentinel.profile').read_text())


class PreservationTests(unittest.TestCase):
    def test_policy_additions_preserve_existing_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy.json'
            inputs = Path(folder) / 'inputs.json'
            path.write_text(json.dumps({'protected': ['sentinel-core'], 'allowed_install': ['git'], 'blocked_install': ['firefox']}))
            inputs.write_text(json.dumps({'approved_apt': ['neovim'], 'blocked_browsers': ['chromium']}))
            with patch.object(platform_postinst, 'protected', side_effect=Path):
                platform_postinst.update_policy(path, inputs)
            updated = json.loads(path.read_text())
            self.assertEqual(updated['protected'], ['sentinel-core', 'sentinel-platform'])
            self.assertEqual(updated['allowed_install'], ['git', 'neovim'])
            self.assertEqual(updated['blocked_install'], sorted(['chromium', 'firefox',
                                                               *platform_postinst.DISCOVER_PACKAGES]))
