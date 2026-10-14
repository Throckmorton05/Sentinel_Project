from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sentinel.daemon.operations import recurrence


def schedule(start, end, repeat, zone='America/Chicago'):
    tz = ZoneInfo(zone)
    return dict(start_at=datetime.fromisoformat(start).replace(tzinfo=tz).timestamp(),
                end_at=datetime.fromisoformat(end).replace(tzinfo=tz).timestamp(),
                recurrence=repeat, timezone=zone)


def local(item, key='start_at'):
    return datetime.fromtimestamp(item[key], ZoneInfo(item['timezone']))


def test_monthly_clamp_uses_original_day_after_february_and_across_year():
    item = schedule('2028-01-31T22:00', '2028-02-01T01:00', 'monthly')
    february = recurrence.occurrence(item, 1)
    assert local(february).isoformat() == '2028-02-29T22:00:00-06:00'
    assert local(february, 'end_at').isoformat() == '2028-03-01T01:00:00-06:00'
    assert local(recurrence.occurrence(item, 2)).day == 31
    assert local(recurrence.occurrence(item, 12)).isoformat() == '2029-01-31T22:00:00-06:00'


@pytest.mark.parametrize('repeat,index', [('daily', 3), ('weekly', 1)])
def test_repeat_preserves_local_clock_across_dst(repeat, index):
    item = schedule('2026-03-06T09:00', '2026-03-06T10:00', repeat)
    next_item = recurrence.occurrence(item, index)
    assert local(next_item).hour == 9
    assert local(next_item).utcoffset().total_seconds() == -5*3600


def test_missing_spring_clock_time_moves_forward_and_skips_empty_occurrence():
    item = schedule('2026-03-07T02:15', '2026-03-07T02:45', 'daily')
    moved = recurrence.occurrence(item, 1)
    assert local(moved).hour == 3
    assert local(moved).minute == 15
    item = schedule('2026-03-07T02:30', '2026-03-07T03:30', 'daily')
    assert recurrence.occurrence(item, 1)['start_at'] == recurrence.occurrence(item, 1)['end_at']
    now = schedule('2026-03-08T00:00', '2026-03-08T01:00', 'none')['start_at']
    assert local(recurrence.current_or_next(item, now)).day == 9


def test_overnight_occurrence_and_end_boundary():
    item = schedule('2026-10-05T23:00', '2026-10-06T02:00', 'weekly')
    second = recurrence.occurrence(item, 1)
    assert recurrence.current_or_next(item, second['start_at']+7200) == second
    assert local(recurrence.current_or_next(item, second['end_at'])).day == 19
    assert recurrence.in_range(item, second['end_at'], second['end_at']+3600) == []


def test_full_local_day_across_fall_dst_is_25_hours():
    item = schedule('2026-10-31T00:00', '2026-11-01T00:00', 'daily')
    recurrence.validate(item)
    fall = recurrence.occurrence(item, 1)
    assert fall['end_at']-fall['start_at'] == 25*3600


@pytest.mark.parametrize('repeat,days', [('daily', 2), ('weekly', 8), ('monthly', 29)])
def test_rejects_overlapping_recurrence_durations(repeat, days):
    item = schedule('2026-01-01T00:00', f'2026-01-{days+1:02}T00:00', repeat)
    with pytest.raises(ValueError):
        recurrence.validate(item)


def test_distant_recurrence_lookup_does_not_enumerate_past_instances():
    item = schedule('2026-01-31T12:00', '2026-01-31T13:00', 'monthly')
    now = schedule('2099-02-28T12:30', '2099-02-28T13:30', 'none')['start_at']
    assert local(recurrence.current_or_next(item, now)).isoformat() == '2099-02-28T12:00:00-06:00'
