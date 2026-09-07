"""Tests for idempotent re-import of 30 days data.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 12.
Kryteria akceptacji:
- "Duplicate import: Ten sam payload nie tworzy drugiego odczytu, lotu ani statystyki."
- "HA reimport: Reimport historii nie tworzy skokow, duplikatow ani odwrocenia import/export."
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import tempfile
import os
import pytest

from custom_components.energa_mobile.storage.sqlite.database import CanonicalStorage
from custom_components.energa_mobile.core.readings.models import IntervalReading
from custom_components.energa_mobile.projections.statistics import build_cumulative_statistic_data
from custom_components.energa_mobile.ha.recorder_adapter import validate_and_clean_statistics


def test_idempotent_30_day_reimport():
    """Simulate 30-day live reimport: verify zero duplicate rows in SQLite and zero sum jumps."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_reimport.db")
        storage = CanonicalStorage(db_path)

        t_start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
        ppe = "PL_TEST_PPE_01"
        meter = "METER_999"

        # Generate 30 days of hourly readings (30 * 24 = 720 points)
        points: list[IntervalReading] = []
        for h in range(720):
            dt = t_start + timedelta(hours=h)
            points.append(
                IntervalReading(
                    ppe_id=ppe,
                    meter_id=meter,
                    register="import",
                    interval_start_utc=dt,
                    resolution="1h",
                    import_kwh=Decimal("1.250"),
                    export_kwh=Decimal("0.000"),
                    quality="ok",
                    source="energa",
                )
            )

        # 1. First import
        inserted_first = storage.insert_readings_idempotent(points)
        assert inserted_first == 720
        assert storage.get_readings_count() == 720

        # Project to cumulative statistics
        stats_run1 = build_cumulative_statistic_data(points, initial_sum=Decimal("100.0"))
        assert len(stats_run1) == 720
        final_sum_run1 = stats_run1[-1]["sum"]
        # Expected final sum: 100.0 + (720 * 1.25) = 100.0 + 900.0 = 1000.0
        assert final_sum_run1 == 1000.0

        # 2. Re-import the exact same 30 days during active operation
        inserted_second = storage.insert_readings_idempotent(points)
        assert inserted_second == 0  # Zero duplicate inserts!
        assert storage.get_readings_count() == 720  # DB count did not double

        # Re-project to cumulative statistics
        stats_run2 = build_cumulative_statistic_data(points, initial_sum=Decimal("100.0"))
        assert len(stats_run2) == 720
        final_sum_run2 = stats_run2[-1]["sum"]

        # Final sum must be 100% identical: no jumps, no doubling
        assert final_sum_run2 == final_sum_run1 == 1000.0

        # Clean validation run
        cleaned = validate_and_clean_statistics(stats_run2, last_known_sum=100.0)
        assert len(cleaned) == 720
        assert cleaned[-1]["sum"] == 1000.0
