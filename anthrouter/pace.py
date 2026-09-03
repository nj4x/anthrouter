"""Workday-aware pace baseline calculations for OAuth usage meter."""

from __future__ import annotations

import calendar
import datetime as dt
import os
import time
from zoneinfo import ZoneInfo


def period_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Return the UTC calendar month boundaries containing ``now``.

    Returns ``(first_day_of_month, first_day_of_next_month)`` at 00:00:00+00:00.
    ``now`` must be tz-aware UTC.
    """
    year = now.year
    month = now.month
    start = dt.datetime(year, month, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    if month == 12:
        end = dt.datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    else:
        end = dt.datetime(year, month + 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    return start, end


def calendar_elapsed_pct(now: dt.datetime) -> float:
    """Fraction of the current UTC calendar month elapsed, 0-100."""
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    seconds_into_day = now.hour * 3600 + now.minute * 60 + now.second
    return (now.day - 1 + seconds_into_day / 86400.0) / days_in_month * 100.0


def period_workday_dates(
    period_start: dt.datetime, period_end: dt.datetime, tz: ZoneInfo
) -> list[dt.date]:
    """Return all Mon-Fri local dates overlapping the UTC period.

    The range runs from ``period_start.astimezone(tz).date()`` through
    ``(period_end - 1 microsecond).astimezone(tz).date()``, inclusive.
    No holiday exclusions.
    """
    start_local = period_start.astimezone(tz).date()
    end_local = (period_end - dt.timedelta(microseconds=1)).astimezone(tz).date()
    workdays: list[dt.date] = []
    current = start_local
    while current <= end_local:
        if current.weekday() < 5:
            workdays.append(current)
        current += dt.timedelta(days=1)
    return workdays


def workday_elapsed_pct(
    now: dt.datetime, tz: ZoneInfo
) -> tuple[float, int]:
    """Return ``(pct, workday_count)`` for the workday-aware pace baseline.

    ``workday_count`` is the denominator: total Mon-Fri dates in the period.
    ``pct`` is the fractional elapsed percentage (0-100).

    The elapsed count is fractional:
    - Full weekdays strictly before ``now``'s local date
    - Plus ``fraction_of_current_local_day`` if today is Mon-Fri

    Weekend days contribute 0 to the elapsed count (baseline freezes).
    """
    period_start, period_end = period_bounds(now)
    workdays = period_workday_dates(period_start, period_end, tz)
    workday_count = len(workdays)

    if workday_count == 0:
        return (0.0, 0)

    now_local = now.astimezone(tz)
    now_date = now_local.date()
    seconds_into_day = now_local.hour * 3600 + now_local.minute * 60 + now_local.second
    fraction_of_day = seconds_into_day / 86400.0

    elapsed = 0.0
    for wd in workdays:
        if wd < now_date:
            elapsed += 1.0
        elif wd == now_date and now_local.weekday() < 5:
            elapsed += fraction_of_day

    pct = elapsed / workday_count * 100.0
    return (pct, workday_count)


def resolve_timezone(configured: str | None) -> ZoneInfo:
    """Resolve a timezone name to a ``ZoneInfo`` instance.

    - If ``configured`` is a non-empty string, return ``ZoneInfo(configured)``.
      Let ``ZoneInfoNotFoundError`` / ``ValueError`` propagate.
    - If ``configured`` is None/empty, auto-detect. If detection yields nothing,
      or yields a UTC-equivalent name, return ``ZoneInfo('America/Los_Angeles')``.
    """
    if configured and configured.strip():
        return ZoneInfo(configured.strip())

    detected = detect_local_timezone_name()
    if detected is None:
        return ZoneInfo('America/Los_Angeles')

    utc_equivalents = {'UTC', 'Etc/UTC', 'Universal', 'Zulu'}
    if detected in utc_equivalents:
        return ZoneInfo('America/Los_Angeles')

    return ZoneInfo(detected)


def detect_local_timezone_name() -> str | None:
    """Best-effort IANA timezone name detection.

    Returns the first successful match:
    1. ``datetime.now().astimezone().tzinfo.key`` (from TZ env var)
    2. Symlink target of ``/etc/localtime`` if it contains 'zoneinfo'
    3. Each entry of ``time.tzname`` that constructs a valid ``ZoneInfo``

    Returns ``None`` if all attempts fail. Never raises.
    """
    try:
        key = getattr(dt.datetime.now().astimezone().tzinfo, 'key', None)
        if key:
            return key
    except Exception:  # noqa: BLE001, S110 - detection is best-effort; caller falls back
        pass

    try:
        localtime_path = os.path.join(os.sep, 'etc', 'localtime')
        if os.path.islink(localtime_path):
            resolved = os.readlink(localtime_path)
            parts = resolved.split(os.sep)
            if 'zoneinfo' in parts:
                idx = parts.index('zoneinfo')
                zone_name = os.sep.join(parts[idx + 1:])
                if zone_name:
                    return zone_name
    except Exception:  # noqa: BLE001, S110 - detection is best-effort; caller falls back
        pass

    for name in time.tzname:
        if name:
            try:
                ZoneInfo(name)
                return name
            except Exception:  # noqa: BLE001, S110 - probing candidate names
                pass

    return None
