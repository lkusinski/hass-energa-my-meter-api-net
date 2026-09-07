"""Unit tests for HA RecorderAdapter and statistic validation.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 9 & 12.
Kryteria akceptacji:
- Monotoniczność sumy.
- Odrzucanie wartości ujemnych i skoków > MAX_HOURLY_KWH.
- Bezpieczeństwo metadanych StatisticMetaData.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from custom_components.energa_mobile.ha.recorder_adapter import (
    RecorderAdapter,
    validate_and_clean_statistics,
)


def test_validate_and_clean_statistics_monotonic():
    """Verify that declining sums are clamped to ensure strictly non-decreasing series."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    raw = [
        {"start": t0, "state": 1.5, "sum": 10.0},
        {"start": t0 + timedelta(hours=1), "state": 2.0, "sum": 8.0},  # Sum dropped!
        {"start": t0 + timedelta(hours=2), "state": 1.0, "sum": 15.0},
    ]

    cleaned = validate_and_clean_statistics(raw, last_known_sum=5.0)
    assert len(cleaned) == 3
    assert cleaned[0]["sum"] == 10.0
    # Sum dropped from 10.0 to 8.0 -> clamped to 10.0
    assert cleaned[1]["sum"] == 10.0
    assert cleaned[2]["sum"] == 15.0


def test_validate_and_clean_statistics_spike_and_negative():
    """Verify spike guard (> MAX_HOURLY_KWH) and negative states are rejected."""
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    raw = [
        {"start": t0, "state": -5.0},                     # Negative -> skip
        {"start": t0 + timedelta(hours=1), "state": 1.2},  # OK
        {"start": t0 + timedelta(hours=2), "state": 75.0}, # Spike > 50.0 -> skip
        {"start": t0 + timedelta(hours=3), "state": 0.8},  # OK
    ]

    cleaned = validate_and_clean_statistics(raw, max_value=50.0, last_known_sum=100.0)
    assert len(cleaned) == 2
    assert cleaned[0]["state"] == 1.2
    assert cleaned[0]["sum"] == 101.2
    assert cleaned[1]["state"] == 0.8
    assert cleaned[1]["sum"] == 102.0


def test_validate_and_clean_statistics_tz_normalization():
    """Verify naive datetimes are converted to UTC."""
    t_naive = datetime(2026, 9, 1, 12, 0)
    raw = [{"start": t_naive, "state": 2.0}]
    cleaned = validate_and_clean_statistics(raw)
    assert len(cleaned) == 1
    assert cleaned[0]["start"].tzinfo == timezone.utc


def test_recorder_adapter_build_metadata():
    """Verify standard schema for StatisticMetaData."""
    adapter = RecorderAdapter(hass=MagicMock())
    meta = adapter.build_metadata(
        statistic_id="energa_mobile:PL_123456__grid_import_total",
        name="Pobór energii",
        unit="kWh",
        is_energy=True,
    )
    assert meta.statistic_id == "energa_mobile:PL_123456__grid_import_total"
    assert meta.unit_of_measurement == "kWh"
    assert meta.has_mean is False
    assert meta.has_sum is True
    assert meta.unit_class == "energy"


def test_recorder_adapter_import_energy():
    """Verify import_energy_statistics delegates cleaned stats to HA."""
    hass_mock = MagicMock()
    adapter = RecorderAdapter(hass=hass_mock)

    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    stats = [
        {"start": t0, "state": 1.0},
        {"start": t0 + timedelta(hours=1), "state": 2.5},
    ]

    count = adapter.import_energy_statistics(
        statistic_id="sensor.energa_12345_import",
        statistics=stats,
        last_known_sum=50.0,
    )

    assert count == 2


def test_recorder_adapter_empty_batch():
    """Verify handling of empty or None statistics."""
    adapter = RecorderAdapter(hass=MagicMock())
    assert adapter.import_statistics(MagicMock(), []) == 0
