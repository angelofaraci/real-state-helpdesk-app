"""Unit tests for `app.core.sla_defaults` (stage 7 — analytics, PR2).

`SLA_WARNING_ELAPSED_FRACTION` is promoted here from
`app.workers.sla`'s previously-private `_SLA_WARNING_ELAPSED_FRACTION` —
this module is the shared, session/repository-free home for SLA-related
constants (see the module's own docstring), so a later stage-7 metric
that also needs the 80%-elapsed threshold (e.g. an "at risk" rollup) can
import it without pulling in the worker module.
"""

from __future__ import annotations

from app.core import sla_defaults


def test_sla_warning_elapsed_fraction_is_80_percent() -> None:
    assert sla_defaults.SLA_WARNING_ELAPSED_FRACTION == 0.8
