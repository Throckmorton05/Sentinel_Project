#!/usr/bin/python3
"""Sentinel Cubic kit preparation and guarded image configuration."""
from __future__ import annotations

import argparse
from datetime import datetime
import os
import subprocess
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import require_image_environment, verify_kit


def main():
    project = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='normal user: stage assets, five .debs and locked vendor downloads')
    prepare.add_argument('--debs', required=True, type=Path)
    prepare.add_argument('--output', type=Path)
    prepare.add_argument('--platform', type=Path, help='matching sentinel-platform DEB for signed distribution updates')
    platform = commands.add_parser('platform', help='normal user: package ISO integration for APT updates')
    platform.add_argument('--kit', type=Path, required=True)
    platform.add_argument('--debs', type=Path, required=True)
    platform.add_argument('--output', type=Path, required=True)
    verify = commands.add_parser('verify', help='read-only: verify a prepared kit')
    verify.add_argument('kit', type=Path)
    commands.add_parser('image', help='root inside Cubic: configure the extracted image')
    commands.add_parser('repair-live', help='root inside Cubic: repair an existing Sentinel live image, without reinstalling packages')
    boot = commands.add_parser('boot-branding', help='host: brand menu titles in Cubic custom-disk')
    boot.add_argument('custom_disk', type=Path)
    args = parser.parse_args()
    if args.command == 'prepare':
        from prepare import prepare as prepare_kit
        output = args.output or project / 'dist/iso' / ('kit-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        prepare_kit(project, args.debs.expanduser(), output.expanduser(), args.platform)
    elif args.command == 'platform':
        from platform_package import build_platform
        build_platform(project, args.kit.expanduser(), args.debs.expanduser(), args.output.expanduser())
    elif args.command == 'verify':
        result = verify_kit(args.kit.expanduser())
        print('Verified complete ISO kit for package version ' + result['package_version'])
    elif args.command == 'image':
        require_image_environment()  # Must precede imports with side effects and all filesystem changes.
        verify_kit(project)
        from image import configure_image
        configure_image(project)
    elif args.command == 'repair-live':
        require_image_environment()
        if not Path('/usr/lib/sentinel-iso/image-built.json').is_file():
            raise ValueError('Repair requires an existing Sentinel image. Configure it with a COMPLETE kit first.')
        from live_repair import apply_live_fixes
        apply_live_fixes(project)
    elif args.command == 'boot-branding':
        from boot_branding import brand_boot_tree
        brand_boot_tree(args.custom_disk)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'Sentinel ISO setup stopped: {exc}') from exc
