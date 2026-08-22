"""Container smoke test for the `tzdata` dependency pin (stage 6 — queues +
SLA). `python:3.12-slim` (see `Dockerfile`) has no guaranteed system IANA
timezone database, unlike most CI runners/dev machines — invisible until
the built container runs. Pinning `tzdata` guarantees `zoneinfo` can
always resolve a key via `importlib.resources`. This test clears
`zoneinfo`'s search path (`TZPATH`) to simulate that slim-container gap,
so resolution can only succeed through the `tzdata` package.
"""

import zoneinfo


def test_zoneinfo_resolves_business_hours_timezone_without_system_tzdata() -> None:
    """`America/Argentina/Buenos_Aires` (the platform default timezone —
    see `app.core.sla_defaults.DEFAULT_TIMEZONE`) must resolve via the
    `tzdata` package alone, with no system tz database on `TZPATH`."""
    zoneinfo.reset_tzpath(to=())
    try:
        tz = zoneinfo.ZoneInfo("America/Argentina/Buenos_Aires")
    finally:
        zoneinfo.reset_tzpath()

    assert tz.key == "America/Argentina/Buenos_Aires"
