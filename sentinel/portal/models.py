"""Pure presentation helpers; protection decisions remain in sentineld."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from sentinel.daemon.client import user_message

MODE_LABELS = {'unlocked': 'Unlocked', 'self_lock': 'Self-Lock', 'guardian_lock': 'Guardian Lock'}
SETTING_LABELS = {
    'image_filtering': 'Image filtering',
    'safe_installs': 'Approved software installs', 'safe_browsing': 'Safe browsing',
    'safe_steam': 'Safe Steam', 'safe_crunchyroll': 'Safe Crunchyroll',
    'boot_media_protection': 'Boot media protection', 'dns.enabled': 'DNS filtering',
    'vm_protection': 'Block virtual machines',
    'dns.adult': 'Adult content', 'dns.adult_heavy_sites': 'Adult-heavy websites',
    'dns.gambling': 'Gambling', 'dns.social_media': 'Social media',
    'dns.unsafe_search_engines': 'Unsafe search engines',
    'dns.browsers_sans_safe_search': 'Browsers without SafeSearch',
}


def setting_label(name):
    return SETTING_LABELS.get(name, name.replace('dns.', '').replace('_', ' ').capitalize())


def setting_editable(definition, value, mode):
    if mode != 'self_lock':
        return True
    if definition.get('type') != 'boolean':
        return False
    direction = 'disable' if value else 'enable'
    return definition.get('changes', {}).get(direction) in {'maintain', 'strengthen'}


def month_range(year, month, timezone):
    zone = ZoneInfo(timezone)
    start = datetime(year, month, 1, tzinfo=zone)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone)
    return start.timestamp(), end.timestamp()


def clock_time(value):
    return value.strftime('%I:%M %p').lstrip('0')


def overlaps_day(window, day, timezone):
    start = datetime.combine(day, time.min, ZoneInfo(timezone))
    end = datetime.combine(day + timedelta(days=1), time.min, ZoneInfo(timezone))
    return window['start_at'] < end.timestamp() and window['end_at'] > start.timestamp()


def wall_time(value, timezone, fold=0):
    """Reject nonexistent DST times and make repeated-hour choice explicit."""
    zone = ZoneInfo(timezone)
    instant = value.replace(tzinfo=zone, fold=fold)
    if datetime.fromtimestamp(instant.timestamp(), zone).replace(tzinfo=None) != value:
        raise ValueError('This time does not exist because the clocks move forward. Choose another time.')
    return instant.timestamp()


def read_pages(fetch, key):
    offset = 0
    seen = set()
    result = []
    while True:
        if offset in seen:
            raise ValueError('Sentinel backend returned a repeated page. Try again.')
        seen.add(offset)
        page = fetch(offset)
        result.extend(page[key])
        offset = page.get('next_offset')
        if offset is None:
            return result


def enforcement_message(result):
    """Separate a saved policy from pending enforcement without dropping errors."""
    messages = []
    def walk(value, path=''):
        if not isinstance(value, dict):
            return
        if value.get('status') in {'pending', 'failed', 'error'}:
            messages.append((path.replace('_', ' ') or 'Enforcement') + ' is ' + value['status'])
        for key in ('error', 'errors', 'unresolved'):
            issue = value.get(key)
            if issue:
                if isinstance(issue, dict):
                    messages.extend(str(item) for item in issue.values())
                elif isinstance(issue, list):
                    messages.extend(map(str, issue))
                else:
                    messages.append(str(issue))
        for key, child in value.items():
            if isinstance(child, dict) and key not in {'unresolved'}:
                walk(child, key)
    walk(result)
    return user_message('Saved. Enforcement needs attention: ' + '; '.join(dict.fromkeys(messages))) if messages else 'Saved.'
