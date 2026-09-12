"""Tests for EnergaBillForecastSensor WAL integration and new Arbitrage Binary Sensors (Etap 5)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from custom_components.energa_mobile.adapters.pse.models import MarketPriceRecord
from custom_components.energa_mobile.binary_sensor import (
    EnergaBessChargeWindowBinarySensor,
    EnergaBessDischargeWindowBinarySensor,
    EnergaRceNegativePriceBinarySensor,
)
from custom_components.energa_mobile.core.readings.models import IntervalReading
from custom_components.energa_mobile.projections.arbitrage import (
    ArbitrageEngine,
)
from custom_components.energa_mobile.sensor import (
    EnergaBillComponentSensor,
    EnergaBillForecastSensor,
    PseRceArbitrageSpreadSensor,
    PseRceDynamicPriceSensor,
)


@pytest.fixture
def mock_entry():
    entry = MagicMock()
    entry.entry_id = "test_entry_etap5"
    entry.options = {
        "tariff_code": "G12w",
        "tariff_energy_day": 0.6107,
        "tariff_energy_night": 0.3990,
    }
    entry.data = {}
    return entry


@pytest.fixture
def mock_device_info():
    dev = MagicMock()
    dev.identifiers = {("energa_mobile", "1340026")}
    dev.name = "Energa 1340026"
    return dev


def test_forecast_sensor_uses_wal_profile(mock_entry, mock_device_info):
    """Verify that EnergaBillForecastSensor switches to hourly_profile_wal when storage has >=7d history."""
    coord = MagicMock()
    coord.data = [{"meter_point_id": 1340026, "meter_serial": "00069839", "tariff": "G12w"}]
    coord._mtd = {
        "1340026": {
            "import": 50.0,
            "export": 20.0,
            "import_1": 30.0,
            "import_2": 20.0,
            "export_1": 15.0,
            "export_2": 5.0,
        }
    }
    coord._rce_cache = 0.25

    # Mock storage returning 14 days of hourly canonical readings
    storage = MagicMock()
    readings = []
    base_dt = datetime.now(timezone.utc) - timedelta(days=14)
    for i in range(14 * 24):
        readings.append(
            IntervalReading(
                ppe_id="1340026",
                meter_id="1340026",
                register="total",
                interval_start_utc=base_dt + timedelta(hours=i),
                resolution="1h",
                import_kwh=Decimal("1.5"),
                export_kwh=Decimal("0.5"),
            )
        )
    storage.get_readings.return_value = readings
    coord.storage = storage

    sensor = EnergaBillForecastSensor(
        coordinator=coord,
        meter_id="1340026",
        device_info=mock_device_info,
        entry=mock_entry,
        has_zones=True,
        serial="00069839",
    )

    val = sensor.native_value
    assert val is not None
    attrs = sensor.extra_state_attributes
    assert "hourly_profile_wal" in attrs["forecast_method"]
    assert attrs["profile_history_days"] >= 14
    assert attrs["profile_confidence"] > 0.5
    assert attrs["forecast_import_t1_kwh"] > 0
    assert attrs["forecast_import_t2_kwh"] > 0


def test_arbitrage_binary_sensors(mock_entry):
    """Verify state and attributes of BESS charge/discharge and negative price binary sensors."""
    coord = MagicMock()
    now_utc = datetime.now(timezone.utc)

    # Build 24 hours of prices centered around now_utc
    records = []
    start_hour = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=10)
    for h in range(24):
        st = start_hour + timedelta(hours=h)
        en = st + timedelta(hours=1)
        # Make the current hour the peak discharge hour (1.50 PLN/kWh)
        # Make 4 hours ago the cheap charge hour (0.10 PLN/kWh)
        if st <= now_utc < en:
            price_mwh = Decimal("1500.0")
        elif h == 6:
            price_mwh = Decimal("100.0")
        elif h == 7:
            price_mwh = Decimal("-50.0")  # Negative price interval
        else:
            price_mwh = Decimal("500.0")

        records.append(
            MarketPriceRecord(
                price_type="RCE",
                applicable_year=st.year,
                applicable_month=st.month,
                publication_date=st.date(),
                price_mwh=price_mwh,
                price_kwh=round(price_mwh / Decimal("1000"), 5),
                interval_start_utc=st,
                interval_end_utc=en,
                resolution="1H",
                business_date=now_utc.date(),
            )
        )

    engine = ArbitrageEngine(
        battery_efficiency=Decimal("0.88"),
        charge_hours=2,
        discharge_hours=2,
        min_spread_pln=Decimal("0.05"),
    )
    plan = engine.plan_day(records, target_date=now_utc.date())

    coord._arbitrage_plan = plan
    coord._rce_current_record = next(
        r for r in records if r.interval_start_utc <= now_utc < r.interval_end_utc
    )

    # 1. Peak discharge sensor should be ON right now
    discharge_sensor = EnergaBessDischargeWindowBinarySensor(
        coordinator=coord,
        entry=mock_entry,
        meter_point_id="1340026",
        meter_serial="00069839",
    )
    assert discharge_sensor.is_on is True
    assert discharge_sensor.extra_state_attributes["is_spread_profitable"] is True
    assert len(discharge_sensor.extra_state_attributes["windows"]) > 0

    # 2. Charge sensor should be OFF right now
    charge_sensor = EnergaBessChargeWindowBinarySensor(
        coordinator=coord,
        entry=mock_entry,
        meter_point_id="1340026",
        meter_serial="00069839",
    )
    assert charge_sensor.is_on is False

    # 3. Negative price sensor should report negative intervals count > 0
    neg_sensor = EnergaRceNegativePriceBinarySensor(
        coordinator=coord,
        entry=mock_entry,
        meter_point_id="1340026",
        meter_serial="00069839",
    )
    # Right now price is 1.50, so sensor is OFF, but extra_state_attributes detects negative interval in day
    assert neg_sensor.is_on is False
    assert neg_sensor.extra_state_attributes["negative_intervals_count"] == 1


def test_dynamic_rce_and_spread_sensors(mock_entry):
    """Verify telemetry sensors: current dynamic RCE price and BESS arbitrage spread."""
    coord = MagicMock()
    now_utc = datetime.now(timezone.utc)
    curr_rec = MarketPriceRecord(
        price_type="RCE",
        applicable_year=now_utc.year,
        applicable_month=now_utc.month,
        publication_date=now_utc.date(),
        price_mwh=Decimal("450.0"),
        price_kwh=Decimal("0.450"),
        interval_start_utc=now_utc - timedelta(minutes=10),
        interval_end_utc=now_utc + timedelta(minutes=5),
        resolution="15M",
    )
    coord._rce_current_record = curr_rec

    price_sensor = PseRceDynamicPriceSensor(
        coordinator=coord,
        entry=mock_entry,
        meter_point_id="1340026",
        meter_serial="00069839",
    )
    # Native value is brutto (with VAT 23% for prosumer deposit valuation)
    assert price_sensor.native_value == 0.5535
    assert price_sensor.extra_state_attributes["price_netto_pln_kwh"] == 0.450
    assert price_sensor.extra_state_attributes["price_brutto_pln_kwh"] == 0.5535
    assert price_sensor.extra_state_attributes["resolution"] == "15M"

    # Arbitrage spread sensor
    plan_mock = MagicMock()
    plan_mock.effective_spread_kwh = Decimal("0.352")
    plan_mock.is_spread_profitable = True
    plan_mock.avg_charge_price_kwh = Decimal("0.200")
    plan_mock.avg_discharge_price_kwh = Decimal("0.650")
    plan_mock.battery_efficiency = Decimal("0.88")
    plan_mock.target_date = date(2026, 9, 7)
    coord._arbitrage_plan = plan_mock

    spread_sensor = PseRceArbitrageSpreadSensor(
        coordinator=coord,
        entry=mock_entry,
        meter_point_id="1340026",
        meter_serial="00069839",
    )
    # Native value is brutto (with VAT 23%)
    assert spread_sensor.native_value == round(0.352 * 1.23, 5)
    assert spread_sensor.extra_state_attributes["effective_spread_netto_pln_kwh"] == 0.352
    assert spread_sensor.extra_state_attributes["is_spread_profitable"] is True


def test_bill_component_sensors_return_brutto_and_sum_to_total(mock_entry, mock_device_info):
    """Verify that MTD component sensors return BRUTTO amounts that sum to total gross."""
    from unittest.mock import patch

    coord = MagicMock()
    coord.data = [{"meter_point_id": 1340026, "meter_serial": "00069839"}]
    coord._mtd = {"1340026": {"import_1": 100.0, "import_2": 50.0}}

    sensor_sale = EnergaBillComponentSensor(
        coordinator=coord,
        meter_id="1340026",
        device_info=mock_device_info,
        entry=mock_entry,
        component_key="sale_total",
        name="Koszt Energii Czynnej MTD",
        icon="mdi:flash-outline",
        serial="00069839",
    )
    sensor_distr = EnergaBillComponentSensor(
        coordinator=coord,
        meter_id="1340026",
        device_info=mock_device_info,
        entry=mock_entry,
        component_key="distr_total",
        name="Koszt Dystrybucji MTD",
        icon="mdi:transmission-tower",
        serial="00069839",
    )
    sensor_brutto = EnergaBillComponentSensor(
        coordinator=coord,
        meter_id="1340026",
        device_info=mock_device_info,
        entry=mock_entry,
        component_key="brutto",
        name="Koszt Brutto MTD",
        icon="mdi:receipt-text-outline",
        serial="00069839",
    )

    mock_bill = {
        "sale_total": 100.00,       # 100 zł netto
        "sale_gross": 123.00,       # 123 zł brutto
        "distr_total": 50.00,       # 50 zł netto
        "distr_gross": 61.50,       # 61.50 zł brutto
        "netto": 150.00,
        "vat": 34.50,
        "brutto": 184.50,           # 123.00 + 61.50 = 184.50
        "deposit": 0.0,
        "deposit_applied": 0.0,
        "do_zaplaty": 184.50,
    }

    with patch.object(sensor_sale, "_calculate_bill_mtd", return_value=(mock_bill, {
        "mtd_sale_gross_pln": 123.00,
        "mtd_distr_gross_pln": 61.50,
        "mtd_sale_total_pln": 100.00,
        "mtd_distr_total_pln": 50.00,
        "mtd_brutto_pln": 184.50,
        "mtd_netto_pln": 150.00,
        "mtd_vat_pln": 34.50,
    })):
        assert sensor_sale.native_value == 123.00
        assert sensor_sale.extra_state_attributes["netto_pln"] == 100.00
        assert sensor_sale.extra_state_attributes["gross_pln"] == 123.00
        assert sensor_sale.extra_state_attributes["vat_rate"] == "23%"

    with patch.object(sensor_distr, "_calculate_bill_mtd", return_value=(mock_bill, {
        "mtd_sale_gross_pln": 123.00,
        "mtd_distr_gross_pln": 61.50,
        "mtd_sale_total_pln": 100.00,
        "mtd_distr_total_pln": 50.00,
        "mtd_brutto_pln": 184.50,
        "mtd_netto_pln": 150.00,
        "mtd_vat_pln": 34.50,
    })):
        assert sensor_distr.native_value == 61.50
        assert sensor_distr.extra_state_attributes["netto_pln"] == 50.00
        assert sensor_distr.extra_state_attributes["gross_pln"] == 61.50
        assert sensor_distr.extra_state_attributes["vat_rate"] == "23%"

    with patch.object(sensor_brutto, "_calculate_bill_mtd", return_value=(mock_bill, {
        "mtd_sale_gross_pln": 123.00,
        "mtd_distr_gross_pln": 61.50,
        "mtd_sale_total_pln": 100.00,
        "mtd_distr_total_pln": 50.00,
        "mtd_brutto_pln": 184.50,
        "mtd_netto_pln": 150.00,
        "mtd_vat_pln": 34.50,
    })):
        assert sensor_brutto.native_value == 184.50

    # Exact equality: Sale Gross + Distr Gross == Total Gross
    assert round(sensor_sale.native_value + sensor_distr.native_value, 2) == sensor_brutto.native_value

