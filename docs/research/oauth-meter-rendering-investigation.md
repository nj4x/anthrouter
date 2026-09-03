# OAuth usage meter: is the red overuse segment correct?

**Date**: 2026-09-03
**Scope**: the red "overuse" segment of the OAuth usage meter in the admin UI
**Status**: complete. The rendering is correct; the pace baseline it is measured against is questionable.

---

## Question

At $85–86 of a $466 monthly quota (18% used), roughly three days into the usage period, the red segment of the meter occupies about half of the filled portion of the bar. The expectation was a much smaller red tail:

- Usage period: 2026-08-31 17:00 to 2026-09-30 17:00.
- 23 workdays in that period (confirmed: Aug 31 2026 is a Monday; Aug 31 through Sep 30 contains 23 weekdays).
- Daily allowance on a workday basis: $466 / 23 = $20.26.
- About 3.5 workdays elapsed, so roughly $71 of on-pace allowance.
- Actual spend $85, so overuse should be roughly $14 — a red tail of about 17% of the filled portion, or 3% of the whole bar.

Two candidate faults were considered: wrong pace arithmetic, or wrong bar rendering.

---

## The rendering is correct

The meter is drawn in `anthrouter/ui/src/components/OAuthCard.tsx:73-104`. Three absolutely positioned children sit inside a `relative` track:

- Blue (or amber/red above 80%/100%): `width: ${pct}%` where `pct = Math.min(token.burn_pct ?? 0, 100)` (`OAuthCard.tsx:28`, `:83`).
- Red overuse: `left: ${month_elapsed_pct}%`, `width: ${burn_pct - month_elapsed_pct}%`, rendered only when `burn_pct > month_elapsed_pct` (`OAuthCard.tsx:85-93`).
- Green underuse: `left: ${burn_pct}%`, `width: ${month_elapsed_pct - burn_pct}%`, rendered only when the gap exceeds 0.5 (`OAuthCard.tsx:94-103`).

The red block overlays the right-hand part of the blue block, so the visible blue width equals `month_elapsed_pct` and the visible red width equals `burn_pct - month_elapsed_pct`. Total filled width equals `burn_pct`. That is the intended semantics and the arithmetic is sound provided both inputs are on a 0–100 scale.

Both inputs are on a 0–100 scale. A live query of the running proxy returned:

```
$ curl -s http://127.0.0.1:8083/admin/oauth-usage
{"oauth_token": {"burn_pct": 18.56008583690987, "used_usd": 86.49, "total_usd": 466.0,
 "month_elapsed_pct": 9.672415123456789, ...}}
```

Those values give a filled width of 18.56% of the track, split as 9.67% blue and 8.89% red — red is 47.9% of the filled portion. The screenshot of the anthrouter meter measures 247px blue and 221px red, i.e. red is 47.2% of the filled portion. The rendering therefore reproduces the served values precisely.

An earlier draft of this note claimed the bar was rendered 2.64× too wide, based on the filled portion measuring 48% of the *visible* track. That conclusion was wrong: the anthrouter screenshot is cropped at the right edge (the caption text below the card is cut mid-word), so the visible track is not the whole track. A filled portion of 468 device pixels at 18.26% implies a full track of about 2563 device pixels, which at the 2× device pixel ratio evident in both screenshots corresponds to a card roughly 1310 CSS pixels wide — consistent with a wide browser window. The reference (anthproxy) screenshot shows a much narrower card, which is why its filled portion looks small in comparison. The *ratio* of red to blue is essentially identical in both screenshots (52.5% and 47.2% of the filled portion), so anthrouter is not a regression against anthproxy — both use the same baseline.

## The pace baseline is the real issue

The red segment's left edge is `month_elapsed_pct`, produced by `anthrouter/oauth_usage.py:23-27`:

```python
def _month_elapsed_pct(now: dt.datetime) -> float:
    """Fraction of the current UTC month already elapsed, 0-100."""
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    seconds_into_day = now.hour * 3600 + now.minute * 60 + now.second
    return (now.day - 1 + seconds_into_day / 86400.0) / days_in_month * 100.0
```

It is called with the current UTC time at `oauth_usage.py:194` and passed through unmodified by the admin endpoint at `anthrouter/admin.py:437-449`. So the baseline is: fraction of the current **calendar UTC month** elapsed, measured in **calendar days**, from the 1st of the month at 00:00 UTC.

Two consequences follow.

**1. Calendar days, not workdays.** Spreading $466 over 30 calendar days gives $15.53/day rather than $20.26/workday. Early in a month the shortfall compounds because weekend days consume allowance that is never spent. At the observed data point the calendar baseline allows $45.07 (9.67% of $466) against a spend of $86.49, producing $41.42 of red. A workday-prorated baseline at 3.5 elapsed workdays allows $70.91 and produces $15.58 of red. The red segment is therefore about 2.7× larger than a workday model would draw it. This is the dominant source of the discrepancy and it fully explains the near-50/50 split.

**2. The calendar month is not the usage period.** The quota resets at 17:00, not at 00:00 UTC on the 1st (period 2026-08-31 17:00 to 2026-09-30 17:00). Nothing in `oauth_usage.py:151-200` reads a reset or period-boundary field from the upstream response — only `monthly_limit`, `used_credits`, `utilization`, `is_enabled`, `spend_limit_reached` and `decimal_places` are parsed (`oauth_usage.py:156-188`), and the period is inferred locally from the wall clock instead. Mid-month the resulting offset is small (about one percentage point here). At the period edges it is not: between 17:00 and 24:00 on the last day of a month, the true period elapsed is near 0% while `_month_elapsed_pct` returns near 100%, so a freshly reset quota would be drawn as heavily under-spent (a wide green bar) instead of at the start of a new period. The mirror-image error occurs on the first hours of the new calendar month.

Whether the upstream `/api/oauth/usage` response actually exposes a reset timestamp was not verified — that requires a live token and was out of scope for this note. If it does, it should be preferred over the local inference.

## Test coverage

None. `anthrouter/ui/src/test/App.test.tsx` stubs several admin routes but never renders `OAuthCard` or asserts on segment widths, and there is no Python test exercising `_month_elapsed_pct`. Nothing pins either the current behaviour or the intended behaviour, so a change to the baseline would not be caught by the suite in either direction.

## Verdict

The red segment is drawn correctly for the numbers it is given, and those numbers are internally consistent. It is nonetheless misleading in practice, because it measures spend against a calendar-linear burn rate over the UTC month rather than against the actual usage period prorated over workdays. For a workday-only spend pattern the segment overstates overuse by roughly 2.7× early in the month, and it is materially wrong for the hours around the 17:00 period boundary.

If the meter is meant to answer "am I ahead of budget?", the baseline needs the real period start and end and a workday-aware proration. If it is meant to answer the narrower "have I spent more than a uniform calendar burn?", the current behaviour is correct as written but should be labelled as such, since the current presentation reads as a budget-pace indicator.

## Locations

- Pace baseline: `anthrouter/oauth_usage.py:23-27`, called at `:194`.
- Upstream response parsing (no period field read): `anthrouter/oauth_usage.py:151-200`.
- Admin endpoint passthrough: `anthrouter/admin.py:437-449`.
- Meter rendering: `anthrouter/ui/src/components/OAuthCard.tsx:73-104`.
- Tests: none covering the meter (`anthrouter/ui/src/test/App.test.tsx`, `tests/`).
