# Anthrouter

A single-backend Anthropic Messages API proxy that classifies requests, rewrites model tiers, sanitizes system prompts, and records activity to SQLite for a read-only admin UI.

## Language

### Request routing

**Tier**: One of three routing levels (trivial, standard, deep) that maps an inbound request to a model in the configured tier.
_Avoid_: Level, class, category

**Routing decision**: The outcome of `route_model()` — a chosen tier plus a stable `ReasonCode` explaining why.
_Avoid_: Routing result, classification result

**Classifier**: The lightweight LLM call (or deterministic rules pass) that scores request complexity and produces a tier.
_Avoid_: Ranker, scorer

**Walk-back**: Replaying the session's last classified tier for a text-less continuation turn instead of reclassifying boilerplate.
_Avoid_: Cache replay, tier replay

### OAuth usage metering

**Usage period**: The billing cycle for an OAuth quota — anchored to UTC midnight at the calendar month boundary (which equals 17:00 PDT).
_Avoid_: Billing period, reset period, monthly period

**Pace baseline**: The reference spend rate against which actual usage is compared in the meter; expressed as a percentage of the usage period elapsed, prorated either over workdays or calendar days.
_Avoid_: Burn rate, expected spend, budget line

**Workday mode**: Pace baseline computed by prorating the quota over Mon–Fri days in the usage period, using a configured or system-detected local timezone to determine which days are weekdays.
_Avoid_: Business day mode, weekday mode

**Calendar mode**: Pace baseline computed by prorating the quota uniformly over all calendar days in the usage period.
_Avoid_: Linear mode, uniform mode

### Sanitization

**Volatile block**: A system-prompt segment (e.g. a per-request billing header) that changes every turn and defeats prompt caching; the sanitizer optionally strips these before upstream dispatch.
_Avoid_: Dynamic block, ephemeral block
