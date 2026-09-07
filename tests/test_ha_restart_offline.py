"""Tests for Home Assistant offline restart resilience.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 9 & 12.
Kryterium akceptacji:
"HA restart: Brak polaczenia po restarcie nie emituje zera i nie resetuje state/sum."
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from custom_components.energa_mobile.sensor import (
    EnergaStatisticsSensor,
    EnergaBankKwhSensor,
    EnergaBillCurrentSensor,
)
from custom_components.energa_mobile.ha.recorder_adapter import RecorderAdapter


def test_statistics_sensor_offline_restart_no_zero_emission():
    """Verify statistics sensor does not emit zero or reset sum when coordinator has no data."""
    coordinator_mock = MagicMock()
    coordinator_mock.data = None  # Offline / no connection
    coordinator_mock.get_hourly_stats.return_value = None
    coordinator_mock.get_pre_fetched_stats.return_value = {"sensor.test": {"sum": 150.0, "start": None}}

    entry_mock = MagicMock()
    entry_mock.entry_id = "test_entry"
    entry_mock.options = {}

    sensor = EnergaStatisticsSensor(
        coordinator=coordinator_mock,
        meter_id="12345",
        data_key="import",
        name="Pobór energii",
        device_info=MagicMock(),
        entry=entry_mock,
    )
    sensor.hass = MagicMock()
    sensor.entity_id = "sensor.energa_12345_import"
    sensor._last_sum = 150.0

    # Sensor should report unavailable when coordinator.data is None
    assert sensor.available is False

    # Trigger update while offline
    sensor._handle_coordinator_update()

    # Sum must NOT have been reset to 0
    assert sensor._last_sum == 150.0


def test_bank_kwh_sensor_offline_returns_none_not_zero():
    """Verify virtual bank sensor returns None (not 0.0) when offline."""
    coordinator_mock = MagicMock()
    coordinator_mock.data = None
    coordinator_mock._meter_totals = {}  # Empty offline totals

    entry_mock = MagicMock()
    entry_mock.options = {}

    bank_sensor = EnergaBankKwhSensor(
        coordinator=coordinator_mock,
        meter_id="12345",
        device_info=MagicMock(),
        entry=entry_mock,
        has_zones=False,
    )

    assert bank_sensor.native_value is None


def test_current_bill_sensor_offline_returns_none_not_zero():
    """Verify MTD bill sensor returns None when offline instead of displaying 0.00 zł."""
    coordinator_mock = MagicMock()
    coordinator_mock.data = None
    coordinator_mock._mtd = {}

    entry_mock = MagicMock()
    entry_mock.options = {}

    bill_sensor = EnergaBillCurrentSensor(
        coordinator=coordinator_mock,
        meter_id="12345",
        device_info=MagicMock(),
        entry=entry_mock,
        has_zones=False,
    )

    assert bill_sensor.native_value is None
