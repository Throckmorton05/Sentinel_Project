"""Bounded calendar recurrence in the schedule's IANA timezone.

Repeat local clock times, retain the original day for monthly schedules, and
move missing DST clock times forward by the DST gap. Half-open occurrences
never include their end. There is no materialized, ever-growing schedule list.
"""
from calendar import monthrange
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def validate(window):
    repeat = window.get('recurrence', 'none')
    if repeat not in ('none', 'daily', 'weekly', 'monthly'):
        raise ValueError('recurrence must be none, daily, weekly, or monthly.')
    if repeat == 'none':
        return
    zone = ZoneInfo(window['timezone'])
    start = datetime.fromtimestamp(window['start_at'], zone).replace(tzinfo=None)
    end = datetime.fromtimestamp(window['end_at'], zone).replace(tzinfo=None)
    maximum = {'daily': 1, 'weekly': 7, 'monthly': 28}[repeat]
    if not timedelta(0) < end-start <= timedelta(days=maximum):
        raise ValueError(f'A {repeat} window must last at most {maximum} local day(s).')


def _index(window, instant):
    zone = ZoneInfo(window['timezone'])
    anchor = datetime.fromtimestamp(window['start_at'], zone)
    current = datetime.fromtimestamp(instant, zone)
    repeat = window.get('recurrence', 'none')
    if repeat == 'monthly':
        return max(0, (current.year-anchor.year)*12 + current.month-anchor.month)
    days = (current.date()-anchor.date()).days
    return max(0, days // (7 if repeat == 'weekly' else 1))


def occurrence(window, index):
    repeat = window.get('recurrence', 'none')
    if index == 0 or repeat == 'none':
        return dict(window)
    zone = ZoneInfo(window['timezone'])
    first = datetime.fromtimestamp(window['start_at'], zone)
    last = datetime.fromtimestamp(window['end_at'], zone)
    start = first.replace(tzinfo=None)
    duration = last.replace(tzinfo=None)-start
    if repeat == 'monthly':
        month_number = first.year*12 + first.month-1 + index
        year, month = divmod(month_number, 12)
        month += 1
        start = start.replace(year=year, month=month,
                              day=min(first.day, monthrange(year, month)[1]))
    else:
        start += timedelta(days=index*(7 if repeat == 'weekly' else 1))
    def instant(value, fold):
        timestamp = value.replace(tzinfo=zone, fold=fold).timestamp()
        if datetime.fromtimestamp(timestamp, zone).replace(tzinfo=None) != value:
            timestamp = value.replace(tzinfo=zone, fold=0).timestamp()
        return timestamp
    begins = instant(start, first.fold)
    ends = instant(start+duration, last.fold)
    # A DST jump can consume a short window entirely; skip that occurrence.
    return {**window, 'start_at': begins, 'end_at': max(begins, ends)}


def current_or_next(window, now):
    if window.get('recurrence', 'none') == 'none':
        return dict(window)
    index = _index(window, now)
    # The previous occurrence may cross midnight/the start of a month.
    for i in range(max(0, index-1), index+3):
        item = occurrence(window, i)
        if item['end_at'] > now and item['end_at'] > item['start_at']:
            return item
    raise ValueError('Unable to resolve recurring block window.')


def in_range(window, lower, upper):
    if window.get('recurrence', 'none') == 'none':
        candidates = [dict(window)]
    else:
        if upper-lower > 366*86400:
            raise ValueError('Recurring calendar ranges may span at most 366 days.')
        candidates = (occurrence(window, i) for i in range(
            max(0, _index(window, lower)-1), _index(window, upper)+2))
    return [item for item in candidates if item['start_at'] < upper
            and item['end_at'] > lower and item['end_at'] > item['start_at']]
