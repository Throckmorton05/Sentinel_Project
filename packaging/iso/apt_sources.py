"""Disable only the known ISO-media source; preserve signed online repositories."""
from pathlib import Path
import re
from urllib.parse import urlsplit

from common import plain_file, write


def disabled_media_text(text):
    """Disable every deb822 stanza without disturbing embedded signing keys."""
    stanzas = re.split(r'\n[ \t]*\n', text.strip())
    result = []
    for stanza in stanzas:
        if not any(re.match(r'URIs:', line, re.I) for line in stanza.splitlines()):
            continue
        stanza = re.sub(r'^Enabled:[^\n]*\n?', '', stanza, flags=re.I | re.M)
        result.append('Enabled: no\n' + stanza)
    return '\n\n'.join(result) + '\n'


def stage_installer_media_source(source=Path('/etc/apt/sources.list.d/cdrom.sources')):
    """Keep Calamares' copy input present, but never enable the ISO repository."""
    source = Path(source)
    backup = source.with_name(source.name + '.disabled')
    candidate = source if source.exists() else backup
    if source.is_symlink() or backup.is_symlink():
        raise ValueError('Refusing symlinked ISO-media source.')
    if candidate.exists():
        text = plain_file(candidate).read_text()
        if not media_only_source(text):
            raise ValueError('cdrom.sources contains non-media repositories; refusing to change them.')
    else:
        # A complete disabled deb822 stanza, rather than an empty touch workaround.
        text = ('Types: deb\nURIs: file:///cdrom\nSuites: resolute\n'
                'Components: main restricted universe multiverse\n'
                'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
    write(source, disabled_media_text(text))


def media_only_source(text):
    """Check all folded URIs fields, including additional deb822 stanzas."""
    uris = []
    field = None
    for line in text.splitlines():
        if not line.strip():
            field = None
            continue
        if line.lstrip().startswith('#'):
            continue
        if line[:1].isspace():
            if field == 'uris':
                uris.extend(line.split())
            continue
        match = re.fullmatch(r'([A-Za-z][A-Za-z0-9-]*):\s*(.*)', line)
        if not match:
            raise ValueError('Unrecognized cdrom.sources format; review it before building.')
        field, value = match.group(1).lower(), match.group(2)
        if field == 'uris':
            uris.extend(value.split())
    def media(uri):
        parsed = urlsplit(uri)
        return (parsed.scheme == 'file' and parsed.netloc in {'', 'localhost'}
                and parsed.path.rstrip('/') == '/cdrom'
                and not parsed.query and not parsed.fragment)
    return bool(uris) and all(media(uri) for uri in uris)


def disable_media_source(source=Path('/etc/apt/sources.list.d/cdrom.sources')):
    source = Path(source)
    if not source.exists():
        if source.is_symlink():
            raise ValueError('Refusing symlinked ISO-media source.')
        return
    contents = plain_file(source).read_bytes()
    if not media_only_source(contents.decode()):
        raise ValueError('cdrom.sources contains non-media repositories; refusing to disable them.')
    disabled = source.with_name(source.name + '.disabled')
    if disabled.is_symlink():
        raise ValueError('Refusing symlinked disabled ISO-media source.')
    if disabled.exists() and plain_file(disabled).read_bytes() != contents and (
            disabled_media_text(plain_file(disabled).read_text()).encode() != contents):
        raise ValueError('A different saved cdrom.sources.disabled already exists; review it before building.')
    if disabled.exists():
        source.unlink()  # Keep the original build backup, including its inline key.
    else:
        source.replace(disabled)
    print('Disabled the unavailable /cdrom APT source; online repository authentication is unchanged.')


def media_cleanup_config():
    # Run inside the installed target, never against the live environment's APT
    # sources. Both an absent source and the disabled build copy are expected.
    return {'dontChroot': False, 'timeout': 30, 'verbose': False,
            'script': ['/bin/rm -f -- /etc/apt/sources.list.d/cdrom.sources',
                       '/bin/rm -f -- /etc/apt/sources.list.d/cdrom.sources.disabled']}
