"""Deterministic projections from Canonical Storage into Home Assistant Statistics.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 4, 9 & 10.
Key invariants:
- Stable statistic IDs based on PPE (survives meter replacements).
- Cumulative sums are strictly monotonically increasing.
- Derived deterministically from canonical readings (never directly editing Recorder tables).
- Anchoring support: preserves running sum across partial re-imports.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from ..core.readings.models import IntervalReading


def build_statistic_id(ppe_id: str, metric: str, zone: str = "total") -> str:
    """Generate a stable statistic ID tied to PPE logical identity.

    Examples:
        energa_mobile:PL_123456__grid_import_total
        energa_mobile:PL_123456__grid_import_day
        energa_mobile:PL_123456__grid_export_total
        energa_mobile:PL_123456__battery_charge
        energa_mobile:PL_123456__battery_discharge
    """
    clean_ppe = ppe_id.replace(":", "_").replace("/", "_").strip()
    return f"energa_mobile:{clean_ppe}__{metric}_{zone}"


def build_cumulative_statistic_data(
    readings: list[IntervalReading],
    initial_sum: Decimal = Decimal("0.0"),
    use_export: bool = False,
) -> list[dict]:
    """Derive monotonically increasing StatisticData dicts for Home Assistant Recorder.

    Args:
        readings: List of canonical IntervalReading objects sorted chronologically.
        initial_sum: Anchored starting sum before the first reading in the batch.
        use_export: If True, sums export_kwh; else sums import_kwh.

    Returns:
        List of dicts formatted for Home Assistant `async_import_statistics`:
        [{"start": datetime, "state": float, "sum": float}, ...]
    """
    if not readings:
        return []

    sorted_readings = sorted(readings, key=lambda r: r.interval_start_utc)
    running_sum = initial_sum
    out: list[dict] = []

    for r in sorted_readings:
        kwh = r.export_kwh if use_export else r.import_kwh
        # Defensive clamp against negative values or corruption
        if kwh < Decimal("0.0"):
            kwh = Decimal("0.0")

        running_sum += kwh
        # Recorder StatisticData requires float representation for state and sum
        out.append({
            "start": r.interval_start_utc,
            "state": float(kwh),
            "sum": float(running_sum),
        })

    return out


def build_virtual_bank_flow_data(
    readings: list[IntervalReading],
    coefficient: Decimal = Decimal("0.8"),
    initial_charge_sum: Decimal = Decimal("0.0"),
    initial_discharge_sum: Decimal = Decimal("0.0"),
) -> tuple[list[dict], list[dict]]:
    """Derive virtual battery charge and discharge flows from canonical readings.

    In Polish prosumer net-metering:
    - Charge (ładowanie wirtualnego magazynu) = export * coefficient
    - Discharge (rozładowanie / odbiór) = min(import, current_available_bank_balance)

    Returns:
        (charge_stats, discharge_stats)
    """
    if not readings:
        return [], []

    sorted_readings = sorted(readings, key=lambda r: r.interval_start_utc)
    charge_stats: list[dict] = []
    discharge_stats: list[dict] = []

    running_charge = initial_charge_sum
    running_discharge = initial_discharge_sum

    for r in sorted_readings:
        # 1. Charge from export
        exp = max(Decimal("0.0"), r.export_kwh)
        charge_kwh = round(exp * coefficient, 3)
        running_charge += charge_kwh

        charge_stats.append({
            "start": r.interval_start_utc,
            "state": float(charge_kwh),
            "sum": float(running_charge),
        })

        # 2. Discharge from import
        imp = max(Decimal("0.0"), r.import_kwh)
        discharge_kwh = imp
        running_discharge += discharge_kwh

        discharge_stats.append({
            "start": r.interval_start_utc,
            "state": float(discharge_kwh),
            "sum": float(running_discharge),
        })

    return charge_stats, discharge_stats
