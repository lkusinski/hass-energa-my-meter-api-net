"""Tests for PSE RCE parser and BESS Arbitrage Engine (Etap 5)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from custom_components.energa_mobile.adapters.pse.models import MarketPriceRecord
from custom_components.energa_mobile.adapters.pse.rce_client import parse_rce_api_payload
from custom_components.energa_mobile.projections.arbitrage import (
    ArbitrageAction,
    ArbitrageEngine,
)


def test_parse_rce_api_payload_15m():
    """Verify parsing official PSE OIRE API 15-minute interval response."""
    payload = {
        "value": [
            {
                "dtime": "2024-09-01 00:15:00",
                "period": "00:00 - 00:15",
                "rce_pln": 400.00,
                "dtime_utc": "2024-08-31 22:15:00",
                "period_utc": "22:00 - 22:15",
                "business_date": "2024-09-01",
                "publication_ts": "2024-08-31 14:01:16.056",
                "publication_ts_utc": "2024-08-31 12:01:16.056000",
            },
            {
                "dtime": "2024-09-01 00:30:00",
                "period": "00:15 - 00:30",
                "rce_pln": -26.50,  # Negative price
                "dtime_utc": "2024-08-31 22:30:00",
                "period_utc": "22:15 - 22:30",
                "business_date": "2024-09-01",
                "publication_ts": "2024-08-31 14:01:16.056",
                "publication_ts_utc": "2024-08-31 12:01:16.056000",
            },
        ]
    }

    records = parse_rce_api_payload(payload)
    assert len(records) == 2

    # Record 1: 400 PLN/MWh -> 0.40 PLN/kWh
    r1 = records[0]
    assert r1.price_type == "RCE"
    assert r1.price_mwh == Decimal("400.0")
    assert r1.price_kwh == Decimal("0.4")
    assert r1.resolution == "15M"
    assert r1.interval_start_utc == datetime(2024, 8, 31, 22, 0, tzinfo=timezone.utc)
    assert r1.interval_end_utc == datetime(2024, 8, 31, 22, 15, tzinfo=timezone.utc)

    # Record 2: -26.50 PLN/MWh -> -0.0265 PLN/kWh
    r2 = records[1]
    assert r2.price_mwh == Decimal("-26.5")
    assert r2.price_kwh == Decimal("-0.0265")
    assert r2.interval_start_utc == datetime(2024, 8, 31, 22, 15, tzinfo=timezone.utc)


def test_arbitrage_engine_plan_day():
    """Verify BESS optimal charging, discharging, and spread calculations."""
    base_dt = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    records = []

    # 24 hours of hourly prices:
    # 00:00-06:00: cheap (0.20 PLN/kWh)
    # 06:00-11:00: medium (0.50 PLN/kWh)
    # 11:00-13:00: negative prices (-0.05 PLN/kWh)
    # 13:00-18:00: medium (0.45 PLN/kWh)
    # 18:00-21:00: evening peak (1.20 PLN/kWh)
    # 21:00-24:00: night decrease (0.35 PLN/kWh)
    for h in range(24):
        st = base_dt + timedelta(hours=h)
        en = st + timedelta(hours=1)
        if 11 <= h < 13:
            price_mwh = Decimal("-50.0")
        elif 0 <= h < 6:
            price_mwh = Decimal("200.0")
        elif 18 <= h < 21:
            price_mwh = Decimal("1200.0")
        else:
            price_mwh = Decimal("500.0")

        records.append(
            MarketPriceRecord(
                price_type="RCE",
                applicable_year=2026,
                applicable_month=9,
                publication_date=date(2026, 8, 31),
                price_mwh=price_mwh,
                price_kwh=round(price_mwh / Decimal("1000"), 5),
                interval_start_utc=st,
                interval_end_utc=en,
                resolution="1H",
                business_date=date(2026, 9, 1),
            )
        )

    engine = ArbitrageEngine(
        battery_efficiency=Decimal("0.88"),
        charge_hours=3,
        discharge_hours=3,
        min_spread_pln=Decimal("0.10"),
    )

    plan = engine.plan_day(records, target_date=date(2026, 9, 1))

    # 1. Negative prices detected (hours 11 and 12)
    assert len(plan.negative_intervals) == 2
    assert plan.action_at(datetime(2026, 9, 1, 11, 30, tzinfo=timezone.utc)) == ArbitrageAction.NEGATIVE_ALERT

    # 2. Cheapest 3 hours should include 11:00, 12:00, and one night hour (00:00-05:00)
    # Average charge price should be very low (~0.033 PLN/kWh)
    assert plan.avg_charge_price_kwh < Decimal("0.10")

    # 3. Peak 3 hours should be 18:00 - 21:00 (1.20 PLN/kWh)
    assert plan.avg_discharge_price_kwh == Decimal("1.2")
    assert plan.action_at(datetime(2026, 9, 1, 19, 0, tzinfo=timezone.utc)) == ArbitrageAction.DISCHARGE

    # 4. Spread calculation: 1.20 * 0.88 - avg_charge > 0.90 PLN/kWh
    assert plan.effective_spread_kwh > Decimal("0.80")
    assert plan.is_spread_profitable is True


def test_arbitrage_engine_unprofitable_flat_prices():
    """Verify that flat prices result in unprofitable spread (no cycling recommended)."""
    base_dt = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    records = []
    # Flat price 0.50 PLN/kWh all day
    for h in range(24):
        st = base_dt + timedelta(hours=h)
        en = st + timedelta(hours=1)
        records.append(
            MarketPriceRecord(
                price_type="RCE",
                applicable_year=2026,
                applicable_month=9,
                publication_date=date(2026, 8, 31),
                price_mwh=Decimal("500.0"),
                price_kwh=Decimal("0.500"),
                interval_start_utc=st,
                interval_end_utc=en,
                resolution="1H",
                business_date=date(2026, 9, 1),
            )
        )

    engine = ArbitrageEngine(
        battery_efficiency=Decimal("0.88"),
        charge_hours=3,
        discharge_hours=3,
        min_spread_pln=Decimal("0.05"),
    )

    plan = engine.plan_day(records, target_date=date(2026, 9, 1))

    # Spread = 0.50 * 0.88 - 0.50 = -0.06 PLN/kWh (Loss due to 12% roundtrip dissipation)
    assert plan.is_spread_profitable is False
    assert plan.action_at(datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)) == ArbitrageAction.IDLE
    assert plan.action_at(datetime(2026, 9, 1, 19, 0, tzinfo=timezone.utc)) == ArbitrageAction.IDLE
