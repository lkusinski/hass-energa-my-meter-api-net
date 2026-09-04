"""Unit tests for Energa API adapter and payload normalizer.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 5.
Verifies:
- Raw payload archive creation with SHA-256 hash.
- Canonical IntervalReading extraction with UTC timestamps and Decimals.
- Import vs export register routing.
- Defensive handling of empty or corrupt payloads.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json

from custom_components.energa_mobile.adapters.energa.client import (
    normalize_chart_payload,
)


def test_normalize_chart_payload_with_timestamps():
    # Sample JSON returned by Energa chart API
    sample_payload = json.dumps({
        "response": [
            {"time": 1788516000000, "value": 0.852},  # 2026-09-04 10:00:00 UTC
            {"time": 1788519600000, "value": 1.204},  # 2026-09-04 11:00:00 UTC
        ]
    })

    obs, readings = normalize_chart_payload(
        raw_json_str=sample_payload,
        endpoint="/dp/chart/day",
        ppe_id="PPE_WIŚNIOWA",
        meter_id="meter_123",
        register="import_total",
        resolution="1h",
    )

    # Observation verification
    assert obs.source == "energa"
    assert obs.endpoint == "/dp/chart/day"
    assert len(obs.payload_hash) == 64
    assert obs.raw_payload == sample_payload

    # Readings verification
    assert len(readings) == 2
    assert readings[0].ppe_id == "PPE_WIŚNIOWA"
    assert readings[0].meter_id == "meter_123"
    assert readings[0].register == "import_total"
    assert readings[0].import_kwh == Decimal("0.8520")
    assert readings[0].export_kwh == Decimal("0.0")
    assert readings[0].observation_id == obs.observation_id
    assert readings[0].interval_start_utc == datetime.fromtimestamp(1788516000, tz=timezone.utc)

    assert readings[1].import_kwh == Decimal("1.2040")


def test_normalize_chart_export_register():
    sample_payload = json.dumps([
        {"time": 1788516000000, "value": 2.500},
    ])

    obs, readings = normalize_chart_payload(
        raw_json_str=sample_payload,
        endpoint="/dp/chart/day",
        ppe_id="PPE_WIŚNIOWA",
        meter_id="meter_123",
        register="export_total",
    )

    assert len(readings) == 1
    assert readings[0].import_kwh == Decimal("0.0")
    assert readings[0].export_kwh == Decimal("2.5000")


def test_normalize_corrupt_or_empty_json():
    obs, readings = normalize_chart_payload(
        raw_json_str="INVALID_JSON",
        endpoint="/dp/chart",
        ppe_id="PPE_1",
        meter_id="m1",
    )
    assert obs is not None
    assert readings == []
