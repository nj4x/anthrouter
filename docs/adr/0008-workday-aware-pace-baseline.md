---
artifact-type: adr
lineage-rules: root
---

# Workday-aware pace baseline for the OAuth usage meter

The OAuth usage meter's pace baseline (`month_elapsed_pct`) was computed as a fraction of the calendar UTC month elapsed, treating all days uniformly. Because actual spend is concentrated on workdays, this overstated overuse by roughly 2.7× early in the month. We replaced it with a workday-aware proration (Mon–Fri in the configured local timezone) as the default, keeping calendar-day proration as an opt-in second mode.

## Decisions

**Period boundary.** The usage period is the calendar UTC month: `[2026-09-01 00:00:00 UTC, 2026-10-01 00:00:00 UTC)`. This aligns with the upstream quota reset, which occurs at 17:00 Pacific Time every month — PDT (UTC-7) converts to 00:00 UTC, PST (UTC-8) to 01:00 UTC. The boundary is inferred from wall-clock UTC at request time (via `datetime.now(tz=UTC)`). No attempt is made to read a `period_start` or `period_end` field from the upstream `/api/oauth/usage` response; if such a field exists in a future API version, it should be preferred over local inference (follow-up).

**Workday denominator and elapsed count.** Workdays are Mon–Fri only in the configured local timezone (no holiday exclusions). The denominator is the count of all local weekdays that overlap the UTC period (inclusive of partial-day overlap at the edges). For September 2026, Aug 31 is a Monday and Sep 30 is a Wednesday in Pacific Time. The UTC period [Sep 1 00:00 UTC, Oct 1 00:00 UTC) overlaps Aug 31 because Sep 1 00:00 UTC is Aug 31 17:00 PDT — 7 hours into that local calendar day — so Aug 31 counts. The denominator is 23: Aug 31 (1) + Sep 1–4 Mon–Fri (4) + Sep 7–11, 14–18, 21–25 (15) + Sep 28–30 Mon–Wed (3) = 23.

The elapsed count is fractional: number of fully elapsed weekdays in the period plus the fraction of the current local weekday already elapsed (proportion of the 24-hour local day consumed). On a local weekend day the fractional contribution is zero, so the baseline freezes from Friday close until Monday midnight. At period open (Aug 31 17:00 PDT), Aug 31 is already ~70.8% complete (17 of 24 hours), so the baseline opens at 0.708/23 ≈ 3.1%, not 0%; this is correct — that portion of Aug 31 was already spent. Any spend on Saturday or Sunday reads as over-pace; this is intentional (no workday budget was allocated to those hours).

**Timezone detection and configuration.** The server detects the local timezone at startup via `time.tzname` (or `zoneinfo` APIs on Python 3.9+). If detection returns `UTC` (common in containers), the fallback is `America/Los_Angeles`, with the rationale that the reference deployment is Pacific-anchored and explicit configuration is the only way to specify a genuinely UTC-operated deployment. This fallback can be overridden with `--oauth-usage-timezone <name>` (env: `ANTHROUTER_OAUTH_USAGE_TIMEZONE`). An unrecognized or unavailable timezone name causes a fatal error at startup. The resolved timezone name is included in the HTTP response (see below) so the UI can label the baseline accurately.

**Two modes, both computed server-side.** The server returns two separate elapsed-percentage fields — `workday_elapsed_pct` and `calendar_elapsed_pct` — so the UI can switch without a network round-trip. The response also includes `workday_timezone` (the name of the configured timezone), `period_start` and `period_end` (ISO-8601 UTC strings), and `period_workday_count` (the total number of weekdays in the period) for the UI to render an unambiguous period label. The existing `month_elapsed_pct` field is retained with calendar semantics for backward compatibility but deprecated.

**UI toggle with localStorage persistence.** A segmented control in `OAuthCard` switches the displayed mode. The choice is persisted in `localStorage` under the key `anthrouter.oauthMeterMode` (values `workdays` | `calendar`, default `workdays`). The selected mode drives the red overuse segment and the green underuse segment, as well as the colour thresholds (80% amber, 100% red). When localStorage is unavailable or corrupted, the mode defaults to `workdays`.

## Considered options

- **Server returns one value controlled by a query param**: rejected — requires a network round-trip on every toggle and shifts the state-management burden to the client.
- **Timezone-agnostic: always use UTC for period boundaries and workday counting**: rejected — UTC workdays (05:00 Mon UTC = 21:00 Sun PDT) do not align with the user's actual working hours and calendar perception.
- **Exclude US federal holidays from workday count**: rejected — requires a holiday calendar data file or third-party package, adds operational surface for calendar updates, and the monthly impact (1–2 day difference) is small relative to the complexity.
- **Strict UTC period edges (no partial-day overlap)**: rejected — would exclude Aug 31 (a Monday) from the September 2026 period entirely, since its local calendar day started 17 hours before the UTC period opened, misaligning with the 23-weekday expectation documented in the research.
- **Single mode (workday proration only, no calendar toggle)**: not chosen — the calendar mode was an explicit product requirement alongside the workday fix.

## Test coverage

The elapsed-percentage functions must be tested for:
- A mid-period workday (e.g. Sep 15 12:00 UTC)
- A weekend day (e.g. Sep 7 Saturday)
- The first period day, partial (e.g. Sep 1 00:00 UTC)
- The last period day, partial (e.g. Sep 30 23:59 UTC)

Each test must assert the correct elapsed and total counts, and the rendered metre (red and green segments) under both modes.

## References

- `docs/research/oauth-meter-rendering-investigation.md` — renders the September 2026 data point that anchors the 2.7× figure and the 23-weekday denominator.

## Consequences

- The meter now correctly represents spend against an actual working schedule, but only when the deployment's timezone is configured correctly. Containers without an explicit `--oauth-usage-timezone` will silently assume Pacific Time.
- Weekend spend is visible as over-pace by design, surfacing the reality that no budget was allocated for weekend activity. This is intentional.
- The fallback to Pacific Time is deliberate and documented; an operator who wants strict UTC must pass `--oauth-usage-timezone UTC` explicitly.
