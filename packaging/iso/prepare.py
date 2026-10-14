"""Create a checked, minimal Cubic input kit as the ordinary developer user."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import urllib.parse
import urllib.request
import zipfile

from common import PACKAGES, REQUIRED_ASSETS, WALLPAPERS, RESOLUTIONS, digest, plain_file, verify_kit


def source_constant(path, name):
    for node in ast.parse(plain_file(path).read_text()).body:
        names = [t.id for t in node.targets if isinstance(t, ast.Name)] if isinstance(node, ast.Assign) else ([node.target.id] if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) else [])
        if name in names:
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == 'frozenset':
                value = value.args[0]
            return ast.literal_eval(value)
    raise ValueError(f'Missing reviewed source constant: {name}')


def deb_fields(path):
    fields = {}
    for field in ('Package', 'Version', 'Architecture'):
        fields[field] = subprocess.check_output(['dpkg-deb', '-f', str(path), field], text=True).strip()
    return fields


def validate_debs(directory):
    packages = {}
    versions = set()
    for path in sorted(Path(directory).glob('*.deb')):
        plain_file(path)
        fields = deb_fields(path)
        name = fields['Package']
        if name not in PACKAGES or name in packages:
            raise ValueError(f'Unexpected/duplicate package: {path}')
        if fields['Architecture'] not in {'amd64', 'all'}:
            raise ValueError(f'Wrong architecture: {path}')
        versions.add(fields['Version'])
        packages[name] = path
    if set(packages) != PACKAGES or len(versions) != 1:
        raise ValueError('Supply one successful build directory with all five packages at the same version.')
    if (Path(directory) / 'VALIDATION_FAILED.txt').exists():
        raise ValueError('This package build failed validation.')
    sums = plain_file(Path(directory) / 'SHA256SUMS')
    expected = {}
    for line in sums.read_text().splitlines():
        checksum, filename = line.split('  ', 1)
        if filename in expected:
            raise ValueError('Duplicate retained package checksum.')
        expected[filename] = checksum
    for path in packages.values():
        if expected.get(path.name) != digest(path):
            raise ValueError(f'Package no longer matches its successful build checksum: {path}')
    return packages, versions.pop()


def validate_png(path, size):
    with plain_file(path).open('rb') as stream:
        header = stream.read(24)
    if header[:8] != b'\x89PNG\r\n\x1a\n' or struct.unpack('>II', header[16:24]) != tuple(map(int, size.split('x'))):
        raise ValueError(f'Wallpaper has unexpected dimensions: {path}')


def download(asset, cache):
    url = urllib.parse.urlsplit(asset['url'])
    if url.scheme != 'https' or url.hostname != 'github.com':
        raise ValueError('Only locked upstream GitHub release assets are supported.')
    target = cache / asset['filename']
    if target.exists() and (target.stat().st_size != asset['size'] or digest(target) != asset['sha256']):
        raise ValueError(f'Cached download does not match vendor-lock.json: {target}')
    if not target.exists():
        request = urllib.request.Request(asset['url'], headers={'User-Agent': 'Sentinel-ISO/0.1'})
        temp = target.with_suffix(target.suffix + '.part')
        try:
            with urllib.request.urlopen(request, timeout=60) as response, temp.open('wb') as stream:
                shutil.copyfileobj(response, stream)
            if temp.stat().st_size != asset['size'] or digest(temp) != asset['sha256']:
                raise ValueError(f'Upstream asset checksum mismatch: {asset["filename"]}')
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
    return target


def extract_one(archive, member, destination):
    # Never extract a whole archive or follow archive-created symlinks.
    with tarfile.open(archive) as stream:
        matches = [i for i in stream.getmembers() if Path(i.name).as_posix() == member]
        if len(matches) != 1 or not matches[0].isfile():
            raise ValueError(f'Expected regular archive member: {member}')
        info = matches[0]
        with stream.extractfile(info) as source, Path(destination).open('wb') as target:
            shutil.copyfileobj(source, target)


def prepare(project, debs, output, platform=None):
    if os.geteuid() == 0:
        raise ValueError('Prepare the kit as your normal user, without sudo.')
    packages, version = validate_debs(debs)
    project, output = Path(project).resolve(), Path(output).resolve()
    if (project / 'configs/updates').exists() and platform is None:
        raise ValueError('Repository configured: supply the matching --platform DEB so this ISO can update its platform.')
    if platform is not None:
        platform = plain_file(Path(platform).expanduser())
        if deb_fields(platform) != {'Package': 'sentinel-platform', 'Version': version, 'Architecture': 'amd64'}:
            raise ValueError('Supply a sentinel-platform DEB matching the core package version and architecture.')
    assets = project / 'assets'
    for name in REQUIRED_ASSETS:
        plain_file(assets / name)
    for folder, prefix in WALLPAPERS.values():
        for size in RESOLUTIONS:
            validate_png(assets / 'Screensavers' / folder / f'{prefix}-{size}.png', size)
    if output.exists():
        raise ValueError(f'Use a new output directory; this path already exists: {output}')
    output.mkdir(parents=True)
    cache = project / 'dist/iso/vendor-cache'
    cache.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project / 'build.sh', output / 'build.sh')
    shutil.copytree(project / 'packaging/iso', output / 'packaging/iso',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'tests'))
    approved = source_constant(project / 'sen/install/packages.py', 'APPROVED_PACKAGES')
    allowed = sorted({p for d in approved.values() if d['type'] == 'apt' for p in d['packages']} | {'neovim', 'unityhub', 'reaper'})
    blocked = sorted(source_constant(project / 'sentinel/browser_policies/restrictions.py', 'BROWSER_PACKAGES'))
    (output / 'packaging/iso/package-policy-input.json').write_text(json.dumps({'approved_apt': allowed, 'blocked_browsers': blocked}, indent=2) + '\n')
    for name in REQUIRED_ASSETS:
        target = output / 'assets' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(assets / name, target)
    for folder, prefix in WALLPAPERS.values():
        target = output / 'assets/Screensavers' / folder
        target.mkdir(parents=True)
        for size in RESOLUTIONS:
            shutil.copy2(assets / 'Screensavers' / folder / f'{prefix}-{size}.png', target)
    (output / 'debs').mkdir()
    for path in packages.values():
        shutil.copy2(path, output / 'debs' / path.name)
    (output / 'debs/SHA256SUMS').write_text(''.join(
        digest(path) + '  ' + path.name + '\n' for path in sorted((output / 'debs').glob('*.deb'))))
    if platform is not None:
        (output / 'platform').mkdir()
        shutil.copy2(platform, output / 'platform' / platform.name)
    vendor = output / 'vendor'
    vendor.mkdir()
    lock = json.loads((project / 'packaging/iso/vendor-lock.json').read_text())
    for key, asset in lock['assets'].items():
        print(f'Checking locked {key} {asset["version"]}...', flush=True)
        archive = download(asset, cache)
        if key == 'adguardhome':
            extract_one(archive, 'AdGuardHome/AdGuardHome', vendor / 'AdGuardHome')
            extract_one(archive, 'AdGuardHome/LICENSE.txt', vendor / 'AdGuardHome-LICENSE.txt')
        elif key == 'starship':
            extract_one(archive, 'starship', vendor / 'starship')
        elif key == 'fastfetch':
            fields = deb_fields(archive)
            if fields['Package'] != 'fastfetch' or fields['Architecture'] != 'amd64':
                raise ValueError('Unexpected Fastfetch package metadata.')
            shutil.copy2(archive, vendor / 'fastfetch.deb')
        elif key == 'starship-license':
            shutil.copy2(archive, vendor / 'starship-LICENSE.txt')
        elif key == 'nerd-fonts':
            (vendor / 'fonts').mkdir()
            with zipfile.ZipFile(archive) as stream:
                for style in ('Regular', 'Bold', 'Italic', 'BoldItalic'):
                    name = f'JetBrainsMonoNerdFont-{style}.ttf'
                    matches = [n for n in stream.namelist() if Path(n).name == name]
                    if len(matches) != 1:
                        raise ValueError(f'Missing/ambiguous Nerd Font: {name}')
                    (vendor / 'fonts' / name).write_bytes(stream.read(matches[0]))
                licenses = [n for n in stream.namelist() if 'license' in Path(n).name.lower() or Path(n).name.lower() == 'ofl.txt']
                if not licenses:
                    raise ValueError('Nerd Font archive lacks its license.')
                for i, name in enumerate(licenses):
                    (vendor / 'fonts' / f'LICENSE-{i}.txt').write_bytes(stream.read(name))
    manifest = {'format_version': 1, 'target': 'kubuntu-26.04-amd64', 'package_version': version,
                'vendor_releases': lock['assets'], 'sha256': {p.relative_to(output).as_posix(): digest(p)
                    for p in sorted(output.rglob('*')) if p.is_file()}}
    (output / 'kit-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    verify_kit(output)
    print(f'Prepared and verified: {output}')
    print('Copy this directory into Cubic as /root/sentinel-iso-kit, then run its build.sh image.')
