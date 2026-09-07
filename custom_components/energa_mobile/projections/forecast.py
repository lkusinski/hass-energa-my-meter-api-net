"""Hourly Profile Forecasting Engine based on Canonical SQLite WAL readings.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 4, 10 & 11 (Etap 5).
Pure domain logic:
- Polish statutory public holiday awareness (fixed and Easter-based moveable feasts).
- Decomposes canonical historical readings into 24-hour Weekday vs. Weekend/Holiday profiles.
- Zone allocation for G11, G12, and G12w tariffs.
- Adaptive trend scaling comparing current month-to-date rate against baseline.
- Produces month-end projections for kWh (total, T1, T2) and full invoice cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
import logging
from typing import Sequence

from ..core.readings.models import IntervalReading
from ..tariff import compute_bill, fees_from_options

_LOGGER = logging.getLogger(__name__)


class DayType(str, Enum):
    """Classification of day for energy consumption profiling."""

    WEEKDAY = "weekday"
    WEEKEND = "weekend"


def compute_easter(year: int) -> date:
    """Compute Gregorian Easter Sunday using the Meeus/Jones/Butcher algorithm.

    Valid for all years in the Gregorian calendar.
    """
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def is_polish_holiday(d: date) -> bool:
    """Check whether a date is a statutory public holiday in Poland (Dni wolne od pracy).

    Statutory holidays under the Act of 18 January 1951 on Public Holidays:
    - 1 Jan (Nowy Rok)
    - 6 Jan (Trzech Króli)
    - Easter Sunday (Wielkanoc)
    - Easter Monday (Poniedziałek Wielkanocny)
    - 1 May (Święto Pracy)
    - 3 May (Święto Konstytucji 3 Maja)
    - Pentecost / Whit Sunday (Zielone Świątki - Easter + 49 days, always Sunday)
    - Corpus Christi (Boże Ciało - Easter + 60 days)
    - 15 Aug (Wniebowzięcie NMP / Święto Wojska Polskiego)
    - 1 Nov (Wszystkich Świętych)
    - 11 Nov (Święto Niepodległości)
    - 25 Dec (Boże Narodzenie 1. dzień)
    - 26 Dec (Boże Narodzenie 2. dzień)
    """
    # 1. Fixed-date holidays
    fixed = {
        (1, 1),
        (1, 6),
        (5, 1),
        (5, 3),
        (8, 15),
        (11, 1),
        (11, 11),
        (12, 25),
        (12, 26),
    }
    if (d.month, d.day) in fixed:
        return True

    # 2. Moveable Easter-based holidays
    easter = compute_easter(d.year)
    easter_monday = easter + timedelta(days=1)
    whit_sunday = easter + timedelta(days=49)
    corpus_christi = easter + timedelta(days=60)

    if d in (easter, easter_monday, whit_sunday, corpus_christi):
        return True

    return False


def get_day_type(d: date) -> DayType:
    """Return DayType.WEEKEND for Saturdays, Sundays, and Polish holidays; WEEKDAY otherwise."""
    if d.weekday() in (5, 6) or is_polish_holiday(d):
        return DayType.WEEKEND
    return DayType.WEEKDAY


def determine_tariff_zone(tariff_code: str, dt_local: datetime) -> int:
    """Determine tariff zone (1 = peak/day, 2 = off-peak/night/weekend) for a local timestamp.

    - G11: Single-zone tariff (always zone 1).
    - G12: Two-zone tariff:
      * Peak (T1): 06:00-13:00 and 15:00-22:00.
      * Off-peak (T2): 13:00-15:00 and 22:00-06:00.
    - G12w: Weekend two-zone tariff:
      * Weekday: T1 (06:00-13:00 and 15:00-22:00), T2 (13:00-15:00 and 22:00-06:00).
      * Weekend & Polish holidays: 100% T2.
    """
    code = (tariff_code or "G11").upper().strip()
    if "G11" in code:
        return 1

    day_t = get_day_type(dt_local.date())
    hour = dt_local.hour

    if "G12W" in code:
        if day_t == DayType.WEEKEND:
            return 2
        # Weekday G12w
        if (6 <= hour < 13) or (15 <= hour < 22):
            return 1
        return 2

    if "G12" in code:
        # Standard G12 (zones apply on weekends too unless 'w' variant)
        if (6 <= hour < 13) or (15 <= hour < 22):
            return 1
        return 2

    # Default fallback
    return 1


@dataclass(frozen=True)
class HourlyProfileResult:
    """Detailed result of hourly profile forecasting."""

    forecast_import_total_kwh: Decimal
    forecast_import_t1_kwh: Decimal
    forecast_import_t2_kwh: Decimal
    forecast_export_total_kwh: Decimal
    forecast_export_t1_kwh: Decimal
    forecast_export_t2_kwh: Decimal
    method: str
    confidence_score: float
    history_days_count: int
    trend_factor: float
    forecast_payable_pln: Decimal | None = None
    bill_breakdown: dict | None = None


class HourlyProfileForecaster:
    """Forecaster engine decomposing interval readings into DayType-specific 24h profiles."""

    def __init__(
        self,
        readings: Sequence[IntervalReading],
        tariff_code: str = "G12w",
        tz_offset_hours: int = 2,  # Europe/Warsaw summer (+2) or winter (+1)
    ) -> None:
        """Initialize with historical readings."""
        self.tariff_code = tariff_code
        self.tz = timezone(timedelta(hours=tz_offset_hours))
        self._build_profiles(readings)

    def _build_profiles(self, readings: Sequence[IntervalReading]) -> None:
        """Decompose readings into 24-hour weekday and weekend profile curves."""
        # 24 buckets each: [hour] -> (total_kwh, count)
        self.weekday_import_sums = [Decimal("0.0")] * 24
        self.weekday_import_counts = [0] * 24
        self.weekday_export_sums = [Decimal("0.0")] * 24
        self.weekday_export_counts = [0] * 24

        self.weekend_import_sums = [Decimal("0.0")] * 24
        self.weekend_import_counts = [0] * 24
        self.weekend_export_sums = [Decimal("0.0")] * 24
        self.weekend_export_counts = [0] * 24

        observed_days: set[date] = set()

        for r in readings:
            # Convert UTC start to local Polish time
            local_dt = r.interval_start_utc.astimezone(self.tz)
            d = local_dt.date()
            observed_days.add(d)
            h = local_dt.hour
            day_t = get_day_type(d)

            imp = max(Decimal("0.0"), r.import_kwh)
            exp = max(Decimal("0.0"), r.export_kwh)

            if day_t == DayType.WEEKDAY:
                self.weekday_import_sums[h] += imp
                self.weekday_import_counts[h] += 1
                self.weekday_export_sums[h] += exp
                self.weekday_export_counts[h] += 1
            else:
                self.weekend_import_sums[h] += imp
                self.weekend_import_counts[h] += 1
                self.weekend_export_sums[h] += exp
                self.weekend_export_counts[h] += 1

        self.history_days_count = len(observed_days)

        # Compute average hourly profiles
        self.weekday_import_profile = [
            (self.weekday_import_sums[h] / Decimal(self.weekday_import_counts[h]))
            if self.weekday_import_counts[h] > 0
            else Decimal("0.0")
            for h in range(24)
        ]
        self.weekday_export_profile = [
            (self.weekday_export_sums[h] / Decimal(self.weekday_export_counts[h]))
            if self.weekday_export_counts[h] > 0
            else Decimal("0.0")
            for h in range(24)
        ]

        self.weekend_import_profile = [
            (self.weekend_import_sums[h] / Decimal(self.weekend_import_counts[h]))
            if self.weekend_import_counts[h] > 0
            else Decimal("0.0")
            for h in range(24)
        ]
        self.weekend_export_profile = [
            (self.weekend_export_sums[h] / Decimal(self.weekend_export_counts[h]))
            if self.weekend_export_counts[h] > 0
            else Decimal("0.0")
            for h in range(24)
        ]

    def forecast_month(
        self,
        current_date: date,
        mtd_readings: Sequence[IntervalReading],
        tariff_options: dict | None = None,
        rce_price: float | None = None,
        warehouse_kwh: float = 0.0,
        is_old_system: bool = False,
    ) -> HourlyProfileResult:
        """Generate month-end forecast by projecting remaining hours based on profiles."""
        import calendar

        year = current_date.year
        month = current_date.month
        days_in_month = calendar.monthrange(year, month)[1]

        # 1. Sum up actual MTD readings from the start of the month
        mtd_import_t1 = Decimal("0.0")
        mtd_import_t2 = Decimal("0.0")
        mtd_export_t1 = Decimal("0.0")
        mtd_export_t2 = Decimal("0.0")

        latest_dt_local: datetime | None = None

        for r in mtd_readings:
            local_dt = r.interval_start_utc.astimezone(self.tz)
            if local_dt.year == year and local_dt.month == month:
                zone = determine_tariff_zone(self.tariff_code, local_dt)
                imp = max(Decimal("0.0"), r.import_kwh)
                exp = max(Decimal("0.0"), r.export_kwh)

                if zone == 1:
                    mtd_import_t1 += imp
                    mtd_export_t1 += exp
                else:
                    mtd_import_t2 += imp
                    mtd_export_t2 += exp

                if latest_dt_local is None or local_dt > latest_dt_local:
                    latest_dt_local = local_dt

        mtd_import_total = mtd_import_t1 + mtd_import_t2
        mtd_export_total = mtd_export_t1 + mtd_export_t2

        # 2. Insufficient history fallback check (< 7 days of history)
        if self.history_days_count < 7:
            # Fallback to linear MTD
            elapsed_days = max(1, min(current_date.day, days_in_month))
            factor = Decimal(days_in_month) / Decimal(elapsed_days)
            f_imp_t1 = round(mtd_import_t1 * factor, 3)
            f_imp_t2 = round(mtd_import_t2 * factor, 3)
            f_exp_t1 = round(mtd_export_t1 * factor, 3)
            f_exp_t2 = round(mtd_export_t2 * factor, 3)

            return HourlyProfileResult(
                forecast_import_total_kwh=f_imp_t1 + f_imp_t2,
                forecast_import_t1_kwh=f_imp_t1,
                forecast_import_t2_kwh=f_imp_t2,
                forecast_export_total_kwh=f_exp_t1 + f_exp_t2,
                forecast_export_t1_kwh=f_exp_t1,
                forecast_export_t2_kwh=f_exp_t2,
                method="linear_fallback_insufficient_history",
                confidence_score=0.4,
                history_days_count=self.history_days_count,
                trend_factor=1.0,
            )

        # 3. Compute Adaptive Trend Scaling Factor (k)
        # Compare actual MTD consumption against what the profile would have predicted for elapsed days
        elapsed_days = current_date.day
        expected_mtd_import = Decimal("0.0")
        for d_num in range(1, elapsed_days + 1):
            d_iter = date(year, month, d_num)
            day_t = get_day_type(d_iter)
            prof = (
                self.weekday_import_profile
                if day_t == DayType.WEEKDAY
                else self.weekend_import_profile
            )
            expected_mtd_import += sum(prof)

        if expected_mtd_import > Decimal("0.0") and mtd_import_total > Decimal("0.0"):
            trend_factor = float(mtd_import_total / expected_mtd_import)
            # Defensive clamp between 0.5 and 2.0 to prevent runaway extrapolation
            trend_factor = max(0.5, min(2.0, trend_factor))
        else:
            trend_factor = 1.0

        k = Decimal(str(round(trend_factor, 4)))

        # 4. Project remaining hours of the month
        # Start immediately after the latest local reading, or at the start of tomorrow if none
        if latest_dt_local:
            next_hour_start = latest_dt_local.replace(
                minute=0, second=0, microsecond=0
            ) + timedelta(hours=1)
        else:
            next_hour_start = datetime(
                year, month, current_date.day, 0, 0, tzinfo=self.tz
            ) + timedelta(days=1)

        month_end_dt = datetime(
            year, month, days_in_month, 23, 59, 59, tzinfo=self.tz
        )

        future_import_t1 = Decimal("0.0")
        future_import_t2 = Decimal("0.0")
        future_export_t1 = Decimal("0.0")
        future_export_t2 = Decimal("0.0")

        curr = next_hour_start
        while curr <= month_end_dt:
            day_t = get_day_type(curr.date())
            h = curr.hour
            zone = determine_tariff_zone(self.tariff_code, curr)

            if day_t == DayType.WEEKDAY:
                hour_imp = self.weekday_import_profile[h] * k
                hour_exp = self.weekday_export_profile[h] * k
            else:
                hour_imp = self.weekend_import_profile[h] * k
                hour_exp = self.weekend_export_profile[h] * k

            if zone == 1:
                future_import_t1 += hour_imp
                future_export_t1 += hour_exp
            else:
                future_import_t2 += hour_imp
                future_export_t2 += hour_exp

            curr += timedelta(hours=1)

        # 5. Combine MTD Actuals + Future Projections
        f_imp_t1 = round(mtd_import_t1 + future_import_t1, 3)
        f_imp_t2 = round(mtd_import_t2 + future_import_t2, 3)
        f_imp_total = f_imp_t1 + f_imp_t2

        f_exp_t1 = round(mtd_export_t1 + future_export_t1, 3)
        f_exp_t2 = round(mtd_export_t2 + future_export_t2, 3)
        f_exp_total = f_exp_t1 + f_exp_t2

        # 6. Optional Invoice Cost Calculation
        payable_pln: Decimal | None = None
        bill_dict: dict | None = None

        if tariff_options:
            fees = fees_from_options(tariff_options, self.tariff_code)
            rce = float(rce_price or 0.25)
            try:
                bill_res = compute_bill(
                    import_day=float(f_imp_t1),
                    import_night=float(f_imp_t2),
                    export_total=float(f_exp_total),
                    rcem=rce,
                    fees=fees,
                    cover_day=warehouse_kwh if is_old_system else 0.0,
                    cover_night=0.0,
                    deposit_pln=0.0 if is_old_system else None,
                )
                payable_pln = Decimal(str(bill_res["do_zaplaty"]))
                bill_dict = bill_res
            except Exception as err:
                _LOGGER.debug("Invoice bill compute failed in forecaster: %s", err)

        confidence = min(0.95, 0.5 + (self.history_days_count / 60.0) * 0.45)

        return HourlyProfileResult(
            forecast_import_total_kwh=f_imp_total,
            forecast_import_t1_kwh=f_imp_t1,
            forecast_import_t2_kwh=f_imp_t2,
            forecast_export_total_kwh=f_exp_total,
            forecast_export_t1_kwh=f_exp_t1,
            forecast_export_t2_kwh=f_exp_t2,
            method="hourly_profile_wal",
            confidence_score=round(confidence, 2),
            history_days_count=self.history_days_count,
            trend_factor=round(trend_factor, 3),
            forecast_payable_pln=payable_pln,
            bill_breakdown=bill_dict,
        )
