#!/usr/bin/env python3
"""Prepare a locked wheelhouse, build five Debian packages, and retain evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time

from stage import PACKAGES, ROOT, check_sources, validate_wheel


def run(*args, cwd=ROOT, env=None):
    print('+ ' + ' '.join(str(arg) for arg in args), flush=True)
    subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, check=True)


def target_environment():
    release = platform.freedesktop_os_release()
    if release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '26.04':
        raise ValueError('Build on Kubuntu/Ubuntu 26.04 amd64, matching the intended ISO base.')
    arch = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
    if arch != 'amd64':
        raise ValueError('This first package set targets amd64 only.')
    python = Path('/usr/bin/python3.14')
    if not python.is_file():
        raise ValueError('Install python3.14 and python3.14-venv first.')
    version = subprocess.check_output([str(python), '-I', '-c',
                                      'import sys; print(".".join(map(str, sys.version_info[:3])))'], text=True).strip()
    if not version.startswith('3.14.') or tuple(map(int, version.split('.'))) < (3, 14, 2):
        raise ValueError('The package build requires distro Python 3.14.2 or newer within 3.14.')
    for command in ('poetry', 'dpkg-buildpackage', 'dpkg-checkbuilddeps', 'dh',
                    'lintian', 'desktop-file-validate', 'patchelf', 'gcc', 'readelf'):
        if shutil.which(command) is None:
            raise ValueError(f'Missing build tool: {command}; see docs/debian-packaging.md')
    result = subprocess.run(['poetry', 'export', '--help'], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError('Install the Poetry exporter: poetry self add poetry-plugin-export')
    return python


def copy_source(destination: Path):
    """Work in an isolated source tree; never touch host runtime/service files."""
    def ignored(directory, names):
        return {name for name in names if name in {
            '.git', '.venv', '.pytest_cache', '__pycache__', '.mypy_cache',
            '.ruff_cache', 'dist', 'build', 'Sentinel_Logs', '.debhelper',
            'debhelper-build-stamp', 'files', 'tmp', *PACKAGES,
        } or name.endswith(('.pem', '.key', '.pyc', '.swp', '.substvars', '.debhelper'))}
    for filename in ('pyproject.toml', 'poetry.lock', 'README.md', 'LICENSE', 'NOTICE.md', 'THIRD_PARTY.md'):
        shutil.copyfile(ROOT / filename, destination / filename)
    for directory in ('sen', 'sentinel', 'configs', 'assets', 'licenses', 'packaging', 'debian', 'docs'):
        shutil.copytree(ROOT / directory, destination / directory, ignore=ignored)


def inspect_key_material(file: Path, root: Path):
    """Only the two validated Certifi public CA bundles may use a PEM suffix."""
    if file.suffix.lower() not in {'.pem', '.key'}:
        return
    site_packages = Path('usr/lib/sentinel/venv/lib/python3.14/site-packages')
    certificates = {site_packages / 'certifi/cacert.pem',
                    site_packages / 'pip/_vendor/certifi/cacert.pem'}
    if file.relative_to(root) not in certificates or file.is_symlink() or not file.is_file():
        raise ValueError(f'Private signing material in package: {file}')
    content = file.read_bytes()
    labels = re.findall(rb'-----BEGIN ([^-\r\n]+)-----', content)
    if not labels or any(label != b'CERTIFICATE' for label in labels):
        raise ValueError(f'Private or unexpected PEM material in CA bundle: {file}')
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cafile=str(file))
    except ssl.SSLError as exc:
        raise ValueError(f'Invalid public CA bundle: {file}') from exc


def inspect_packages(source: Path, work: Path):
    packages = sorted(work.glob('*.deb'))
    found = {}
    for path in packages:
        name = subprocess.check_output(['dpkg-deb', '-f', str(path), 'Package'], text=True).strip()
        if name in found:
            raise ValueError(f'Duplicate built package: {name}')
        found[name] = path
        root = source / 'debian' / name
        for file in root.rglob('*'):
            inspect_key_material(file, root)
            if 'usr/lib/sentinel/venv' not in str(file) and file.is_symlink():
                raise ValueError(f'Unexpected symlink in package: {file}')
        for desktop in root.rglob('*.desktop'):
            run('desktop-file-validate', desktop, cwd=source)
    if set(found) != set(PACKAGES):
        raise ValueError(f'Incomplete package output: expected {PACKAGES}, got {sorted(found)}')
    changes = list(work.glob('*.changes'))
    if len(changes) != 1:
        raise ValueError('Expected exactly one .changes build record')
    print('Lintian is checking the bundled runtime; this can take several minutes.', flush=True)
    run('lintian', '--fail-on', 'error', '--verbose', '--tag-display-limit', '0', changes[0], cwd=source)
    return packages


def retain_artifacts(source: Path, work: Path, inputs: Path, output: Path):
    """Keep completed build outputs before checks can fail and erase scratch."""
    output.mkdir()
    for path in sorted(work.iterdir()):
        if path.is_file() and path.suffix in {'.deb', '.changes', '.buildinfo'}:
            shutil.copyfile(path, output / path.name)
    for filename in ('manifest.json', 'requirements.txt'):
        shutil.copyfile(inputs / filename, output / filename)
    shutil.copyfile(source / 'poetry.lock', output / 'poetry.lock')
    for directory in ('wheelhouse', 'project-wheel'):
        shutil.copytree(inputs / directory, output / directory)
    sums = ''.join(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(output)}\n'
                   for path in sorted(output.rglob('*')) if path.is_file())
    (output / 'SHA256SUMS').write_text(sums)


def audit_and_retain(source: Path, work: Path, inputs: Path, final: Path):
    unchecked = final.with_name(final.name + '-unchecked')
    retain_artifacts(source, work, inputs, unchecked)
    try:
        packages = inspect_packages(source, work)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        (unchecked / 'VALIDATION_FAILED.txt').write_text(
            f'Build completed, but package validation failed. Do not install these packages.\n{exc}\n')
        raise ValueError(f'{exc}\nUnvalidated build artifacts retained at: {unchecked}') from exc
    unchecked.rename(final)
    return packages


def verify_retained_artifacts(output: Path):
    """Check every retained input/output, including every archive to be audited."""
    expected = {}
    for line in (output / 'SHA256SUMS').read_text().splitlines():
        digest, separator, name = line.partition('  ')
        relative = Path(name)
        if (not separator or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or relative.is_absolute() or '..' in relative.parts
                or name in expected or name in {'SHA256SUMS', 'VALIDATION_FAILED.txt'}):
            raise ValueError('Invalid retained SHA256SUMS record')
        expected[name] = digest
    actual = set()
    for path in output.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'Unexpected symlink in retained artifacts: {path}')
        if path.is_file() and path.relative_to(output).as_posix() not in {'SHA256SUMS', 'VALIDATION_FAILED.txt'}:
            actual.add(path.relative_to(output).as_posix())
    if not expected or actual != set(expected):
        raise ValueError('Retained files do not match SHA256SUMS')
    for name, digest in expected.items():
        if hashlib.sha256((output / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'Retained artifact checksum mismatch: {name}')


def audit_retained(output: Path):
    """Audit saved .debs directly without downloads, compilation, or installation."""
    final = output.with_name(output.name.removesuffix('-unchecked'))
    if final != output and final.exists():
        raise ValueError(f'Output already exists: {final}')
    verify_retained_artifacts(output)
    try:
        with tempfile.TemporaryDirectory(prefix='sentinel-audit-') as temporary:
            source = Path(temporary)
            (source / 'debian').mkdir()
            seen = set()
            for archive in sorted(output.glob('*.deb')):
                name = subprocess.check_output(
                    ['dpkg-deb', '-f', str(archive), 'Package'], text=True).strip()
                if name not in PACKAGES or name in seen:
                    raise ValueError(f'Unexpected or duplicate built package: {name}')
                seen.add(name)
                run('dpkg-deb', '-x', archive, source / 'debian' / name, cwd=output)
            packages = inspect_packages(source, output)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        (output / 'VALIDATION_FAILED.txt').write_text(
            f'Package validation failed. Do not install these packages.\n{exc}\n')
        raise ValueError(f'{exc}\nUnvalidated build artifacts retained at: {output}') from exc
    if final != output:
        output.rename(final)
    (final / 'VALIDATION_FAILED.txt').unlink(missing_ok=True)
    print(f'Checked {len(packages)} retained packages: {final}')


def reuse_wheelhouse(retained: Path, source: Path, requirements: Path, wheelhouse: Path):
    """Reuse only verified dependencies for the same lock and requirement export."""
    verify_retained_artifacts(retained)
    if (retained / 'poetry.lock').read_bytes() != (source / 'poetry.lock').read_bytes():
        raise ValueError('Saved wheelhouse uses a different poetry.lock; run a normal build')
    if (retained / 'requirements.txt').read_bytes() != requirements.read_bytes():
        raise ValueError('Saved wheelhouse uses a different requirement export; run a normal build')
    wheels = list((retained / 'wheelhouse').iterdir())
    if not wheels or any(not path.is_file() or path.suffix != '.whl' for path in wheels):
        raise ValueError('Saved wheelhouse must contain only dependency wheels')
    for path in wheels:
        shutil.copyfile(path, wheelhouse / path.name)
    print(f'Reusing {len(wheels)} verified dependency wheels from {retained}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true',
                      help='validate source inputs and target build tools without downloading or building')
    mode.add_argument('--audit', type=Path,
                      help='audit a retained build directory without rebuilding or installing')
    mode.add_argument('--reuse-inputs', type=Path,
                      help='rebuild with verified dependency wheels from a retained build')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/debs',
                        help='parent directory for a new versioned build output (default: dist/debs)')
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise ValueError('Run the build as your normal user, without sudo.')
    if args.audit is not None:
        audit_retained(args.audit.expanduser().resolve())
        return
    version = check_sources()
    python = target_environment()
    run('dpkg-checkbuilddeps')
    run('poetry', 'check', '--lock')
    if args.check:
        print(f'Sentinel {version}: source inputs and target build tools are ready.')
        return
    args.output = args.output.expanduser().resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    build_id = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    final = args.output / f'{version}-{build_id}'
    if final.exists() or final.with_name(final.name + '-unchecked').exists():
        raise ValueError(f'Output already exists: {final}')
    with tempfile.TemporaryDirectory(prefix='sentinel-debs-') as temporary:
        work = Path(temporary)
        source = work / 'source'
        source.mkdir()
        copy_source(source)
        inputs = work / 'inputs'
        inputs.mkdir()
        wheelhouse = inputs / 'wheelhouse'
        wheelhouse.mkdir()
        project_wheel = inputs / 'project-wheel'
        project_wheel.mkdir()
        requirements = inputs / 'requirements.txt'
        run('poetry', 'export', '--only', 'main', '--format', 'requirements.txt',
            '--output', requirements, cwd=source)
        run('poetry', 'build', '--format', 'wheel', '--output', project_wheel, cwd=source)
        wheels = list(project_wheel.glob('sen-*.whl'))
        if len(wheels) != 1:
            raise ValueError('Expected one freshly built project wheel')
        validate_wheel(wheels[0])
        if args.reuse_inputs is not None:
            reuse_wheelhouse(args.reuse_inputs.expanduser().resolve(), source, requirements, wheelhouse)
        else:
            builder = work / 'download-venv'
            run(python, '-m', 'venv', builder)
            run(builder / 'bin/python', '-I', '-m', 'pip', 'download', '--only-binary=:all:',
                '--no-deps', '--require-hashes', '-r', requirements, '--dest', wheelhouse, cwd=source)
        manifest = {
            'project_version': version, 'target': 'ubuntu-26.04-amd64',
            'lock_sha256': hashlib.sha256((source / 'poetry.lock').read_bytes()).hexdigest(),
            'inputs_sha256': {str(path.relative_to(inputs)): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in sorted(inputs.rglob('*')) if path.is_file()},
        }
        (inputs / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        env = {**os.environ, 'SENTINEL_BUILD_INPUT': str(inputs),
               'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}
        env.pop('PYTHONPATH', None)
        env.pop('PYTHONHOME', None)
        run('dpkg-buildpackage', '-us', '-uc', '-b', cwd=source, env=env)
        packages = audit_and_retain(source, work, inputs, final)
        print(f'Built and checked {len(packages)} packages: {final}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Sentinel Debian build stopped: {exc}') from exc
