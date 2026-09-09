"""Unit tests for PV Autoconsumption & Microgrid Household Engine."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pytest

from custom_components.energa_mobile.autoconsumption import (
    AutoconsumptionSummary,
    HourlyAutoconsumptionBucket,
    compute_autoconsumption_summary,
    get_variable_unit_price_brutto,
)
from custom_components.energa_mobile.core.readings.models import IntervalReading


def test_variable_unit_price_brutto():
    """Verify variable price calculation (energy + grid + quality + oze + cogen) * 1.23."""
    # Weekday 10:00 -> Zone 1 (Day/Peak) for G12w
    dt_peak = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)
    price_peak = get_variable_unit_price_brutto("G12W", dt_peak)
    # (0.6107 + 0.4017 + 0.0332 + 0.0073 + 0.0030) * 1.23 = 1.0559 * 1.23 = 1.2988
    assert round(price_peak, 2) == 1.30

    # Weekday 23:00 -> Zone 2 (Night/Off-peak) for G12w
    dt_offpeak = datetime(2026, 9, 9, 23, 0, tzinfo=timezone.utc)
    price_offpeak = get_variable_unit_price_brutto("G12W", dt_offpeak)
    # (0.3990 + 0.0851 + 0.0332 + 0.0073 + 0.0030) * 1.23 = 0.5276 * 1.23 = 0.6489
    assert round(price_offpeak, 2) == 0.65


def test_hourly_autoconsumption_bucket_alignment():
    """Test exact hour-by-hour alignment of PV generation and Energa export/import."""
    now_ref = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)

    # 3 hours on today (2026-09-09)
    h10 = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)
    h11 = datetime(2026, 9, 9, 11, 0, tzinfo=timezone.utc)
    h12 = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    pv_hourly = {
        h10: 3.5,  # 3.5 kWh produced
        h11: 4.2,  # 4.2 kWh produced
        h12: 5.0,  # 5.0 kWh produced
    }

    readings = [
        IntervalReading(
            ppe_id="PPE1",
            meter_id="M1",
            register="combined",
            interval_start_utc=h10,
            resolution="1h",
            import_kwh=Decimal("0.1"),
            export_kwh=Decimal("2.0"),
        ),
        IntervalReading(
            ppe_id="PPE1",
            meter_id="M1",
            register="combined",
            interval_start_utc=h11,
            resolution="1h",
            import_kwh=Decimal("0.0"),
            export_kwh=Decimal("3.0"),
        ),
        IntervalReading(
            ppe_id="PPE1",
            meter_id="M1",
            register="combined",
            interval_start_utc=h12,
            resolution="1h",
            import_kwh=Decimal("0.5"),
            export_kwh=Decimal("1.0"),
        ),
    ]

    summary = compute_autoconsumption_summary(
        pv_hourly_map=pv_hourly,
        energa_readings=readings,
        tariff="G12W",
        now_dt=now_ref,
    )

    # h10: auto = 3.5 - 2.0 = 1.5 kWh, home = 0.1 + 1.5 = 1.6 kWh
    # h11: auto = 4.2 - 3.0 = 1.2 kWh, home = 0.0 + 1.2 = 1.2 kWh
    # h12: auto = 5.0 - 1.0 = 4.0 kWh, home = 0.5 + 4.0 = 4.5 kWh
    # Total auto today = 1.5 + 1.2 + 4.0 = 6.7 kWh
    # Total home today = 1.6 + 1.2 + 4.5 = 7.3 kWh
    # Total PV = 3.5 + 4.2 + 5.0 = 12.7 kWh

    assert summary.today_kwh == 6.7
    assert summary.mtd_kwh == 6.7
    assert summary.today_home_consumption_kwh == 7.3
    assert summary.mtd_home_consumption_kwh == 7.3
    assert summary.mtd_pv_kwh == 12.7

    # Ratio check
    # 6.7 / 12.7 * 100 = 52.8%
    assert summary.autoconsumption_ratio_mtd == 52.8
    # 6.7 / 7.3 * 100 = 91.8%
    assert summary.self_sufficiency_ratio_mtd == 91.8
    assert summary.synced_hours_count == 3
    assert summary.synced_until is not None


def test_autoconsumption_handles_delayed_energa_telemetry():
    """Verify that future PV hours without Energa export are excluded from calculation.

    This strictly eliminates the false '100% autoconsumption' spike during daytime.
    """
    now_ref = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)

    # Inverter has data all the way up to 17:00
    pv_hourly = {
        datetime(2026, 9, 9, h, 0, tzinfo=timezone.utc): 2.0 for h in range(8, 18)
    }

    # But Energa OSD only has data up to 13:00 (5 hours behind)
    readings = [
        IntervalReading(
            ppe_id="PPE1",
            meter_id="M1",
            register="combined",
            interval_start_utc=datetime(2026, 9, 9, h, 0, tzinfo=timezone.utc),
            resolution="1h",
            import_kwh=Decimal("0.2"),
            export_kwh=Decimal("1.5"),
        )
        for h in range(8, 14)
    ]

    summary = compute_autoconsumption_summary(
        pv_hourly_map=pv_hourly,
        energa_readings=readings,
        tariff="G12W",
        now_dt=now_ref,
    )

    # Only 6 hours (8..13) are common and evaluated
    assert summary.synced_hours_count == 6
    # Each hour: 2.0 PV - 1.5 Export = 0.5 kWh autoconsumption
    assert summary.today_kwh == round(6 * 0.5, 3)
    # Total PV across synced hours is 6 * 2.0 = 12.0 kWh
    assert summary.mtd_pv_kwh == 12.0
    # Autoconsumption ratio = 3.0 / 12.0 = 25.0%
    assert summary.autoconsumption_ratio_mtd == 25.0


def test_autoconsumption_edge_case_export_greater_than_pv():
    """Verify clamp to 0.0 if export slightly exceeds inverter reading due to clock drift."""
    h = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    pv_hourly = {h: 2.0}
    readings = [
        IntervalReading(
            ppe_id="PPE1",
            meter_id="M1",
            register="combined",
            interval_start_utc=h,
            resolution="1h",
            import_kwh=Decimal("0.0"),
            export_kwh=Decimal("2.2"),
        )
    ]

    summary = compute_autoconsumption_summary(
        pv_hourly_map=pv_hourly,
        energa_readings=readings,
        tariff="G12W",
        now_dt=h + timedelta(hours=1),
    )

    assert summary.today_kwh == 0.0
    assert summary.autoconsumption_ratio_mtd == 0.0


def test_autoconsumption_sensor_entity():
    """Verify EnergaAutoconsumptionSensor state and attributes mapping."""
    from unittest.mock import MagicMock
    from custom_components.energa_mobile.sensor import EnergaAutoconsumptionSensor
    from custom_components.energa_mobile.const import CONF_INVERTER_ENERGY_ENTITY

    coordinator = MagicMock()
    entry = MagicMock()
    entry.options = {CONF_INVERTER_ENERGY_ENTITY: "sensor.solis_energy_total"}

    summary = AutoconsumptionSummary(
        today_kwh=10.5,
        mtd_kwh=150.0,
        autoconsumption_ratio_mtd=75.5,
        self_sufficiency_ratio_mtd=88.2,
        savings_mtd_pln=195.0,
        today_home_consumption_kwh=12.0,
        synced_hours_count=24,
    )
    coordinator._autoconsumption_summary = {"METER123": summary}

    device_info = MagicMock()
    sensor = EnergaAutoconsumptionSensor(
        coordinator=coordinator,
        meter_id="METER123",
        device_info=device_info,
        entry=entry,
        metric_key="today_kwh",
        name="Autokonsumpcja Dziś",
        icon="mdi:solar-power-variant",
    )
    assert sensor.native_value == 10.5
    attrs = sensor.extra_state_attributes
    assert attrs["today_autoconsumption_kwh"] == 10.5
    assert attrs["synced_hours_count"] == 24
    assert attrs["inverter_entity"] == "sensor.solis_energy_total"

