"""Autoconsumption & Microgrid Household Engine.

Reference: Energa HA Skorygowana Architektura Docelowa, Rozdział 10 & 11.
Pure domain logic:
- Synchronizes hourly PV inverter production against delayed OSD Energa export/import buckets.
- Strictly eliminates the 'asynchronous 100% autoconsumption fallacy' by only evaluating
  hours where both Energa and Inverter have confirmed readings.
- Computes true household consumption:
    HomeConsumption[h] = Import[h] + max(0, PV[h] - Export[h])
- Computes gross savings (PLN brutto z VAT 23%) based on dynamic or tiered tariff rates
  (G11, G12, G12w) avoided by self-consuming local solar energy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import logging
from typing import Sequence
from zoneinfo import ZoneInfo

from .core.readings.models import IntervalReading
from .projections.forecast import determine_tariff_zone
from .tariff import G11_DEFAULT_FEES, G12W_DEFAULT_FEES, VAT_RATE, fees_from_options, tariff_family

_LOGGER = logging.getLogger(__name__)
WARSAW_TZ = ZoneInfo("Europe/Warsaw")


@dataclass(frozen=True)
class HourlyAutoconsumptionBucket:
    """One-hour synchronized energy bucket."""

    start_utc: datetime
    start_local: datetime
    pv_kwh: float
    export_kwh: float
    import_kwh: float
    autoconsumption_kwh: float
    home_consumption_kwh: float
    savings_pln_brutto: float
    tariff_zone: int
    unit_price_brutto: float


@dataclass
class AutoconsumptionSummary:
    """Summary metrics of synchronized autoconsumption."""

    # Autoconsumption energy (kWh)
    today_kwh: float = 0.0
    yesterday_kwh: float = 0.0
    mtd_kwh: float = 0.0

    # Total household consumption (kWh)
    today_home_consumption_kwh: float = 0.0
    yesterday_home_consumption_kwh: float = 0.0
    mtd_home_consumption_kwh: float = 0.0

    # PV production across synchronized hours (kWh)
    mtd_pv_kwh: float = 0.0

    # Key Performance Indicators (%)
    # Autoconsumption Ratio: % of generated solar power used on site
    autoconsumption_ratio_mtd: float = 0.0
    # Self Sufficiency Ratio: % of total home energy covered by own solar power
    self_sufficiency_ratio_mtd: float = 0.0

    # Financial savings (PLN brutto z VAT 23%)
    savings_today_pln: float = 0.0
    savings_yesterday_pln: float = 0.0
    savings_mtd_pln: float = 0.0

    # Audit & telemetry status
    synced_until: datetime | None = None
    synced_hours_count: int = 0
    buckets: list[HourlyAutoconsumptionBucket] = field(default_factory=list)


def get_variable_unit_price_brutto(
    tariff: str,
    dt_local: datetime,
    options: dict | None = None,
) -> float:
    """Calculate the variable tariff electricity rate (PLN brutto/kWh) for a given hour.

    Includes energy price + variable distribution + quality + OZE + cogeneration fees.
    """
    family = tariff_family(tariff)
    fees = fees_from_options(options or {}, family)
    zone = determine_tariff_zone(tariff, dt_local)

    if zone == 1:
        # Day / Peak
        energy = fees.get("energy_day", 0.6107)
        grid_var = fees.get("grid_var_day", 0.4017)
    else:
        # Night / Off-peak
        energy = fees.get("energy_night", 0.3990)
        grid_var = fees.get("grid_var_night", 0.0851)

    quality = fees.get("quality", 0.0332)
    oze = fees.get("oze", 0.0073)
    cogen = fees.get("cogen", 0.0030)

    var_netto = energy + grid_var + quality + oze + cogen
    var_brutto = var_netto * (1.0 + VAT_RATE)
    return round(var_brutto, 4)


def normalize_hour_dt(dt: datetime) -> datetime:
    """Normalize datetime to top of the hour in UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def compute_autoconsumption_summary(
    pv_hourly_map: dict[datetime, float],
    energa_readings: Sequence[IntervalReading],
    tariff: str = "G12W",
    options: dict | None = None,
    now_dt: datetime | None = None,
) -> AutoconsumptionSummary:
    """Compute synchronized hour-by-hour autoconsumption and home consumption.

    Args:
        pv_hourly_map: Map of hour UTC datetime -> inverter solar generation in kWh.
        energa_readings: Sequence of canonical Energa hourly interval readings.
        tariff: Active tariff code (e.g. "G11", "G12", "G12W").
        options: Integration options containing price and fee overrides.
        now_dt: Current reference datetime (defaults to utcnow).

    Returns:
        AutoconsumptionSummary containing MTD, today, yesterday, and ratio KPIs.
    """
    if now_dt is None:
        now_utc = datetime.now(timezone.utc)
    else:
        now_utc = now_dt.astimezone(timezone.utc) if now_dt.tzinfo else now_dt.replace(tzinfo=timezone.utc)

    now_local = now_utc.astimezone(WARSAW_TZ)
    today_date = now_local.date()
    yesterday_date = today_date - timedelta(days=1)
    month_start_date = today_date.replace(day=1)

    # Normalize PV hourly map to top-of-hour UTC
    normalized_pv: dict[datetime, float] = {}
    for dt_raw, kwh in pv_hourly_map.items():
        if kwh is not None and kwh >= 0:
            h_utc = normalize_hour_dt(dt_raw)
            # If multiple points land in the same hour, take max or sum
            normalized_pv[h_utc] = max(normalized_pv.get(h_utc, 0.0), float(kwh))

    # Index Energa readings by hour UTC
    energa_by_hour: dict[datetime, tuple[float, float]] = {}
    for r in energa_readings:
        if r.resolution != "1h":
            continue
        h_utc = normalize_hour_dt(r.interval_start_utc)
        imp_val = float(r.import_kwh) if r.import_kwh is not None else 0.0
        exp_val = float(r.export_kwh) if r.export_kwh is not None else 0.0
        # If reading already seen, pick highest or latest
        energa_by_hour[h_utc] = (imp_val, exp_val)

    # Find intersection of hours where BOTH Energa and PV data are present
    common_hours = sorted(set(normalized_pv.keys()).intersection(energa_by_hour.keys()))

    summary = AutoconsumptionSummary()
    if not common_hours:
        return summary

    total_mtd_autoconsumption = 0.0
    total_mtd_home_consumption = 0.0
    total_mtd_pv = 0.0
    total_mtd_savings = 0.0

    today_autoconsumption = 0.0
    today_home_consumption = 0.0
    today_savings = 0.0

    yesterday_autoconsumption = 0.0
    yesterday_home_consumption = 0.0
    yesterday_savings = 0.0

    latest_synced: datetime | None = None

    for h_utc in common_hours:
        h_local = h_utc.astimezone(WARSAW_TZ)
        h_date = h_local.date()

        # Only process hours within current month
        if h_date < month_start_date:
            continue

        pv_kwh = normalized_pv[h_utc]
        imp_kwh, exp_kwh = energa_by_hour[h_utc]

        # Autoconsumption in hour h: energy from PV not exported to grid
        # Guard against slight timing discrepancies: cannot exceed PV generation
        autoconsumption_kwh = max(0.0, round(pv_kwh - exp_kwh, 4))
        if autoconsumption_kwh > pv_kwh:
            autoconsumption_kwh = pv_kwh

        # Real household consumption: grid import + PV consumed on site
        home_kwh = round(imp_kwh + autoconsumption_kwh, 4)

        zone = determine_tariff_zone(tariff, h_local)
        unit_price = get_variable_unit_price_brutto(tariff, h_local, options)
        savings_brutto = round(autoconsumption_kwh * unit_price, 4)

        bucket = HourlyAutoconsumptionBucket(
            start_utc=h_utc,
            start_local=h_local,
            pv_kwh=pv_kwh,
            export_kwh=exp_kwh,
            import_kwh=imp_kwh,
            autoconsumption_kwh=autoconsumption_kwh,
            home_consumption_kwh=home_kwh,
            savings_pln_brutto=savings_brutto,
            tariff_zone=zone,
            unit_price_brutto=unit_price,
        )
        summary.buckets.append(bucket)

        total_mtd_autoconsumption += autoconsumption_kwh
        total_mtd_home_consumption += home_kwh
        total_mtd_pv += pv_kwh
        total_mtd_savings += savings_brutto

        if h_date == today_date:
            today_autoconsumption += autoconsumption_kwh
            today_home_consumption += home_kwh
            today_savings += savings_brutto
        elif h_date == yesterday_date:
            yesterday_autoconsumption += autoconsumption_kwh
            yesterday_home_consumption += home_kwh
            yesterday_savings += savings_brutto

        if latest_synced is None or h_local > latest_synced:
            latest_synced = h_local

    # Store aggregated totals
    summary.today_kwh = round(today_autoconsumption, 3)
    summary.yesterday_kwh = round(yesterday_autoconsumption, 3)
    summary.mtd_kwh = round(total_mtd_autoconsumption, 3)

    summary.today_home_consumption_kwh = round(today_home_consumption, 3)
    summary.yesterday_home_consumption_kwh = round(yesterday_home_consumption, 3)
    summary.mtd_home_consumption_kwh = round(total_mtd_home_consumption, 3)

    summary.mtd_pv_kwh = round(total_mtd_pv, 3)

    # Calculate ratios
    if total_mtd_pv > 0:
        summary.autoconsumption_ratio_mtd = round(
            min(100.0, (total_mtd_autoconsumption / total_mtd_pv) * 100.0), 1
        )
    else:
        summary.autoconsumption_ratio_mtd = 0.0

    if total_mtd_home_consumption > 0:
        summary.self_sufficiency_ratio_mtd = round(
            min(100.0, (total_mtd_autoconsumption / total_mtd_home_consumption) * 100.0), 1
        )
    else:
        summary.self_sufficiency_ratio_mtd = 0.0

    summary.savings_today_pln = round(today_savings, 2)
    summary.savings_yesterday_pln = round(yesterday_savings, 2)
    summary.savings_mtd_pln = round(total_mtd_savings, 2)

    summary.synced_until = latest_synced
    summary.synced_hours_count = len(summary.buckets)

    return summary
