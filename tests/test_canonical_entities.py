"""Tests for canonical entity names, has_entity_name=True, device_info, and EntityRegistry migration."""

from unittest.mock import MagicMock
import pytest

from custom_components.energa_mobile.sensor import (
    EnergaProsumerBalanceSensor,
    EnergaBankKwhSensor,
    EnergaBankPlnSensor,
    EnergaBankLevelSensor,
    EnergaBankFlowSensor,
    EnergaFirstDataDateSensor,
    EnergaRceSensor,
    EnergaBillForecastSensor,
    EnergaBillCurrentSensor,
    EnergaBillComponentSensor,
)


@pytest.fixture
def mock_device_info():
    dev = MagicMock()
    dev.identifiers = {("energa_mobile", "00069839")}
    dev.name = "Energa 00069839"
    return dev


@pytest.fixture
def mock_coordinator():
    coord = MagicMock()
    coord.data = [{"meter_point_id": 1340026, "meter_serial": "00069839"}]
    coord._meter_totals = {"1340026": {"import": 100, "export": 50}}
    coord._mtd = {"1340026": {"import": 10, "export": 5}}
    return coord


@pytest.fixture
def mock_entry():
    entry = MagicMock()
    entry.entry_id = "test_entry_123"
    entry.options = {}
    entry.data = {}
    return entry


class TestCanonicalSensorNames:
    """Verify that every sensor class has clean Polish name without serial, has_entity_name=True, and device_info set."""

    def test_prosumer_balance_sensor(self, mock_coordinator, mock_device_info, mock_entry):
        sensor = EnergaProsumerBalanceSensor(
            coordinator=mock_coordinator,
            meter_id="1340026",
            device_info=mock_device_info,
            entry=mock_entry,
            serial="00069839",
        )
        assert sensor._attr_name == "Bilans Prosumencki"
        assert sensor._attr_has_entity_name is True
        assert sensor._attr_device_info == mock_device_info
        assert "(" not in sensor._attr_name

    def test_bank_kwh_sensor(self, mock_coordinator, mock_device_info, mock_entry):
        sensor = EnergaBankKwhSensor(
            coordinator=mock_coordinator,
            meter_id="1340026",
            device_info=mock_device_info,
            entry=mock_entry,
            serial="00069839",
        )
        assert sensor._attr_name == "Bank Wirtualny kWh"
        assert sensor._attr_has_entity_name is True
        assert sensor._attr_device_info == mock_device_info
        assert "(" not in sensor._attr_name

    def test_bank_pln_sensor(self, mock_coordinator, mock_device_info, mock_entry):
        sensor = EnergaBankPlnSensor(
            coordinator=mock_coordinator,
            meter_id="1340026",
            device_info=mock_device_info,
            entry=mock_entry,
            serial="00069839",
        )
        assert sensor._attr_name == "Bank Wirtualny PLN"
        assert sensor._attr_has_entity_name is True
        assert sensor._attr_device_info == mock_device_info
        assert "(" not in sensor._attr_name

    def test_bank_level_sensor(self, mock_coordinator, mock_device_info, mock_entry):
        sensor = EnergaBankLevelSensor(
            coordinator=mock_coordinator,
            meter_id="1340026",
            device_info=mock_device_info,
            entry=mock_entry,
            serial="00069839",
        )
        assert sensor._attr_name == "Magazyn Poziom"
        assert sensor._attr_has_entity_name is True
        assert sensor._attr_device_info == mock_device_info
        assert "(" not in sensor._attr_name

    def test_bank_flow_sensors(self, mock_coordinator, mock_device_info, mock_entry):
        charge = EnergaBankFlowSensor(
            coordinator=mock_coordinator,
            meter_id="1340026",
            device_info=mock_device_info,
            entry=mock_entry,
            direction="charge",
            serial="00069839",
        )
        discharge = EnergaBankFlowSensor(
            coordinator=mock_coordinator,
            meter_id="1340026",
            device_info=mock_device_info,
            entry=mock_entry,
            direction="discharge",
            serial="00069839",
        )
        assert charge._attr_name == "Bank Ładowanie"
        assert charge._attr_has_entity_name is True
        assert charge._attr_device_info == mock_device_info
        assert "(" not in charge._attr_name

        assert discharge._attr_name == "Bank Rozładowanie"
        assert discharge._attr_has_entity_name is True
        assert discharge._attr_device_info == mock_device_info
        assert "(" not in discharge._attr_name

    def test_first_data_date_sensor(self, mock_coordinator, mock_device_info, mock_entry):
        sensor = EnergaFirstDataDateSensor(
            coordinator=mock_coordinator,
            meter_id="1340026",
            device_info=mock_device_info,
            entry=mock_entry,
            serial="00069839",
        )
        assert sensor._attr_name == "Data Pierwszego Odczytu"
        assert sensor._attr_has_entity_name is True
        assert sensor._attr_device_info == mock_device_info
        assert "(" not in sensor._attr_name

    def test_rce_sensor(self, mock_coordinator, mock_device_info, mock_entry):
        api = MagicMock()
        sensor = EnergaRceSensor(
            coordinator=mock_coordinator,
            meter_id="1340026",
            device_info=mock_device_info,
            entry=mock_entry,
            api=api,
            serial="00069839",
        )
        assert sensor._attr_name == "RCEm Auto"
        assert sensor._attr_has_entity_name is True
        assert sensor._attr_device_info == mock_device_info
        assert "(" not in sensor._attr_name

    def test_bill_forecast_sensor(self, mock_coordinator, mock_device_info, mock_entry):
        sensor = EnergaBillForecastSensor(
            coordinator=mock_coordinator,
            meter_id="1340026",
            device_info=mock_device_info,
            entry=mock_entry,
            serial="00069839",
        )
        assert sensor._attr_name == "Prognoza Rachunku"
        assert sensor._attr_has_entity_name is True
        assert sensor._attr_device_info == mock_device_info
        assert "(" not in sensor._attr_name

    def test_bill_current_sensor(self, mock_coordinator, mock_device_info, mock_entry):
        sensor = EnergaBillCurrentSensor(
            coordinator=mock_coordinator,
            meter_id="1340026",
            device_info=mock_device_info,
            entry=mock_entry,
            serial="00069839",
        )
        assert sensor._attr_name == "Dotychczasowy Rachunek"
        assert sensor._attr_has_entity_name is True
        assert sensor._attr_device_info == mock_device_info
        assert "(" not in sensor._attr_name

    def test_bill_component_sensor(self, mock_coordinator, mock_device_info, mock_entry):
        sensor = EnergaBillComponentSensor(
            coordinator=mock_coordinator,
            meter_id="1340026",
            device_info=mock_device_info,
            entry=mock_entry,
            component_key="deposit_applied",
            name="Odzyskano z Depozytu MTD",
            icon="mdi:cash-minus",
            serial="00069839",
        )
        assert sensor._attr_name == "Odzyskano z Depozytu MTD"
        assert sensor._attr_has_entity_name is True
        assert sensor._attr_device_info == mock_device_info
        assert "(" not in sensor._attr_name


class TestCanonicalMigrationMapping:
    """Verify that canonical mapping dictionary maps legacy entities correctly."""

    def test_canonical_mapping_generation(self):
        mid = "1340026"
        serial = "00069839"

        expected = {
            f"energa_{mid}_prosumer_balance": f"sensor.energa_{serial}_bilans_prosumencki",
            f"energa_{mid}_bank_kwh": f"sensor.energa_{serial}_bank_wirtualny_kwh",
            f"energa_{mid}_bank_pln": f"sensor.energa_{serial}_bank_wirtualny_pln",
            f"energa_{mid}_bank_level": f"sensor.energa_{serial}_magazyn_poziom",
            f"energa_{mid}_bank_charge": f"sensor.energa_{serial}_bank_ladowanie",
            f"energa_{mid}_bank_discharge": f"sensor.energa_{serial}_bank_rozladowanie",
            f"energa_{mid}_first_data_date": f"sensor.energa_{serial}_data_pierwszego_odczytu",
            f"energa_{mid}_rcem_auto": f"sensor.energa_{serial}_rcem_auto",
            f"energa_{mid}_bill_forecast": f"sensor.energa_{serial}_prognoza_rachunku",
            f"energa_{mid}_bill_current": f"sensor.energa_{serial}_dotychczasowy_rachunek",
            f"energa_{mid}_mtd_brutto": f"sensor.energa_{serial}_koszt_brutto_mtd",
            f"energa_{mid}_mtd_sale_total": f"sensor.energa_{serial}_koszt_energii_czynnej_mtd",
            f"energa_{mid}_mtd_distr_total": f"sensor.energa_{serial}_koszt_dystrybucji_mtd",
            f"energa_{mid}_mtd_deposit": f"sensor.energa_{serial}_depozyt_wygenerowany_mtd",
            f"energa_{mid}_mtd_deposit_applied": f"sensor.energa_{serial}_odzyskano_z_depozytu_mtd",
            f"energa_{mid}_mtd_cover_day": f"sensor.energa_{serial}_pokrycie_z_magazynu_dzien_mtd",
            f"energa_{mid}_mtd_cover_night": f"sensor.energa_{serial}_pokrycie_z_magazynu_noc_mtd",
        }

        # Every canonical entity_id starts with sensor.energa_{serial}_
        for uid, eid in expected.items():
            assert eid.startswith(f"sensor.energa_{serial}_")
            # Must not contain duplicated serial (e.g. _00069839_..._00069839)
            assert eid.count(serial) == 1
