"""Unit tests for spike prevention mechanisms."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from custom_components.energa_mobile.sensor import (
    EnergaStatisticsSensor,
    EnergaBillComponentSensor,
)
from custom_components.energa_mobile.data_updater import EnergaDataUpdater
from custom_components.energa_mobile.const import DOMAIN


def test_energa_statistics_sensor_native_value_is_none():
    """Verify EnergaStatisticsSensor returns None for native_value.

    This ensures HA recorder does not compute competing statistics from states table,
    which was the root cause of MWh spikes and sum wipes.
    """
    coordinator = MagicMock()
    coordinator.data = []
    coordinator.get_pre_fetched_stats.return_value = {
        "sensor.energa_12345_panel_energia_zuzycie": {"sum": 5000.0, "start": 1000}
    }
    device_info = MagicMock()
    entry = MagicMock()

    sensor = EnergaStatisticsSensor(
        coordinator=coordinator,
        meter_id="12345",
        data_key="import",
        name="Panel Energia Zużycie",
        device_info=device_info,
        entry=entry,
    )
    sensor.entity_id = "sensor.energa_12345_panel_energia_zuzycie"
    sensor._last_sum = 5000.0

    # native_value MUST be None
    assert sensor.native_value is None


def test_bill_component_sensor_state_class_is_none():
    """Verify EnergaBillComponentSensor does not set TOTAL or MEASUREMENT state class.

    MTD metrics are periodic breakdown indicators, not cumulative meters.
    Setting state_class=TOTAL causes negative deltas and spurious spikes in HA recorder.
    """
    coordinator = MagicMock()
    coordinator.data = []
    device_info = MagicMock()
    entry = MagicMock()
    entry.options = {}

    sensor = EnergaBillComponentSensor(
        coordinator=coordinator,
        meter_id="12345",
        device_info=device_info,
        entry=entry,
        component_key="energy_import",
        name="Pobór energii MTD",
        icon="mdi:transmission-tower-export",
        unit="kWh",
        device_class="energy",
    )

    assert sensor._attr_state_class is None


def test_data_updater_monotonic_guard():
    """Verify forward calculation enforces monotonic increase and alias matching."""
    hass = MagicMock()
    entry = MagicMock()
    entry.options = {}

    pre_fetched = {
        "sensor.energa_99999_panel_energia_zuzycie": {"sum": 1000.0, "start": 100}
    }

    updater = EnergaDataUpdater(hass, entry, pre_fetched_stats=pre_fetched)

    hourly_points = [
        {"dt": datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc), "value": 1.5},
        {"dt": datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc), "value": 2.0},
    ]

    stats = updater._forward_calculation(
        hourly_points,
        {"sum": 1000.0, "start": datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)},
        "sensor.energa_12345_panel_energia_zuzycie",
    )

    assert len(stats) == 2
    assert stats[0]["sum"] == 1001.5
    assert stats[1]["sum"] == 1003.5

    # Test monotonic guard clamping if a corrupt negative point is injected
    corrupted_points = [
        {"dt": datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc), "value": 0.5},
    ]
    clamped_stats = updater._forward_calculation(
        corrupted_points,
        {"sum": 1003.5, "start": datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)},
        "sensor.energa_12345_panel_energia_zuzycie",
    )
    assert clamped_stats[0]["sum"] >= 1003.5


def test_bill_current_sensor_mtd_export_volumes():
    """Verify EnergaBillCurrentSensor exposes MTD export volumes."""
    from custom_components.energa_mobile.sensor import EnergaBillCurrentSensor

    coordinator = MagicMock()
    coordinator.data = []
    coordinator._mtd = {"12345": {"export_1": 30.0, "export_2": 20.0}}
    device_info = MagicMock()
    entry = MagicMock()
    entry.options = {}

    sensor = EnergaBillCurrentSensor(
        coordinator=coordinator,
        meter_id="12345",
        device_info=device_info,
        entry=entry,
        has_zones=True,
    )

    with patch.object(sensor, "_mtd_parts", return_value=(100.0, 50.0)), \
         patch.object(sensor, "_mtd_zone_flows", return_value=(60.0, 40.0, 50.0)), \
         patch.object(sensor, "_rce", return_value=0.5), \
         patch.object(sensor, "_meter_tariff", return_value="G12w"), \
         patch("custom_components.energa_mobile.sensor.compute_bill", return_value={
             "sale_total": 50.0,
             "distr_total": 40.0,
             "netto": 90.0,
             "vat": 20.7,
             "brutto": 110.7,
             "deposit": 0.0,
             "deposit_applied": 0.0,
             "do_zaplaty": 110.7,
         }):
        val, attrs = sensor._calculate_bill_mtd()
        assert attrs["mtd_export_day_kwh"] == 30.0
        assert attrs["mtd_export_night_kwh"] == 20.0

