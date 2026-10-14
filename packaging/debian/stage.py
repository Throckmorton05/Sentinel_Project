#!/usr/bin/env python3
"""Explicit allowlists for Debian payloads; never copy a checkout wholesale."""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ('sentinel-core', 'sentinel-assets', 'sentinel-models',
            'sentinel-desktop', 'sentinel')
MODEL_FILES = {
    'sentinel/image_blocking/models/openNSFW2_ONNX_Model/open_nsfw.onnx':
        'openNSFW2_ONNX_Model/open_nsfw.onnx',
    'sentinel/image_blocking/models/anime_real_cls/mobilenetv3_v1.4_dist/model.onnx':
        'anime_real_cls/mobilenetv3_v1.4_dist/model.onnx',
    'packaging/models/hentai-vit/model.onnx': 'hentai-vit/model.onnx',
}
EXTENSIONS = {
    'safe_steam': ('safe-steam', 'browser_extension.crx'),
    'safe_crunchyroll': ('safe-crunchyroll', 'safe_crunchyroll.crx'),
    'activity': ('activity', 'sentinel_activity.crx'),
    'extension_catalog': ('catalog', 'sentinel_extensions.crx'),
    'newtab': ('newtab', 'sentinel_newtab.crx'),
}
DNS_SEEDS = ('adult.txt', 'gambling.txt', 'social_media.txt',
             'browsers_sans_safesearch.txt')


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def require_file(path: Path) -> Path:
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise ValueError(f'Missing, empty, or symlinked build input: {path}')
    return path


def validate_model(path: Path):
    require_file(path)
    with path.open('rb') as stream:
        if stream.read(160).startswith(b'version https://git-lfs.github.com/spec/'):
            raise ValueError(f'Model is a Git LFS pointer, not model bytes: {path}; run git lfs pull')
    if path.stat().st_size < 1_000_000:
        raise ValueError(f'Model appears truncated: {path}')


def validate_wheel(path: Path):
    with zipfile.ZipFile(require_file(path)) as archive:
        for name in archive.namelist():
            parts = Path(name).parts
            if (Path(name).is_absolute() or '..' in parts or
                    Path(name).suffix.lower() in {'.pem', '.key'} or
                    'tests' in parts or '__pycache__' in parts or
                    name.endswith(('.pyc', '.onnx', '.safetensors'))):
                raise ValueError(f'Unexpected/private file in project wheel: {name}')
        needed = {
            'sentinel/daemon/server.py', 'sen/cli/sen.py',
            'sentinel/image_blocking/screen_blocker/screen_time_blocker.qml',
            'sentinel/image_blocking/screen_blocker/tamper_blocker.qml',
            'sentinel/safe_steam/steam_extension_tests/manifest.json',
            'sentinel/safe_steam/provisioning/safesteam-hashes.json',
            'sentinel/safe_steam/browser_distribution/browser_extension.crx',
        }
        missing = needed - set(archive.namelist())
        if missing:
            raise ValueError(f'Project wheel lacks required runtime assets: {sorted(missing)}')


def check_sources(root: Path = ROOT):
    project = tomllib.loads((root / 'pyproject.toml').read_text())
    version = project['project']['version']
    changelog = (root / 'debian/changelog').read_text().splitlines()[0]
    if not changelog.startswith(f'sentinel ({version}-'):
        raise ValueError('debian/changelog upstream version must match pyproject.toml')
    for source in MODEL_FILES:
        validate_model(root / source)
    for component, (_, filename) in EXTENSIONS.items():
        directory = root / 'sentinel' / component / 'browser_distribution'
        crx = require_file(directory / filename)
        with crx.open('rb') as stream:
            if stream.read(8) != b'Cr24\x03\x00\x00\x00':
                raise ValueError(f'Expected a pre-signed CRX3 file: {crx}')
        tree = ElementTree.parse(require_file(directory / 'updates.xml'))
        app = tree.getroot().find('{http://www.google.com/update2/response}app')
        update = app.find('{http://www.google.com/update2/response}updatecheck') if app is not None else None
        if update is None or not update.get('codebase', '').endswith('/' + filename):
            raise ValueError(f'Extension update URL does not match {filename}')
        if component in {'extension_catalog', 'newtab'}:
            json.loads(require_file(directory / 'metadata.json').read_text())
    bundle = root / 'sentinel/safe_steam'
    hashes = json.loads(require_file(bundle / 'provisioning/safesteam-hashes.json').read_text())
    if set(hashes) != {'manifest.json', 'content.js', 'background.js', 'policy.js'}:
        raise ValueError('Incomplete Safe Steam integrity manifest')
    for filename, expected in hashes.items():
        actual = hashlib.sha256(require_file(bundle / 'steam_extension_tests' / filename).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f'Safe Steam integrity mismatch: {filename}')
    for filename in DNS_SEEDS:
        require_file(root / 'sentinel/dns/data' / filename)
    for source in ('configs/approved_apps.toml', 'configs/browser/approved_extensions.toml',
                   'configs/settings.toml', 'assets/Sentinel_Shield.svg', 'assets/sentinel-appstore.png',
                   'sentinel/portal/assets/Sentinel_Shield.svg'):
        require_file(root / source)
    return version


def copy_file(source: Path, destination: Path, mode=0o644):
    require_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def install_tree(source: Path, destination: Path):
    for path in sorted(source.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Symlink in packaged source assets: {path}')
        if path.is_file():
            if path.suffix.lower() in {'.pem', '.key'}:
                raise ValueError(f'Private signing key in packaged assets: {path}')
            copy_file(path, destination / path.relative_to(source))


def package_roots(root: Path, output: Path):
    result = {name: output / name for name in PACKAGES}
    for path in result.values():
        path.mkdir(parents=True, exist_ok=True)
    return result


def install_payloads(root: Path, output: Path):
    """Public/data payloads and system integration, independent of pip."""
    packages = package_roots(root, output)
    core, assets, models, desktop = (packages[name] for name in PACKAGES[:4])
    share = assets / 'usr/share/sentinel'
    for source, destination in {
        'configs/approved_apps.toml': 'approved_apps.toml',
        'configs/browser/approved_extensions.toml': 'browser/approved_extensions.toml',
        'configs/dns/adult_heavy_sites.txt': 'dns/adult_heavy_sites.txt',
        'configs/dns/unsafe_search_engines.txt': 'dns/unsafe_search_engines.txt',
        'configs/settings.toml': 'defaults/settings.toml',
        'assets/Sentinel_Shield.svg': 'assets/Sentinel_Shield.svg',
        'sentinel/portal/assets/Sentinel_Shield.svg': 'assets/sentinel-portal.svg',
        'assets/sentinel-appstore.png': 'assets/sentinel-appstore.png',
    }.items():
        copy_file(root / source, share / destination)
    defaults = root / 'packaging/debian/defaults'
    for filename in ('blocked_apps.toml', 'info.toml', 'adguard.toml.example'):
        copy_file(defaults / filename, share / 'defaults' / filename)
    copy_file(defaults / 'info.toml', share / 'info.toml')
    for filename in ('user_allowlist.txt', 'user_blocklist.txt'):
        copy_file(defaults / filename, share / 'defaults/dns' / filename)
    for filename in DNS_SEEDS:
        copy_file(root / 'sentinel/dns/data' / filename, share / 'defaults/dns-seed' / filename)
    for component, (destination, filename) in EXTENSIONS.items():
        directory = root / 'sentinel' / component / 'browser_distribution'
        names = [filename, 'updates.xml']
        if component in {'extension_catalog', 'newtab'}:
            names.append('metadata.json')
        for name in names:
            copy_file(directory / name, share / 'extensions' / destination / name)
    for source, destination in MODEL_FILES.items():
        copy_file(root / source, models / 'usr/share/sentinel/models' / destination)
    for filename in ('config.json', 'preprocessor_config.json'):
        copy_file(root / 'packaging/models/hentai-vit' / filename,
                  models / 'usr/share/sentinel/models/hentai-vit' / filename)
    for package in packages.values():
        doc = package / 'usr/share/doc' / package.name
        copy_file(root / 'LICENSE', doc / 'LICENSE')
        copy_file(root / 'NOTICE.md', doc / 'NOTICE.md')
        copy_file(root / 'THIRD_PARTY.md', doc / 'THIRD_PARTY.md')
        install_tree(root / 'licenses', doc / 'licenses')
    copy_file(root / 'sentinel/dns/data/LICENSE-hagezi.txt',
              assets / 'usr/share/doc/sentinel-assets/LICENSE-hagezi.txt')
    for unit in (root / 'packaging/systemd').glob('*'):
        if unit.suffix in {'.service', '.socket'}:
            copy_file(unit, core / 'usr/lib/systemd/system' / unit.name)
    dropin = core / 'usr/lib/systemd/system/sentineld.service.d/10-package-setup.conf'
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text('[Unit]\nConditionPathExists=/etc/sentinel/daemon.toml\n')
    for filename in ('package_lifecycle.py', 'setup.py'):
        copy_file(root / 'packaging/debian' / filename, core / 'usr/lib/sentinel' / filename, 0o755)
    bin_dir = core / 'usr/bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / 'sentinel-setup').write_text('#!/bin/sh\nexec /usr/bin/python3.14 -I -B /usr/lib/sentinel/setup.py "$@"\n')
    (bin_dir / 'sentinel-setup').chmod(0o755)
    install_tree(root / 'sentinel/wayland/kwin_script',
                 desktop / 'usr/share/kwin/scripts/sentinel-window-bridge')
    applications = desktop / 'usr/share/applications'
    applications.mkdir(parents=True, exist_ok=True)
    entries = {
        'sentinel-getting-started': ('Start Here — Sentinel', 'sentinel.portal.main --guide', 'Documentation;Utility;', 'sentinel-portal.svg'),
        'sentinel-portal': ('Sentinel Portal', 'sentinel.portal.main', 'Settings;Security;', 'sentinel-portal.svg'),
        'sentinel-appstore': ('Sentinel App Store', 'sentinel.appstore.main', 'System;', 'sentinel-appstore.png'),
    }
    for name, (label, module, categories, icon) in entries.items():
        (applications / f'{name}.desktop').write_text(
            f'[Desktop Entry]\nType=Application\nName={label}\n'
            f'Exec=/usr/lib/sentinel/venv/bin/python -I -B -m {module}\n'
            f'Icon=/usr/share/sentinel/assets/{icon}\nTerminal=false\nCategories={categories}\n')
        if name == 'sentinel-portal':
            with (applications / f'{name}.desktop').open('a') as stream:
                stream.write('StartupWMClass=sentinel-portal\n')
    # Guardian's allow-only inventory requires this fixed independent recovery
    # entry. Keep it hidden; ordinary management now belongs to the Portal.
    (applications / 'sentinel-guardian-access.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=Sentinel recovery\n'
        'Exec=/usr/lib/sentinel/venv/bin/python -I -B -m sentinel.guardian_apps.manager\n'
        'Icon=/usr/share/sentinel/assets/Sentinel_Shield.svg\n'
        'NoDisplay=true\nTerminal=false\nCategories=Settings;Security;\n')
    (applications / 'sentinel-screen-monitor.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=Sentinel Screen Monitor\n'
        'Exec=/usr/lib/sentinel/venv/bin/sentinel-screen-monitor-python -I -B -m sentinel.image_blocking.screen_monitor.monitor\n'
        'NoDisplay=true\nTerminal=false\n'
        # KService reads this custom property through KConfig's comma-separated
        # QStringList conversion, not the standard semicolon-separated XDG list.
        'X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2\n')
    return packages


def elf_dynamic(path: Path):
    text = subprocess.check_output(['readelf', '-d', str(path)], text=True)
    needed = re.findall(r'\(NEEDED\).*?\[([^\]]+)\]', text)
    search = re.findall(r'\((RPATH|RUNPATH)\).*?\[([^\]]*)\]', text)
    return needed, search


def is_elf(path: Path):
    with path.open('rb') as stream:
        return stream.read(4) == b'\x7fELF'


def normalize_elf_search_paths(venv: Path):
    """Remove stale upstream build paths while preserving private $ORIGIN paths."""
    changed = []
    for path in sorted(venv.rglob('*')):
        if path.is_symlink() or not path.is_file() or not is_elf(path):
            continue
        needed, search = elf_dynamic(path)
        for kind, original in search:
            retained = []
            for entry in original.split(':'):
                if not entry:
                    continue
                if entry.startswith('/opt/_internal/'):
                    continue  # manylinux build-machine path, absent on target
                if not entry.startswith(('$ORIGIN', '${ORIGIN}')):
                    raise ValueError(f'Unexpected ELF search path {entry!r}: {path}')
                relative = entry.replace('${ORIGIN}', '.').replace('$ORIGIN', '.')
                if '$' in relative or not (path.parent / relative).resolve().is_relative_to(venv.resolve()):
                    raise ValueError(f'ELF search path escapes private environment: {path}: {entry}')
                retained.append(entry)
            replacement = ':'.join(retained)
            if replacement == original and original:
                continue
            if replacement:
                args = ['patchelf', '--set-rpath', replacement]
                if kind == 'RPATH':
                    args.insert(1, '--force-rpath')
                run(*args, path)
            else:
                run('patchelf', '--remove-rpath', path)
            after_needed, after_search = elf_dynamic(path)
            if after_needed != needed or (replacement and (kind, replacement) not in after_search) or (
                    not replacement and after_search):
                raise ValueError(f'ELF relocation verification failed: {path}')
            changed.append({'file': path.relative_to(venv).as_posix(),
                            'kind': kind, 'before': original, 'after': replacement})
    report = venv.parent / 'elf-relocations.json'
    report.write_text(json.dumps(changed, indent=2) + '\n')
    return changed


def runtime_file_mode(path: Path, venv: Path):
    if path.is_dir():
        return 0o755
    if path.parent == venv / 'bin':
        return 0o755 if path.stat().st_mode & 0o111 else 0o644
    if re.search(r'\.so(?:\.|$)', path.name):
        return 0o644  # Some Qt DSOs also contain INTERP for version reporting.
    if is_elf(path):
        # PIE executables, unlike ordinary DSOs, carry a program interpreter.
        headers = subprocess.check_output(['readelf', '-l', str(path)], text=True)
        return 0o755 if re.search(r'^\s*INTERP\s', headers, re.MULTILINE) else 0o644
    return 0o644


def relocate_venv(venv: Path, prefix: str, interpreter: Path):
    """Avoid build-machine paths in entry points and pyvenv.cfg."""
    venv.chmod(0o755)
    for entry in (venv / 'bin').iterdir():
        if entry.is_symlink():
            if entry.name.startswith('python'):
                entry.unlink()
                entry.symlink_to(str(interpreter))
            continue
        if not entry.is_file():
            continue
        if entry.name.startswith('activate') or entry.name.startswith('pip'):
            entry.unlink()
            continue
        data = entry.read_bytes()
        if not data.startswith(b'#!'):
            continue
        lines = data.splitlines(keepends=True)
        # pip sometimes creates a /bin/sh trampoline for long/space-containing
        # venv paths; reject it instead of leaving a staging path behind.
        if lines[0].strip() == b'#!/bin/sh' and len(lines) > 2 and b"'''exec'" in lines[1]:
            lines = [f'#!{prefix}/bin/python -IB\n'.encode(), *lines[3:]]
        elif lines[0].startswith(b'#!') and b'python' in lines[0]:
            lines[0] = f'#!{prefix}/bin/python -IB\n'.encode()
        entry.write_bytes(b''.join(lines))
        entry.chmod(0o755)
    (venv / 'pyvenv.cfg').write_text(
        f'home = {interpreter.parent}\ninclude-system-site-packages = false\n'
        f'version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n'
        f'executable = {interpreter}\n')
    lib = venv / 'lib'
    for path in lib.rglob('direct_url.json'):
        path.unlink()
    for path in venv.rglob('__pycache__'):
        shutil.rmtree(path)
    (venv / '.gitignore').unlink(missing_ok=True)
    # Programs remain executable. Imported Python/data and shared libraries
    # are read-only data, even if the upstream wheel marked them executable.
    for path in venv.rglob('*'):
        if not path.is_symlink():
            path.chmod(runtime_file_mode(path, venv))


def install_runtime(core: Path, inputs: Path):
    prefix = '/usr/lib/sentinel/venv'
    venv = core / prefix.lstrip('/')
    wheelhouse = inputs / 'wheelhouse'
    wheels = list((inputs / 'project-wheel').glob('sen-*.whl'))
    if len(wheels) != 1:
        raise ValueError('Expected exactly one freshly built sen wheel')
    validate_wheel(wheels[0])
    python = Path('/usr/bin/python3.14')
    run(python, '-m', 'venv', venv)
    staged_python = venv / 'bin/python'
    requirements = require_file(inputs / 'requirements.txt')
    run(staged_python, '-I', '-m', 'pip', 'install', '--no-compile', '--no-index',
        '--no-deps', '--require-hashes', '--find-links', wheelhouse, '-r', requirements)
    run(staged_python, '-I', '-m', 'pip', 'install', '--no-compile', '--no-index',
        '--no-deps', wheels[0])
    run(staged_python, '-I', '-m', 'pip', 'check')
    # NudeDetector() uses the wheel's bundled default model. An empty model
    # cache must never cause first boot to download mutable executable inputs.
    nudenet = venv / 'lib/python3.14/site-packages/nudenet/320n.onnx'
    validate_model(nudenet)
    relocate_venv(venv, prefix, python)
    # KWin checks /proc/<pid>/exe. A shell script or symlink to ordinary Python
    # cannot give this process a distinct, narrowly registered executable path.
    copy_file(python.resolve(), venv / 'bin/sentinel-screen-monitor-python', 0o755)
    normalize_elf_search_paths(venv)
    run(staged_python, '-I', '-B', '-c',
        'import numpy as np; import scipy.linalg as la; '
        'from scipy.special import cython_special; '
        'from PIL import Image; import cv2; import onnxruntime; '
        'from PyQt6 import QtCore, QtGui, QtWidgets; '
        'assert np.allclose(la.solve(np.eye(2), np.array([1., 2.])), [1., 2.]); '
        'assert abs(cython_special.expit(0.) - .5) < 1e-12; '
        'assert Image.new("RGB", (2, 2)).size == (2, 2); '
        'print("Relocated runtime imports and scientific operations: OK")')
    # Public commands always isolate Python from CWD, PYTHONPATH and user sites.
    parser = configparser.ConfigParser(interpolation=None)
    with zipfile.ZipFile(wheels[0]) as archive:
        entry_points = next(name for name in archive.namelist() if name.endswith('.dist-info/entry_points.txt'))
        parser.read_string(archive.read(entry_points).decode())
    for name in parser['console_scripts']:
        if not name.replace('-', '').isalnum():
            raise ValueError(f'Unsafe console-script name: {name}')
        wrapper = core / 'usr/bin' / name
        wrapper.write_text(f'#!/bin/sh\nexec {prefix}/bin/{name} "$@"\n')
        wrapper.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('validate', 'install'))
    args = parser.parse_args()
    check_sources()
    inputs_text = os.environ.get('SENTINEL_BUILD_INPUT')
    if not inputs_text:
        raise ValueError('Build inputs are not prepared. Run bash tools/build_debs.sh instead.')
    inputs = Path(inputs_text)
    if not inputs.is_absolute():
        raise ValueError('SENTINEL_BUILD_INPUT must be absolute')
    manifest = json.loads(require_file(inputs / 'manifest.json').read_text())
    if manifest.get('lock_sha256') != hashlib.sha256((ROOT / 'poetry.lock').read_bytes()).hexdigest():
        raise ValueError('Prepared dependencies do not match poetry.lock; rebuild inputs')
    for name, expected in manifest['inputs_sha256'].items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe build manifest path')
        path = require_file(inputs / relative)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Prepared build input was modified: {name}')
    if args.action == 'install':
        packages = install_payloads(ROOT, ROOT / 'debian')
        install_runtime(packages['sentinel-core'], inputs)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Sentinel package staging failed: {exc}') from exc
