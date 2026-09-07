"""Tests for HourlyProfileForecaster, Polish holiday calendar, and multi-tariff zone mapping (Etap 5)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from custom_components.energa_mobile.core.readings.models import IntervalReading
from custom_components.energa_mobile.projections.forecast import (
    DayType,
    HourlyProfileForecaster,
    compute_easter,
    determine_tariff_zone,
    get_day_type,
    is_polish_holiday,
)


def test_easter_computation():
    """Verify Meeus Easter algorithm for various years."""
    assert compute_easter(2024) == date(2024, 3, 31)
    assert compute_easter(2025) == date(2025, 4, 20)
    assert compute_easter(2026) == date(2026, 4, 5)
    assert compute_easter(2027) == date(2027, 3, 28)


def test_polish_holidays():
    """Verify Polish statutory holidays (fixed and moveable)."""
    # Fixed
    assert is_polish_holiday(date(2026, 1, 1))   # Nowy Rok
    assert is_polish_holiday(date(2026, 1, 6))   # Trzech Króli
    assert is_polish_holiday(date(2026, 5, 1))   # Święto Pracy
    assert is_polish_holiday(date(2026, 5, 3))   # 3 Maja
    assert is_polish_holiday(date(2026, 8, 15))  # Wniebowzięcie NMP
    assert is_polish_holiday(date(2026, 11, 1))  # Wszystkich Świętych
    assert is_polish_holiday(date(2026, 11, 11)) # Święto Niepodległości
    assert is_polish_holiday(date(2026, 12, 25)) # Boże Narodzenie 1
    assert is_polish_holiday(date(2026, 12, 26)) # Boże Narodzenie 2

    # Moveable for 2026 (Easter = 5 April 2026)
    assert is_polish_holiday(date(2026, 4, 5))   # Wielkanoc
    assert is_polish_holiday(date(2026, 4, 6))   # Poniedziałek Wielkanocny
    assert is_polish_holiday(date(2026, 6, 4))   # Boże Ciało (Easter + 60d)

    # Regular days
    assert not is_polish_holiday(date(2026, 5, 2))
    assert not is_polish_holiday(date(2026, 9, 1))


def test_day_type_classification():
    """Verify weekday vs weekend classification including holidays."""
    # Regular Monday
    assert get_day_type(date(2026, 9, 7)) == DayType.WEEKDAY
    # Saturday
    assert get_day_type(date(2026, 9, 12)) == DayType.WEEKEND
    # Sunday
    assert get_day_type(date(2026, 9, 13)) == DayType.WEEKEND
    # Thursday but holiday (Boże Ciało 4 June 2026)
    assert get_day_type(date(2026, 6, 4)) == DayType.WEEKEND


def test_tariff_zone_mapping():
    """Verify zone mapping for G11, G12, and G12w."""
    tz_pl = timezone(timedelta(hours=2))

    # G11: always zone 1
    dt_g11 = datetime(2026, 9, 7, 12, 0, tzinfo=tz_pl)
    assert determine_tariff_zone("G11", dt_g11) == 1
    assert determine_tariff_zone("G11", datetime(2026, 9, 12, 12, 0, tzinfo=tz_pl)) == 1

    # G12w: Weekday peak (6-13, 15-22) is zone 1; 13-15 and night is zone 2
    monday_10am = datetime(2026, 9, 7, 10, 0, tzinfo=tz_pl)
    monday_14pm = datetime(2026, 9, 7, 14, 0, tzinfo=tz_pl)
    monday_18pm = datetime(2026, 9, 7, 18, 0, tzinfo=tz_pl)
    monday_23pm = datetime(2026, 9, 7, 23, 0, tzinfo=tz_pl)

    assert determine_tariff_zone("G12w", monday_10am) == 1
    assert determine_tariff_zone("G12w", monday_14pm) == 2
    assert determine_tariff_zone("G12w", monday_18pm) == 1
    assert determine_tariff_zone("G12w", monday_23pm) == 2

    # G12w on Weekend or Holiday: 100% zone 2!
    saturday_10am = datetime(2026, 9, 12, 10, 0, tzinfo=tz_pl)
    holiday_10am = datetime(2026, 6, 4, 10, 0, tzinfo=tz_pl)  # Boże Ciało
    assert determine_tariff_zone("G12w", saturday_10am) == 2
    assert determine_tariff_zone("G12w", holiday_10am) == 2


def test_hourly_profile_forecaster_insufficient_history():
    """Verify that fewer than 7 days of history safely falls back to linear MTD."""
    # Only 3 days of readings
    readings = []
    base = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    for i in range(72):
        readings.append(
            IntervalReading(
                ppe_id="TEST_PPE",
                meter_id="TEST_METER",
                register="total",
                interval_start_utc=base + timedelta(hours=i),
                resolution="1h",
                import_kwh=Decimal("1.0"),
                export_kwh=Decimal("0.5"),
            )
        )

    forecaster = HourlyProfileForecaster(readings=readings, tariff_code="G12w")
    assert forecaster.history_days_count < 7

    res = forecaster.forecast_month(
        current_date=date(2026, 9, 3),
        mtd_readings=readings,
    )
    assert res.method == "linear_fallback_insufficient_history"
    assert res.confidence_score == 0.4
    # 3 days elapsed in 30-day month -> factor = 10
    # MTD = 72 kWh import -> forecast = 720 kWh
    assert res.forecast_import_total_kwh == Decimal("720.000")


def test_hourly_profile_forecaster_wal_projection():
    """Verify full 24h profile decomposition and projection with >=14 days of history."""
    readings = []
    start_dt = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    # Generate 45 days of synthetic readings with distinct weekday vs weekend patterns
    # Weekday: 2.0 kWh in peak hours, 0.5 kWh in night
    # Weekend: 1.0 kWh flat
    for day in range(45):
        d_curr = (start_dt + timedelta(days=day)).date()
        is_wknd = d_curr.weekday() in (5, 6)
        for hour in range(24):
            dt = datetime(d_curr.year, d_curr.month, d_curr.day, hour, 0, tzinfo=timezone.utc)
            if is_wknd:
                imp = Decimal("1.0")
            else:
                imp = Decimal("2.0") if 6 <= hour < 22 else Decimal("0.5")

            readings.append(
                IntervalReading(
                    ppe_id="TEST_PPE",
                    meter_id="TEST_METER",
                    register="total",
                    interval_start_utc=dt,
                    resolution="1h",
                    import_kwh=imp,
                    export_kwh=Decimal("0.2"),
                )
            )

    forecaster = HourlyProfileForecaster(readings=readings, tariff_code="G12w", tz_offset_hours=2)
    assert forecaster.history_days_count >= 30

    # Test month forecast for September 10th
    mtd_readings = [
        r for r in readings
        if r.interval_start_utc >= datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        and r.interval_start_utc < datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
    ]

    res = forecaster.forecast_month(
        current_date=date(2026, 9, 10),
        mtd_readings=mtd_readings,
    )

    assert res.method == "hourly_profile_wal"
    assert res.confidence_score >= 0.70
    assert res.forecast_import_total_kwh > Decimal("0")
    # T1 + T2 must strictly equal total import
    assert res.forecast_import_t1_kwh + res.forecast_import_t2_kwh == res.forecast_import_total_kwh
    assert res.forecast_export_t1_kwh + res.forecast_export_t2_kwh == res.forecast_export_total_kwh
    # G12w separates zones: both T1 and T2 must be positive
    assert res.forecast_import_t1_kwh > Decimal("0")
    assert res.forecast_import_t2_kwh > Decimal("0")
