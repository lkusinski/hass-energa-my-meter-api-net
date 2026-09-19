"""Energa API Client adhering to target acquisition contract.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 5 & 6.
Invariants:
- Returns raw payload (SourceObservation) alongside normalized domain records (IntervalReading).
- Semi-open time ranges [start, end) (no hardcoded YYYY-MM-31).
- Preserves raw API response for full provenance and auditability.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from ...core.readings.models import IntervalReading, SourceObservation

_LOGGER = logging.getLogger(__name__)

WARSAW_TZ = ZoneInfo("Europe/Warsaw")


def normalize_chart_payload(
    raw_json_str: str,
    endpoint: str,
    ppe_id: str,
    meter_id: str,
    register: str = "1.8.0",
    resolution: str = "1h",
) -> tuple[SourceObservation, list[IntervalReading]]:
    """Normalize a raw Energa chart response into an Observation and IntervalReadings.

    Extracts hourly readings with UTC timestamps and Decimal kWh values.
    Preserves raw JSON text in SourceObservation for provenance.
    """
    obs = SourceObservation.create(
        source="energa",
        endpoint=endpoint,
        http_status=200,
        raw_payload=raw_json_str,
    )

    readings: list[IntervalReading] = []
    try:
        data = json.loads(raw_json_str)
    except json.JSONDecodeError as err:
        _LOGGER.warning("Failed to decode Energa chart JSON: %s", err)
        return obs, []

    # API returns list of points or dict with "response" / "points"
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("response") or data.get("points") or []

    is_export = "export" in register or register.startswith("2.8")

    for item in items:
        if not isinstance(item, dict):
            continue

        # Extract timestamp: may be 'time', 'timestamp', 'date', or epoch in ms
        dt_val = None
        if "time" in item:
            # Epoch milliseconds or ISO string
            t_val = item["time"]
            if isinstance(t_val, (int, float)):
                dt_val = datetime.fromtimestamp(t_val / 1000.0, tz=timezone.utc)
            elif isinstance(t_val, str):
                try:
                    dt_val = datetime.fromisoformat(t_val.replace("Z", "+00:00"))
                except ValueError:
                    pass
        elif "date" in item and "hour" in item:
            # e.g. "2026-09-04", hour: 12
            try:
                d_part = date.fromisoformat(item["date"])
                h_part = int(item["hour"]) - 1  # 1-indexed to 0-indexed
                local_dt = datetime(d_part.year, d_part.month, d_part.day, h_part, 0, tzinfo=WARSAW_TZ)
                dt_val = local_dt.astimezone(timezone.utc)
            except (ValueError, TypeError):
                pass

        if not dt_val:
            continue

        # Extract kWh value
        val = item.get("value") or item.get("val") or item.get("kwh")
        if val is None:
            continue

        try:
            val_dec = Decimal(str(round(float(val), 4)))
        except (ValueError, TypeError):
            continue

        if val_dec < Decimal("0.0"):
            val_dec = Decimal("0.0")

        readings.append(
            IntervalReading(
                ppe_id=ppe_id,
                meter_id=meter_id,
                register=register,
                interval_start_utc=dt_val,
                resolution=resolution,
                import_kwh=Decimal("0.0") if is_export else val_dec,
                export_kwh=val_dec if is_export else Decimal("0.0"),
                quality="ok",
                source="energa",
                observation_id=obs.observation_id,
            )
        )

    return obs, readings
