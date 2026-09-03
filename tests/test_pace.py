"""Tests for the workday-aware pace baseline module (ADR-0008)."""

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from anthrouter import pace
from anthrouter.pace import (
    calendar_elapsed_pct,
    detect_local_timezone_name,
    period_bounds,
    period_workday_dates,
    resolve_timezone,
    workday_elapsed_pct,
)


class TestPeriodBounds:
    """Tests for period_bounds function."""

    def test_september_2026(self):
        """Period bounds for September 2026."""
        now = dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        start, end = period_bounds(now)
        assert start == dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        assert end == dt.datetime(2026, 10, 1, 0, 0, 0, tzinfo=dt.timezone.utc)

    def test_december_to_january(self):
        """Period bounds across year boundary (December -> January)."""
        now = dt.datetime(2026, 12, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        start, end = period_bounds(now)
        assert start == dt.datetime(2026, 12, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        assert end == dt.datetime(2027, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)


class TestCalendarElapsedPct:
    """Tests for calendar_elapsed_pct function."""

    def test_start_of_month(self):
        """At start of month, elapsed should be ~0."""
        now = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        pct = calendar_elapsed_pct(now)
        assert pct == 0.0

    def test_mid_month(self):
        """Mid-month check."""
        now = dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        pct = calendar_elapsed_pct(now)
        # September has 30 days. Day 14 + 12/24 = 14.5 days elapsed.
        assert pct == pytest.approx(14.5 / 30 * 100, rel=1e-6)
        assert pct == pytest.approx(48.3333333333, rel=1e-6)

    def test_end_of_month(self):
        """Near end of month."""
        now = dt.datetime(2026, 9, 30, 23, 59, 0, tzinfo=dt.timezone.utc)
        pct = calendar_elapsed_pct(now)
        # September has 30 days. Day 29 + (23*3600 + 59*60)/86400
        seconds_into_day = 23 * 3600 + 59 * 60
        fraction = 29 + seconds_into_day / 86400.0
        assert pct == pytest.approx(fraction / 30 * 100, rel=1e-6)
        assert pct == pytest.approx(99.9976851852, rel=1e-6)


class TestWorkdayElapsedPct:
    """Tests for workday_elapsed_pct function.

    Anchor case: September 2026 in America/Los_Angeles (PDT, UTC-7).
    Denominator is exactly 23: Aug 31 (Mon, 1) + Sep 1-4 (4) + Sep 7-11, 14-18, 21-25 (15) + Sep 28-30 (3).
    Aug 31 counts because 2026-09-01 00:00 UTC is 2026-08-31 17:00 PDT.
    """

    TZ_LA = ZoneInfo('America/Los_Angeles')

    def test_period_open_partial_first_day(self):
        """2026-09-01 00:00 UTC -> local Aug 31 17:00 PDT.

        17/24 = 0.708333 of a weekday elapsed, 0 full days before.
        pct = 0.7083333/23*100 ≈ 3.0797%
        """
        now = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        pct, count = workday_elapsed_pct(now, self.TZ_LA)
        assert count == 23
        fraction = 17 / 24.0
        expected_pct = fraction / 23 * 100
        assert pct == pytest.approx(expected_pct, rel=1e-6)
        assert pct == pytest.approx(3.079710144927536, rel=1e-6)

    def test_mid_period_workday(self):
        """2026-09-15 12:00 UTC -> local Sep 15 05:00 PDT (Tue).

        Full weekdays strictly before Sep 15 in range = 11:
          Aug 31 (1) + Sep 1-4 (4) + Sep 7-11 (5) + Sep 14 (1) = 11
        Fraction = 5/24 = 0.208333
        pct = 11.2083333/23*100 ≈ 48.7319%
        """
        now = dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        pct, count = workday_elapsed_pct(now, self.TZ_LA)
        assert count == 23
        full_days_before = 11
        fraction = 5 / 24.0
        elapsed = full_days_before + fraction
        expected_pct = elapsed / 23 * 100
        assert pct == pytest.approx(expected_pct, rel=1e-6)
        assert pct == pytest.approx(48.73188405797101, rel=1e-6)

    def test_weekend_saturday(self):
        """2026-09-05 12:00 UTC -> local Sat Sep 5 05:00 PDT.

        Fraction must be 0 (weekend).
        Full weekdays before = 5 (Aug 31; Sep 1-4).
        pct = 5/23*100 ≈ 21.7391%
        """
        now = dt.datetime(2026, 9, 5, 12, 0, 0, tzinfo=dt.timezone.utc)
        pct, count = workday_elapsed_pct(now, self.TZ_LA)
        assert count == 23
        expected_pct = 5 / 23 * 100
        assert pct == pytest.approx(expected_pct, rel=1e-6)
        assert pct == pytest.approx(21.739130434782606, rel=1e-6)

    def test_weekend_sunday_same_as_saturday(self):
        """2026-09-06 12:00 UTC -> local Sun Sep 6 05:00 PDT.

        Value must be IDENTICAL to Saturday - baseline freezes across weekend.
        """
        now_sat = dt.datetime(2026, 9, 5, 12, 0, 0, tzinfo=dt.timezone.utc)
        now_sun = dt.datetime(2026, 9, 6, 12, 0, 0, tzinfo=dt.timezone.utc)
        pct_sat, _ = workday_elapsed_pct(now_sat, self.TZ_LA)
        pct_sun, _ = workday_elapsed_pct(now_sun, self.TZ_LA)
        assert pct_sat == pct_sun

    def test_weekend_to_monday_midnight_resumes(self):
        """2026-09-07 07:00 UTC = local Mon Sep 7 00:00 PDT.

        Baseline resumes at Monday midnight. Fraction should be 0.0 at start of day.
        Full weekdays before = 5 (Aug 31; Sep 1-4).
        """
        now = dt.datetime(2026, 9, 7, 7, 0, 0, tzinfo=dt.timezone.utc)
        pct, count = workday_elapsed_pct(now, self.TZ_LA)
        assert count == 23
        expected_pct = 5 / 23 * 100
        assert pct == pytest.approx(expected_pct, rel=1e-6)

    def test_last_period_day_partial(self):
        """2026-09-30 23:59 UTC -> local Sep 30 16:59 PDT (Wed).

        Full weekdays before = 22
        fraction = (16*3600 + 59*60)/86400 = 0.7076389
        pct = 22.7076389/23*100 ≈ 98.7288%
        """
        now = dt.datetime(2026, 9, 30, 23, 59, 0, tzinfo=dt.timezone.utc)
        pct, count = workday_elapsed_pct(now, self.TZ_LA)
        assert count == 23
        full_days_before = 22
        fraction = (16 * 3600 + 59 * 60) / 86400.0
        elapsed = full_days_before + fraction
        expected_pct = elapsed / 23 * 100
        assert pct == pytest.approx(expected_pct, rel=1e-6)
        assert pct == pytest.approx(98.72886473429951, rel=1e-6)

    def test_calendar_baseline_lags_workday_at_period_open(self):
        """At 2026-09-01 00:00 UTC the calendar baseline reads 0 but 70.8% of a
        workday (Aug 31 in PDT) has already been spent."""
        now = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        workday_pct, _ = workday_elapsed_pct(now, self.TZ_LA)
        assert calendar_elapsed_pct(now) == 0.0
        assert workday_pct == pytest.approx(3.079710144927536, rel=1e-6)

    def test_calendar_baseline_understates_early_period(self):
        """A calendar baseline this far below the workday baseline is what makes
        early-month spend read as overuse — the distortion ADR-0008 fixes."""
        now = dt.datetime(2026, 9, 2, 0, 0, 0, tzinfo=dt.timezone.utc)
        calendar_pct = calendar_elapsed_pct(now)
        workday_pct, _ = workday_elapsed_pct(now, self.TZ_LA)
        assert calendar_pct == pytest.approx(3.3333333333, rel=1e-6)
        assert workday_pct == pytest.approx(7.4275362318, rel=1e-6)
        assert workday_pct / calendar_pct == pytest.approx(2.2282608695, rel=1e-6)


class TestPeriodWorkdayDates:
    """Tests for period_workday_dates function."""

    TZ_LA = ZoneInfo('America/Los_Angeles')

    def test_september_2026_count(self):
        """September 2026 should have exactly 23 workdays."""
        start = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 10, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        workdays = period_workday_dates(start, end, self.TZ_LA)
        assert len(workdays) == 23

    def test_september_2026_includes_aug_31(self):
        """Aug 31 should be included because Sep 1 00:00 UTC = Aug 31 17:00 PDT."""
        start = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 10, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        workdays = period_workday_dates(start, end, self.TZ_LA)
        assert dt.date(2026, 8, 31) in workdays


class TestNonPacificTimezones:
    """The same UTC period yields a different workday set per zone, because the
    overlapping local dates differ at both edges."""

    SEP_START = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    SEP_END = dt.datetime(2026, 10, 1, 0, 0, 0, tzinfo=dt.timezone.utc)

    def test_utc_excludes_both_pacific_edge_days(self):
        """Under UTC the period starts on Sep 1 and ends on Sep 30, so neither
        Aug 31 nor Oct 1 overlaps: 22 workdays, one fewer than Pacific."""
        tz = ZoneInfo('UTC')
        workdays = period_workday_dates(self.SEP_START, self.SEP_END, tz)
        assert len(workdays) == 22
        assert dt.date(2026, 8, 31) not in workdays
        assert dt.date(2026, 10, 1) not in workdays

        now = dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        pct, count = workday_elapsed_pct(now, tz)
        assert count == 22
        assert pct == pytest.approx(10.5 / 22 * 100, rel=1e-6)
        assert pct == pytest.approx(47.7272727273, rel=1e-6)

    def test_berlin_shifts_the_window_forward(self):
        """Berlin is UTC+2 in September, so the period opens on Sep 1 local and
        runs into Oct 1 — the mirror image of Pacific's Aug 31 overlap."""
        tz = ZoneInfo('Europe/Berlin')
        workdays = period_workday_dates(self.SEP_START, self.SEP_END, tz)
        assert len(workdays) == 23
        assert dt.date(2026, 8, 31) not in workdays
        assert dt.date(2026, 10, 1) in workdays

        now = dt.datetime(2026, 9, 15, 10, 0, 0, tzinfo=dt.timezone.utc)
        pct, count = workday_elapsed_pct(now, tz)
        assert count == 23
        assert pct == pytest.approx(10.5 / 23 * 100, rel=1e-6)
        assert pct == pytest.approx(45.6521739130, rel=1e-6)


class TestDaylightSavingTransition:
    """November 2026 spans the PDT->PST change (2026-11-01), so the UTC offset
    differs between the start and end of a single period."""

    TZ_LA = ZoneInfo('America/Los_Angeles')

    def test_denominator_spans_the_transition(self):
        start = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 12, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        workdays = period_workday_dates(start, end, self.TZ_LA)
        assert len(workdays) == 21
        # Period opens at Oct 31 17:00 PDT (a Saturday), so no October weekday overlaps.
        assert all(d.month == 11 for d in workdays)

    def test_before_transition_while_still_pdt(self):
        """2026-11-01 00:00 UTC is Oct 31 17:00 PDT (UTC-7), a Saturday."""
        now = dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
        pct, count = workday_elapsed_pct(now, self.TZ_LA)
        assert count == 21
        assert pct == 0.0

    def test_after_transition_under_pst(self):
        """2026-11-16 20:00 UTC is Nov 16 12:00 PST (UTC-8), a Monday, with ten
        full weekdays behind it."""
        now = dt.datetime(2026, 11, 16, 20, 0, 0, tzinfo=dt.timezone.utc)
        pct, count = workday_elapsed_pct(now, self.TZ_LA)
        assert count == 21
        assert pct == pytest.approx(10.5 / 21 * 100, rel=1e-6)
        assert pct == pytest.approx(50.0, rel=1e-6)

    def test_fraction_never_exceeds_a_whole_day(self):
        """A 25-hour local day must not push the daily fraction past 1.0. US
        transitions land on a Sunday, so sample every hour of that local day."""
        for hour in range(24):
            now = dt.datetime(2026, 11, 1, 8, 0, 0, tzinfo=dt.timezone.utc) + dt.timedelta(hours=hour)
            pct, count = workday_elapsed_pct(now, self.TZ_LA)
            assert 0.0 <= pct <= 100.0
            assert count == 21


class TestEmptyWorkdayPeriod:
    def test_zero_workdays_does_not_divide_by_zero(self, monkeypatch):
        monkeypatch.setattr(pace, 'period_workday_dates', lambda *_: [])
        now = dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        assert pace.workday_elapsed_pct(now, ZoneInfo('America/Los_Angeles')) == (0.0, 0)


class TestResolveTimezone:
    """Tests for resolve_timezone function."""

    def test_explicit_utc_is_honoured(self):
        """Explicit 'UTC' should return UTC."""
        tz = resolve_timezone('UTC')
        assert tz.key == 'UTC'

    def test_explicit_bad_name_raises(self):
        """An unrecognized timezone name should raise ZoneInfoNotFoundError or ValueError."""
        with pytest.raises((ZoneInfoNotFoundError, ValueError, KeyError)):
            resolve_timezone('Not/AZone')

    def test_none_with_utc_detection_falls_back(self, monkeypatch):
        """Auto-detected UTC falls back to America/Los_Angeles."""
        for detected in ('UTC', 'Etc/UTC', 'Universal', 'Zulu'):
            monkeypatch.setattr(pace, 'detect_local_timezone_name', lambda d=detected: d)
            assert resolve_timezone(None).key == 'America/Los_Angeles'

    def test_none_with_undetectable_timezone_falls_back(self, monkeypatch):
        """Detection returning None falls back to America/Los_Angeles."""
        monkeypatch.setattr(pace, 'detect_local_timezone_name', lambda: None)
        assert resolve_timezone(None).key == 'America/Los_Angeles'

    def test_none_with_detected_timezone_is_used(self, monkeypatch):
        """A non-UTC detected zone is used as-is."""
        monkeypatch.setattr(pace, 'detect_local_timezone_name', lambda: 'Europe/Berlin')
        assert resolve_timezone(None).key == 'Europe/Berlin'

    def test_explicit_utc_is_not_overridden_by_fallback(self, monkeypatch):
        """Explicit 'UTC' is honoured even though auto-detected UTC would not be."""
        monkeypatch.setattr(pace, 'detect_local_timezone_name', lambda: 'Europe/Berlin')
        assert resolve_timezone('UTC').key == 'UTC'

    def test_explicit_timezone_returns_correct(self):
        """Explicit timezone like 'Europe/Berlin' should return that zone."""
        tz = resolve_timezone('Europe/Berlin')
        assert tz.key == 'Europe/Berlin'


class TestDetectLocalTimezoneName:
    """Tests for detect_local_timezone_name function."""

    def test_returns_string_or_none(self):
        """Function should return a string or None, never raise."""
        result = detect_local_timezone_name()
        assert result is None or isinstance(result, str)
