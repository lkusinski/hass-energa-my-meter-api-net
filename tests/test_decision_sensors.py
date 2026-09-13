"""Unit tests for decision and data quality sensors (binary_sensor.energa_tania_strefa, sensor.energa_jakosc_danych)."""

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from custom_components.energa_mobile.binary_sensor import (
    EnergaTaniaStrefaBinarySensor,
)
from custom_components.energa_mobile.sensors.live import (
    EnergaDataQualitySensor,
)

TIMEZONE = ZoneInfo("Europe/Warsaw")


def _create_mock_entry():
    entry = MagicMock()
    entry.options = {
        "import_price": 0.95,
        "import_price_1": 1.15,
        "import_price_2": 0.65,
    }
    return entry


# =========================================================================
# EnergaTaniaStrefaBinarySensor Tests
# =========================================================================

def test_tania_strefa_g12w_weekday_peak():
    """Verify G12w peak hours (e.g. Wednesday 10:00) return is_on=False and T1."""
    coordinator = MagicMock()
    entry = _create_mock_entry()
    sensor = EnergaTaniaStrefaBinarySensor(
        coordinator=coordinator,
        entry=entry,
        meter_point_id="12345",
        meter_serial="SERIAL123",
        tariff="G12W",
    )

    # Wednesday 2026-09-02 at 10:00 (peak hour)
    fake_now = datetime(2026, 9, 2, 10, 0, tzinfo=TIMEZONE)
    with patch("custom_components.energa_mobile.binary_sensor.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now

        assert sensor.is_on is False
        assert sensor.icon == "mdi:clock-alert-outline"
        attrs = sensor.extra_state_attributes
        assert attrs["tariff"] == "G12W"
        assert attrs["zone_id"] == "T1"
        assert attrs["zone_name"] == "Strefa szczytowa (T1 - standardowa)"
        assert attrs["active_price_pln_kwh"] == 1.15
        assert attrs["hours_until_next_zone"] == 3.0  # until 13:00
        assert attrs["is_weekend_or_holiday"] is False


def test_tania_strefa_g12w_weekday_offpeak():
    """Verify G12w off-peak afternoon (e.g. Wednesday 14:00) returns is_on=True and T2."""
    coordinator = MagicMock()
    entry = _create_mock_entry()
    sensor = EnergaTaniaStrefaBinarySensor(
        coordinator=coordinator,
        entry=entry,
        meter_point_id="12345",
        meter_serial="SERIAL123",
        tariff="G12W",
    )

    # Wednesday 2026-09-02 at 14:00 (afternoon off-peak 13-15)
    fake_now = datetime(2026, 9, 2, 14, 0, tzinfo=TIMEZONE)
    with patch("custom_components.energa_mobile.binary_sensor.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now

        assert sensor.is_on is True
        assert sensor.icon == "mdi:clock-check-outline"
        attrs = sensor.extra_state_attributes
        assert attrs["zone_id"] == "T2"
        assert attrs["zone_name"] == "Strefa pozaszczytowa (T2 - tania)"
        assert attrs["active_price_pln_kwh"] == 0.65
        assert attrs["hours_until_next_zone"] == 1.0  # until 15:00


def test_tania_strefa_g12w_weekend():
    """Verify G12w weekend returns 100% off-peak (is_on=True, zone_id=T2)."""
    coordinator = MagicMock()
    entry = _create_mock_entry()
    sensor = EnergaTaniaStrefaBinarySensor(
        coordinator=coordinator,
        entry=entry,
        meter_point_id="12345",
        meter_serial="SERIAL123",
        tariff="G12W",
    )

    # Sunday 2026-09-06 at 12:00
    fake_now = datetime(2026, 9, 6, 12, 0, tzinfo=TIMEZONE)
    with patch("custom_components.energa_mobile.binary_sensor.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now

        assert sensor.is_on is True
        attrs = sensor.extra_state_attributes
        assert attrs["zone_id"] == "T2"
        assert attrs["is_weekend_or_holiday"] is True
        # Sunday 12:00 until Monday 06:00 is 18.0 hours
        assert attrs["hours_until_next_zone"] == 18.0


def test_tania_strefa_g12w_polish_holiday():
    """Verify statutory Polish holiday (e.g. 11 Nov) in G12w is treated as off-peak."""
    coordinator = MagicMock()
    entry = _create_mock_entry()
    sensor = EnergaTaniaStrefaBinarySensor(
        coordinator=coordinator,
        entry=entry,
        meter_point_id="12345",
        meter_serial="SERIAL123",
        tariff="G12W",
    )

    # Wednesday 2026-11-11 at 10:00 (Święto Niepodległości)
    fake_now = datetime(2026, 11, 11, 10, 0, tzinfo=TIMEZONE)
    with patch("custom_components.energa_mobile.binary_sensor.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now

        assert sensor.is_on is True
        attrs = sensor.extra_state_attributes
        assert attrs["zone_id"] == "T2"
        assert attrs["is_weekend_or_holiday"] is True


def test_tania_strefa_g11_single_zone():
    """Verify single-zone G11 reports is_on=False with single-zone note."""
    coordinator = MagicMock()
    entry = _create_mock_entry()
    sensor = EnergaTaniaStrefaBinarySensor(
        coordinator=coordinator,
        entry=entry,
        meter_point_id="12345",
        meter_serial="SERIAL123",
        tariff="G11",
    )

    fake_now = datetime(2026, 9, 2, 14, 0, tzinfo=TIMEZONE)
    with patch("custom_components.energa_mobile.binary_sensor.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now

        assert sensor.is_on is False
        attrs = sensor.extra_state_attributes
        assert attrs["tariff"] == "G11"
        assert attrs["zone_id"] == "T1"
        assert "jednostrefowa" in attrs["zone_name"]
        assert attrs["next_zone_change"] is None
        assert attrs["hours_until_next_zone"] is None
        assert attrs["active_price_pln_kwh"] == 0.95


# =========================================================================
# EnergaDataQualitySensor Tests
# =========================================================================

def test_data_quality_sensor_ok():
    """Verify quality is 'OK' when last reading is yesterday (1 day lag)."""
    coordinator = MagicMock()
    coordinator.data = [{
        "meter_point_id": "12345",
        "last_measurement_date": "2026-09-12",
        "last_measurement_msg": "Odczyt danych został wykonany zdalnie.",
    }]
    device_info = MagicMock()
    sensor = EnergaDataQualitySensor(
        coordinator=coordinator,
        meter_id="12345",
        name="Jakość danych",
        icon="mdi:check-network-outline",
        device_info=device_info,
        ppe="590000000000000000",
        serial="SERIAL123",
        tariff="G12W",
    )

    fake_now = datetime(2026, 9, 13, 10, 0, tzinfo=TIMEZONE)
    with patch("custom_components.energa_mobile.sensors.live.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat

        assert sensor.native_value == "OK"
        assert sensor.icon == "mdi:check-network-outline"
        attrs = sensor.extra_state_attributes
        assert attrs["days_lag"] == 1
        assert attrs["last_reading_date"] == "2026-09-12"
        assert attrs["remote_reading_active"] is True
        assert "zdalnie" in attrs["remote_reading_message"]


def test_data_quality_sensor_delayed():
    """Verify quality is 'Opóźnione' when lag is between 3 and 5 days."""
    coordinator = MagicMock()
    coordinator.data = [{
        "meter_point_id": "12345",
        "last_measurement_date": "2026-09-09",
        "last_measurement_msg": "Odczyt danych został wykonany zdalnie.",
    }]
    device_info = MagicMock()
    sensor = EnergaDataQualitySensor(
        coordinator=coordinator,
        meter_id="12345",
        name="Jakość danych",
        icon="mdi:check-network-outline",
        device_info=device_info,
        ppe="590000000000000000",
        serial="SERIAL123",
    )

    fake_now = datetime(2026, 9, 13, 10, 0, tzinfo=TIMEZONE)
    with patch("custom_components.energa_mobile.sensors.live.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat

        assert sensor.native_value == "Opóźnione"
        assert sensor.icon == "mdi:clock-alert-outline"
        attrs = sensor.extra_state_attributes
        assert attrs["days_lag"] == 4


def test_data_quality_sensor_missing():
    """Verify quality is 'Braki' when lag > 5 days."""
    coordinator = MagicMock()
    coordinator.data = [{
        "meter_point_id": "12345",
        "last_measurement_date": "2026-09-01",
        "last_measurement_msg": "Odczyt danych został wykonany zdalnie.",
    }]
    device_info = MagicMock()
    sensor = EnergaDataQualitySensor(
        coordinator=coordinator,
        meter_id="12345",
        name="Jakość danych",
        icon="mdi:check-network-outline",
        device_info=device_info,
        ppe="590000000000000000",
        serial="SERIAL123",
    )

    fake_now = datetime(2026, 9, 13, 10, 0, tzinfo=TIMEZONE)
    with patch("custom_components.energa_mobile.sensors.live.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat

        assert sensor.native_value == "Braki"
        assert sensor.icon == "mdi:network-strength-off-outline"
        attrs = sensor.extra_state_attributes
        assert attrs["days_lag"] == 12


def test_data_quality_sensor_no_data():
    """Verify quality is 'Brak danych' when no reading date is known."""
    coordinator = MagicMock()
    coordinator.data = []
    device_info = MagicMock()
    sensor = EnergaDataQualitySensor(
        coordinator=coordinator,
        meter_id="12345",
        name="Jakość danych",
        icon="mdi:check-network-outline",
        device_info=device_info,
    )

    assert sensor.native_value == "Brak danych"
    assert sensor.icon == "mdi:network-strength-off-outline"
    attrs = sensor.extra_state_attributes
    assert attrs["last_reading_date"] is None
    assert attrs["days_lag"] is None
