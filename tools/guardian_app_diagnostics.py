#!/usr/bin/env python3
"""Read-only Guardian launch diagnostics. Never enable policy or start an app.

Run with the protected Python (-I -B) and sudo to inspect persisted root state.
Only stdout is written. Credentials, window titles and process environments are
not collected; only Sentinel's Firefox development flag is reported.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import subprocess
import sys
import tomllib
import urllib.request

sys.dont_write_bytecode = True

MODULES = ('sentinel.guardian_apps.desktop', 'sentinel.guardian_apps.launch',
           'sentinel.guardian_apps.manager', 'sentinel.guardian_apps.recovery',
           'sentinel.daemon.core.instance', 'sentinel.daemon.operations.guardian_app_ops',
           'sentinel.daemon.server')
SERVICES = ('sentineld.service', 'sentineld-dev.service', 'sentinel-guardian-recovery.socket', 'sentinel-opennsfw.service',
            'sentinel-drawn.service', 'sentinel-hentai.service', 'sentinel-nudenet.service')


def read_restrictions(source):
    """Read individual action keys, including KDE's immutable key/group suffix."""
    result = {}
    section = ''
    for line in source.splitlines():
        line = line.strip()
        header = re.fullmatch(r'\[([^\]]+)\](?:\[\$i\])?', line)
        if header:
            section = header[1]
        elif section == 'KDE Action Restrictions' and '=' in line and not line.startswith(('#', ';')):
            key, value = line.split('=', 1)
            result[key.removesuffix('[$i]').strip()] = value.strip().lower()
    return result


def launcher_authorizations(source, actions):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(source)
    required = parser['Desktop Entry'].get('X-KDE-AuthorizeAction', '')
    # KIO checks this globally even when the launcher declares no actions.
    required = ['run_desktop_files', *[action.strip() for action in required.split(',')]]
    return list(dict.fromkeys(action for action in required
                             if actions.get(action) in {'false', '0', 'no'}))


def _command(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
        return result.returncode, result.stdout, result.stderr.strip()[:600]
    except (OSError, subprocess.SubprocessError) as exc:
        return None, '', str(exc)


def service_status(unit):
    code, out, error = _command(['/usr/bin/systemctl', 'show', unit, '--no-pager',
        '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'UnitFileState',
        '-p', 'MainPID', '-p', 'ExecMainStatus', '-p', 'Transient', '-p', 'Environment'])
    props = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
    environment = props.pop('Environment', '')
    try:
        props['firefox_dev_exemption'] = 'SENTINEL_DEV_ALLOW_FIREFOX=1' in shlex.split(environment)
    except ValueError:
        props['firefox_dev_exemption'] = None
        props['environment_probe_error'] = 'Could not parse systemctl Environment output.'
    if code != 0:
        props['probe_error'] = error
    return props


def daemon_processes(services):
    import psutil
    managed = {int(data.get('MainPID') or 0): name for name, data in services.items()
               if name.startswith('sentineld')}
    found = []
    for proc in psutil.process_iter(['pid', 'uids', 'cmdline']):
        try:
            info = proc.info
            command = info.get('cmdline') or []
            if not info.get('uids') or info['uids'].real != 0:
                continue
            if ('sentinel.daemon.server' not in command and not any(
                    Path(arg).name == 'sentineld' for arg in command[:2])):
                continue
            try:
                stdin = os.readlink(f'/proc/{proc.pid}/fd/0')
            except OSError:
                stdin = ''
            found.append({'pid': proc.pid, 'known_service': managed.get(proc.pid),
                          'terminal_attached': stdin.startswith(('/dev/pts/', '/dev/tty'))})
        except (psutil.Error, OSError):
            continue
    return found


def daemon_lifecycle():
    # Filter on lifecycle messages before reading journal output. Do not collect
    # monitor window titles, inference results, browsing logs or PIN input.
    code, out, error = _command(['/usr/bin/journalctl', '-b', '--no-pager', '-o', 'json', '-n', '40',
        '-u', 'sentineld.service', '-u', 'sentineld-dev.service',
        r'--grep=sentineld stopped\.|sentineld listening on|Main process exited|Failed with result|Deactivated successfully|Stopping |Failed to start'])
    events = []
    for line in out.splitlines():
        try:
            row = json.loads(line)
            events.append({'unit': row.get('UNIT') or row.get('_SYSTEMD_UNIT'),
                           'timestamp_us': row.get('__REALTIME_TIMESTAMP'),
                           'message': str(row.get('MESSAGE', ''))[:600]})
        except (ValueError, AttributeError):
            continue
    return {'events': events, **({'probe_error': error} if code != 0 else {})}


def api_health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    result = {}
    for port in range(8000, 8004):
        try:
            with opener.open(f'http://127.0.0.1:{port}/health', timeout=1) as response:
                data = json.loads(response.read(4096))
                ready = (response.status == 200 and isinstance(data, dict)
                         and data.get('status') == 'ok' and data.get('model_loaded') is True)
                result[str(port)] = {'ready': ready}
        except (OSError, ValueError) as exc:
            result[str(port)] = {'ready': False, 'error': str(exc)[:300]}
    return result


def package_hashes(project):
    """Compare the actual imported protected runtime with the supplied checkout."""
    import importlib.util
    result = {}
    for name in MODULES:
        try:
            spec = importlib.util.find_spec(name)
            runtime = Path(spec.origin) if spec and spec.origin else None
            source = project / (name.replace('.', '/') + '.py')
            data = {'runtime_path': str(runtime) if runtime else None, 'source_path': str(source)}
            data['same_bytes'] = (hashlib.sha256(runtime.read_bytes()).digest()
                                  == hashlib.sha256(source.read_bytes()).digest()) if runtime and source.is_file() else None
            result[name] = data
        except (OSError, ImportError, ValueError) as exc:
            result[name] = {'error': str(exc)}
    return result


def findings(report):
    result = []
    def add(code, detail):
        result.append({'code': code, 'detail': detail})
    for field, code in (
            ('kde_action_restrictions_on_disk', 'KDE_BLOCKS_ALL_DESKTOP_LAUNCHES'),
            ('kde_action_restrictions_if_enabled', 'KDE_WOULD_BLOCK_ALL_DESKTOP_LAUNCHES')):
        if report.get(field, {}).get('run_desktop_files') in {'false', '0', 'no'}:
            add(code, 'run_desktop_files is disabled globally. KIO denies installed and recovery launchers before Sentinel can check approval.')
    gated = [row['desktop_id'] for row in report.get('launchers', []) if row.get('generated_gate')]
    if not report.get('rpc', {}).get('reachable') and gated:
        add('DAEMON_DOWN_WITH_GATED_LAUNCHERS',
            'Generated launchers still require RPC, so their saved approval cannot launch them while sentineld is unavailable.')
    recovery = next((r for r in report.get('launchers', [])
                     if r['desktop_id'] == 'sentinel-guardian-access.desktop'), None)
    if recovery and recovery.get('generated_gate'):
        add('RECOVERY_USES_SAME_RPC_GATE', 'The built-in recovery launcher depends on the same daemon connection as normal apps.')
    if recovery and not recovery.get('generated_gate') and recovery.get('gate_when_enabled'):
        add('RECOVERY_GATE_WHEN_ENABLED', 'The installed renderer would put recovery behind the RPC gate when this setting is enabled.')
    if recovery and recovery.get('manager_requires_rpc'):
        add('RECOVERY_MANAGER_REQUIRES_RPC', 'The manager also reads lock state and verifies the PIN through sentineld; it has no offline disable route.')
    if recovery and recovery.get('offline_recovery_socket_available') is False and not recovery.get('manager_requires_rpc'):
        add('OFFLINE_RECOVERY_SOCKET_MISSING', 'Install and enable the independent Guardian recovery socket before testing this mode.')
    for row in report.get('launchers', []):
        if row.get('approved') and row.get('denied_kde_actions'):
            add('APPROVAL_CONFLICTS_WITH_KDE_AUTHORIZATION',
                f"{row['desktop_id']} requires disabled KDE actions: {', '.join(row['denied_kde_actions'])}.")
        if row.get('approved') and not row.get('denied_kde_actions') and row.get('denied_kde_actions_if_enabled'):
            add('APPROVED_APP_WOULD_CONFLICT_WITH_KDE_AUTHORIZATION',
                f"{row['desktop_id']} would require KDE actions disabled by this mode: {', '.join(row['denied_kde_actions_if_enabled'])}.")
        if row.get('approved') and row.get('rpc_launch_check') not in {None, 'allowed_by_current_policy'}:
            add('CURRENT_RPC_DENIES_APPROVED_LAUNCH', f"{row['desktop_id']}: {row['rpc_launch_check']}")
        if row.get('approved') and row.get('override_hidden'):
            add('APPROVED_LAUNCHER_HIDDEN', f"{row['desktop_id']} has an override with Hidden=true.")
        if row.get('saved') and not row.get('approved'):
            add('SAVED_APPROVAL_NO_LONGER_MATCHES_LAUNCHER', f"No matching installed launcher identity for {row['desktop_id']}.")
        if row.get('generated_gate') and row.get('gate_runtime_available') is False:
            add('LAUNCH_GATE_RUNTIME_MISSING', f"The protected Python executable is missing for {row['desktop_id']}.")
    if report.get('persisted_policy', {}).get('enabled') is False:
        add('POLICY_CURRENTLY_DISABLED', 'This snapshot is after disabling; it does not reconstruct the policy or KDE caches at the time of the lockout.')
    services = report.get('services', {})
    service = services.get('sentineld.service', {})
    if service.get('UnitFileState') != 'enabled':
        add('DAEMON_NOT_ENABLED_AT_BOOT', 'The installed sentineld unit has no persistent enablement; a manual, runtime-enabled or transient instance is not proof of startup after reboot.')
    if services.get('sentineld-dev.service', {}).get('Transient') == 'yes':
        add('TRANSIENT_DEVELOPMENT_DAEMON', 'The development unit lasts for this boot only.')
    if len(report.get('daemon_processes', [])) > 1:
        add('MULTIPLE_DAEMON_PROCESSES', 'Multiple root daemon processes were detected; check which instance owns the socket.')
    if any(p.get('terminal_attached') and not p.get('known_service') for p in report.get('daemon_processes', [])):
        add('DAEMON_ATTACHED_TO_TERMINAL', 'A daemon has terminal stdin; logout/terminal closure can stop a foreground development process.')
    if any(v.get('same_bytes') is False for v in report.get('package_hashes', {}).values()):
        add('CHECKOUT_AND_RUNTIME_DIFFER', 'The checkout and imported runtime differ. Editing the checkout alone does not update protected launchers.')
    if any(not item['ready'] for item in report.get('api_health', {}).values()):
        add('MONITOR_WAITING_FOR_MODEL_APIS', 'At least one model API is unready. This delays monitor startup, independently of the app launch gate.')
    if report.get('rpc', {}).get('policy', {}).get('application', {}).get('session_restart_required'):
        add('SESSION_RESTART_FLAG_SET', 'The daemon reports a reload flag. It is sticky in this implementation, so it does not prove the current Plasma cache is stale.')
    return result


def collect_report(project):
    from sentinel.daemon import client
    from sentinel.daemon.operations import guardian_app_ops as ops
    from sentinel.guardian_apps import desktop
    from sentinel.settings.paths import GUARDIAN_APPS_FILE, GUARDIAN_DESKTOP_STATE_DIR
    report = {'schema_version': 1, 'read_only': True, 'rpc': {}, 'launchers': []}
    try:
        report['rpc'] = {'reachable': True, 'policy': client.get_guardian_app_policy()}
    except client.SentineldError as exc:
        report['rpc'] = {'reachable': False, 'error': str(exc)}
    try:
        # Validation/selection are pure in this separate process. Do not call
        # initialize/reconcile: those functions write launchers and Kiosk state.
        ops._config = ops._validate(json.loads(GUARDIAN_APPS_FILE.read_text()))
        config = ops._config
        report['persisted_policy'] = {'enabled': config['enabled'], 'apps': config['apps']}
        daemon_config = Path('/etc/sentinel/daemon.toml')
        uid = pwd.getpwnam(tomllib.loads(daemon_config.read_text())['protected_user']).pw_uid
        report['protected_uid'] = uid
        inventory = desktop.discover(include_settings_modules=True, uid=uid)
        actions = read_restrictions(ops.KDEGLOBALS.read_text() if ops.KDEGLOBALS.exists() else '')
        report['kde_action_restrictions_on_disk'] = actions
        managed = GUARDIAN_DESKTOP_STATE_DIR / 'launchers'
        report['tracked_overrides'] = sorted(p.name for p in managed.glob('*.desktop'))
        predicted_kiosk = ops.kiosk_config(inventory) if hasattr(ops, 'kiosk_config') else ops.KIOSK
        report['kde_action_restrictions_if_enabled'] = read_restrictions(predicted_kiosk)
        ids = set(config['apps']) | {'sentinel-guardian-access.desktop'}
        for key in sorted(ids):
            entry = inventory.get(key)
            override = desktop.OVERRIDE_DIR / key
            generated = False
            hidden = False
            if override.is_file():
                text = desktop.read_launcher(override)
                generated = text.startswith(desktop.MARKER)
                if generated:
                    parser = configparser.ConfigParser(interpolation=None, strict=False)
                    parser.read_string(text)
                    hidden = parser['Desktop Entry'].get('Hidden', '').lower() == 'true'
                    generated = 'sentinel.guardian_apps.launch' in parser['Desktop Entry'].get('Exec', '')
            row = {'desktop_id': key, 'installed': entry is not None, 'saved': key in config['apps'],
                   'approved': bool(entry and ops.selected(entry)),
                   'generated_gate': generated, 'override_hidden': hidden,
                   'gate_when_enabled': bool(entry and 'sentinel.guardian_apps.launch' in desktop.render_launcher(entry, True)),
                   'gate_runtime_available': Path('/usr/lib/sentinel/venv/bin/python').is_file(),
                   'manager_requires_rpc': key == 'sentinel-guardian-access.desktop' and not hasattr(desktop, 'RECOVERY_EXEC'),
                   'offline_recovery_socket_available': Path('/run/sentinel/guardian-recovery.sock').is_socket(),
                   'denied_kde_actions': launcher_authorizations(entry['source'], actions) if entry else [],
                   'denied_kde_actions_if_enabled': launcher_authorizations(
                       entry['source'], read_restrictions(predicted_kiosk)) if entry else []}
            if entry:
                field = {'native': 'executable', 'flatpak': 'flatpak_id', 'snap': 'snap_instance'}[entry['type']]
                row.update(type=entry['type'], identity=entry[field], supported=entry['supported'])
            if report['rpc']['reachable']:
                try:
                    client.request('guardian.apps.launch', {'desktop_id': key}, timeout=3)
                    row['rpc_launch_check'] = 'allowed_by_current_policy'
                except client.SentineldError as exc:
                    row['rpc_launch_check'] = str(exc)
            report['launchers'].append(row)
    except (OSError, ValueError, KeyError, configparser.Error) as exc:
        report['offline_probe_error'] = str(exc)
    report['services'] = {unit: service_status(unit) for unit in SERVICES}
    report['daemon_lifecycle'] = daemon_lifecycle()
    report['daemon_processes'] = daemon_processes(report['services'])
    report['api_health'] = api_health()
    report['package_hashes'] = package_hashes(project)
    report['findings'] = findings(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo and the protected Python to inspect root-owned state; this command does not modify it.')
    print(json.dumps(collect_report(args.project.resolve()), indent=2))


if __name__ == '__main__':
    main()
