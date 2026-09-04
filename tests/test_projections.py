"""Unit tests for statistics and flow projections.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 4 & 9.
Verifies:
- Stable statistic IDs based on PPE.
- Deterministic cumulative sums.
- Anchored running sums across re-imports.
- Virtual bank charge/discharge flow projections.
"""

from datetime import datetime, timezone
from decimal import Decimal

from custom_components.energa_mobile.core.readings.models import IntervalReading
from custom_components.energa_mobile.projections.statistics import (
    build_cumulative_statistic_data,
    build_statistic_id,
    build_virtual_bank_flow_data,
)


def test_build_statistic_id():
    stat_id = build_statistic_id("PL_1234567890", "grid_import", "total")
    assert stat_id == "energa_mobile:PL_1234567890__grid_import_total"

    stat_id_zone = build_statistic_id("PL_1234567890", "grid_import", "day")
    assert stat_id_zone == "energa_mobile:PL_1234567890__grid_import_day"


def test_build_cumulative_statistic_data_monotonic():
    t1 = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 4, 11, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

    readings = [
        IntervalReading("PPE1", "m1", "1.8.0", t1, import_kwh=Decimal("1.50")),
        IntervalReading("PPE1", "m1", "1.8.0", t2, import_kwh=Decimal("2.25")),
        IntervalReading("PPE1", "m1", "1.8.0", t3, import_kwh=Decimal("0.75")),
    ]

    # Without anchor (start from 0)
    stats = build_cumulative_statistic_data(readings)
    assert len(stats) == 3
    assert stats[0]["state"] == 1.50
    assert stats[0]["sum"] == 1.50
    assert stats[1]["state"] == 2.25
    assert stats[1]["sum"] == 3.75
    assert stats[2]["state"] == 0.75
    assert stats[2]["sum"] == 4.50

    # With anchor (re-import continuing from 100.00 kWh)
    anchored = build_cumulative_statistic_data(readings, initial_sum=Decimal("100.00"))
    assert len(anchored) == 3
    assert anchored[0]["sum"] == 101.50
    assert anchored[1]["sum"] == 103.75
    assert anchored[2]["sum"] == 104.50


def test_virtual_bank_flows():
    t1 = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    readings = [
        IntervalReading("PPE1", "m1", "1.8.0", t1, import_kwh=Decimal("2.0"), export_kwh=Decimal("10.0")),
    ]

    # Coefficient 0.8: 10 kWh export -> 8.0 kWh charge
    charge, discharge = build_virtual_bank_flow_data(readings, coefficient=Decimal("0.8"))
    assert len(charge) == 1
    assert charge[0]["state"] == 8.0
    assert charge[0]["sum"] == 8.0

    assert len(discharge) == 1
    assert discharge[0]["state"] == 2.0
    assert discharge[0]["sum"] == 2.0
