"""Performance guard for the identity-deduplicating read (issue #4).

The regression that broke config-entry setup was a per-row correlated subquery
in ``CanonicalStorage.get_readings`` (O(n^2)). This test builds a realistically
sized canonical base and asserts the read stays well inside a generous budget,
so a return to quadratic behaviour fails loudly instead of silently blowing the
90 s first-refresh ceiling in production.
"""

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from custom_components.energa_mobile.core.readings.models import IntervalReading
from custom_components.energa_mobile.storage.sqlite.database import CanonicalStorage

# 2 identities x 2 registers x 365 days of hourly data ~= 35k rows.
DAYS = 365
IDENTITIES = [("PPE_372197", "372197"), ("372197", "11685328")]
REGISTERS = ["import_1", "export_1"]
# Very generous: the optimized read is <1 s for this size; the old O(n^2)
# version would take far longer. Guards against a catastrophic regression
# without being flaky on slow CI runners.
BUDGET_SECONDS = 30.0


def test_get_readings_stays_within_budget(tmp_path):
    storage = CanonicalStorage(str(tmp_path / "perf.db"))
    t0 = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    batch: list[IntervalReading] = []
    for h in range(DAYS * 24):
        dt = t0 + timedelta(hours=h)
        for reg in REGISTERS:
            is_export = reg.startswith("export")
            for ppe, meter in IDENTITIES:
                batch.append(
                    IntervalReading(
                        ppe_id=ppe,
                        meter_id=meter,
                        register=reg,
                        interval_start_utc=dt,
                        resolution="1h",
                        import_kwh=Decimal("0.0") if is_export else Decimal("1.250"),
                        export_kwh=Decimal("0.500") if is_export else Decimal("0.0"),
                        source="energa",
                    )
                )
    storage.insert_readings_idempotent(batch)

    start = time.perf_counter()
    rows = storage.get_readings(
        ppe_id="PPE_372197", meter_id="372197", resolution="1h"
    )
    elapsed = time.perf_counter() - start

    # Both registers survive (see test_get_readings_keeps_distinct_registers_same_hour).
    assert len(rows) == DAYS * 24 * len(REGISTERS)
    assert elapsed < BUDGET_SECONDS, f"get_readings took {elapsed:.1f}s (budget {BUDGET_SECONDS}s)"
