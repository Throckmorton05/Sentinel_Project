"""Change only menu-title text in Cubic's extracted ISO boot configuration."""
from pathlib import Path
import re

from common import write


def branded_text(text):
    # Never touch kernel arguments, paths, IDs, search labels or boot commands.
    pattern = re.compile(r'^(\s*menuentry\s+)([\"\'])(.*?)(\2)', re.M)
    def menu(match):
        title = re.sub(r'\bKubuntu\b(?: Linux)?', 'Sentinel Linux', match[3], flags=re.I)
        return match[1] + match[2] + title + match[4]
    text = pattern.sub(menu, text)
    return re.sub(r'^(\s*menu label\s+)(.*)$',
                  lambda m: m[1] + re.sub(r'\bKubuntu\b(?: Linux)?', 'Sentinel Linux', m[2], flags=re.I),
                  text, flags=re.M | re.I)


def brand_boot_tree(directory):
    directory = Path(directory).expanduser().resolve()
    if directory.name != 'custom-disk' or not (directory / 'casper').is_dir():
        raise ValueError('Pass the HOST Cubic project custom-disk directory containing casper/.')
    candidates = []
    for relative in ('boot/grub', 'isolinux', 'syslinux'):
        base = directory / relative
        if base.is_symlink():
            raise ValueError('Refusing symlinked ISO boot directory.')
        if base.is_dir():
            candidates.extend(base.rglob('*.cfg'))
    changes = []
    for path in sorted(candidates):
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError('Refusing boot config outside custom-disk.')
        original = path.read_text()
        replacement = branded_text(original)
        if original != replacement:
            changes.append((path, replacement))
    if not candidates:
        raise ValueError('No boot configuration found in custom-disk; use Cubic Options → Boot.')
    for path, content in changes:
        write(path, content, path.stat().st_mode & 0o777)
        print('Branded: ' + str(path))
    print(f'Checked {len(candidates)} boot configurations; changed {len(changes)}. Boot commands retained.')
