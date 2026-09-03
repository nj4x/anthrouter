# Design Decisions Reached During Grilling

These are the Architecture Decision Records produced by a `/grilling` design interview
about fixing the OAuth usage meter's pace baseline in the anthrouter admin UI.

Context for the review: the meter's red "overuse" segment measures spend against a
calendar-linear burn rate over the UTC month. Because real spend is concentrated on
workdays, this overstates overuse by roughly 2.7x early in the month. The interview
settled how to re-baseline the meter, how to determine workdays, and how to expose
two selectable modes to the user.

Supporting research: `docs/research/oauth-meter-rendering-investigation.md`

---

- docs/adr/0008-workday-aware-pace-baseline.md
