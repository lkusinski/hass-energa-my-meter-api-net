"""Tests for idempotent re-import of 30 days data.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 12.
Kryteria akceptacji:
- "Duplicate import: Ten sam payload nie tworzy drugiego odczytu, lotu ani statystyki."
- "HA reimport: Reimport historii nie tworzy skokow, duplikatow ani odwrocenia import/export."
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from custom_components.energa_mobile.core.readings.models import IntervalReading
from custom_components.energa_mobile.storage.sqlite.database import CanonicalStorage


def test_idempotent_30_day_reimport(tmp_path):
    """Simulate 30-day live reimport: verify zero duplicate rows in SQLite."""
    db_path = str(tmp_path / "test_reimport.db")
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

    # 2. Re-import the exact same 30 days during active operation
    inserted_second = storage.insert_readings_idempotent(points)
    assert inserted_second == 0  # Zero duplicate inserts!
    assert storage.get_readings_count() == 720  # DB count did not double
