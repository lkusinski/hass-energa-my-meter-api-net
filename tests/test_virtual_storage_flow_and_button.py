"""Unit tests for Virtual Storage Net-metering config flow, button, and synthetic sensors (v1.6.0)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.energa_mobile.button import EnergaConfigureEnergyDashboardButton
from custom_components.energa_mobile.config_flow import EnergaOptionsFlow
from custom_components.energa_mobile.const import (
    CONF_BANK_INITIAL_KWH,
    CONF_BANK_INITIAL_KWH_L1,
    CONF_BANK_INITIAL_KWH_L2,
    CONF_ENABLE_SYNTHETIC_STORAGE,
    CONF_ENERGY_DASHBOARD_MODE,
    CONF_INVERTER_ENERGY_ENTITY,
    CONF_PROSUMER_COEFFICIENT,
    CONF_PROSUMER_POWER_GROUP,
    ENERGY_MODE_VIRTUAL_STORAGE,
    POWER_GROUP_GT_10KW,
    POWER_GROUP_LE_10KW,
)
from custom_components.energa_mobile.sensor import (
    EnergaBankKwhSensor,
    EnergaSyntheticStatisticsSensor,
)


class TestVirtualStorageOptionsFlow:
    """Tests for the Energy Dashboard options step."""

    @pytest.mark.asyncio
    async def test_options_energy_dashboard_form_display(self):
        entry = MagicMock()
        entry.options = {
            CONF_ENERGY_DASHBOARD_MODE: ENERGY_MODE_VIRTUAL_STORAGE,
            CONF_PROSUMER_POWER_GROUP: POWER_GROUP_LE_10KW,
            CONF_ENABLE_SYNTHETIC_STORAGE: True,
        }
        flow = EnergaOptionsFlow(entry)
        flow.async_show_form = MagicMock(return_value={"type": "form", "step_id": "energy_dashboard"})

        res = await flow.async_step_energy_dashboard(None)
        assert res["type"] == "form"
        assert res["step_id"] == "energy_dashboard"

    @pytest.mark.asyncio
    async def test_options_energy_dashboard_submit_gt_10kw(self):
        entry = MagicMock()
        entry.options = {
            CONF_ENERGY_DASHBOARD_MODE: ENERGY_MODE_VIRTUAL_STORAGE,
            CONF_PROSUMER_POWER_GROUP: POWER_GROUP_LE_10KW,
            CONF_ENABLE_SYNTHETIC_STORAGE: True,
        }
        flow = EnergaOptionsFlow(entry)
        flow.async_create_entry = MagicMock(side_effect=lambda title, data: {"type": "create_entry", "data": data})

        user_input = {
            CONF_PROSUMER_POWER_GROUP: POWER_GROUP_GT_10KW,
            CONF_ENERGY_DASHBOARD_MODE: ENERGY_MODE_VIRTUAL_STORAGE,
            CONF_ENABLE_SYNTHETIC_STORAGE: True,
        }
        res = await flow.async_step_energy_dashboard(user_input)
        assert res["type"] == "create_entry"
        assert res["data"][CONF_PROSUMER_POWER_GROUP] == POWER_GROUP_GT_10KW
        assert res["data"][CONF_PROSUMER_COEFFICIENT] == 0.7
        assert res["data"][CONF_ENABLE_SYNTHETIC_STORAGE] is True

    @pytest.mark.asyncio
    async def test_options_prices_dual_zone_initial_bank_auto_sum(self):
        entry = MagicMock()
        entry.options = {
            CONF_BANK_INITIAL_KWH: 0.0,
        }
        flow = EnergaOptionsFlow(entry)
        flow.async_create_entry = MagicMock(side_effect=lambda title, data: {"type": "create_entry", "data": data})
        flow._get_active_meters = MagicMock(return_value=[{"meter_point_id": "10000001", "zone_count": 2}])

        user_input = {
            CONF_BANK_INITIAL_KWH_L1: 752.0,
            CONF_BANK_INITIAL_KWH_L2: 606.0,
            CONF_BANK_INITIAL_KWH: 0.0,
        }
        res = await flow.async_step_prices(user_input)
        assert res["type"] == "create_entry"
        assert res["data"][CONF_BANK_INITIAL_KWH] == 1358.0
        assert res["data"][CONF_BANK_INITIAL_KWH_L1] == 752.0
        assert res["data"][CONF_BANK_INITIAL_KWH_L2] == 606.0



class TestConfigureEnergyDashboardButton:
    """Tests for the automated Energy Dashboard configuration button."""

    @pytest.mark.asyncio
    async def test_button_press_multi_zone_virtual_storage(self):
        hass = MagicMock()
        mock_manager = MagicMock()
        mock_manager.data = {"energy_sources": []}
        mock_manager.async_update = AsyncMock()

        entry = MagicMock()
        entry.options = {
            CONF_ENABLE_SYNTHETIC_STORAGE: True,
            CONF_INVERTER_ENERGY_ENTITY: "sensor.solis_total_yield",
        }
        meter = {
            "meter_point_id": "10000001",
            "meter_serial": "10000001",
            "ppe": "PPE10000001",
            "zone_count": 2,
            "tariff": "G12W",
        }

        button = EnergaConfigureEnergyDashboardButton(hass=hass, entry=entry, meter=meter)

        with patch("homeassistant.components.energy.data.async_get_manager", AsyncMock(return_value=mock_manager)):
            await button.async_press()

        assert mock_manager.async_update.called
        saved_prefs = mock_manager.async_update.call_args[0][0]
        sources = saved_prefs["energy_sources"]

        # Check grid sources (modern flat GridSourceType format)
        grid_sources = [s for s in sources if s.get("type") == "grid"]
        assert len(grid_sources) == 2
        assert grid_sources[0]["stat_energy_from"] == "sensor.energa_10000001_syntetyczna_siec_pobor_strefa_1"
        assert grid_sources[0]["stat_energy_to"] == "sensor.energa_10000001_syntetyczna_siec_oddanie_strefa_1"
        assert grid_sources[0]["number_energy_price_export"] == 0.0
        assert grid_sources[1]["stat_energy_from"] == "sensor.energa_10000001_syntetyczna_siec_pobor_strefa_2"
        assert grid_sources[1]["stat_energy_to"] == "sensor.energa_10000001_syntetyczna_siec_oddanie_strefa_2"
        assert grid_sources[1]["number_energy_price_export"] == 0.0

        # Check battery sources (L1 and L2)
        batteries = [s for s in sources if s.get("type") == "battery"]
        assert len(batteries) == 2
        assert batteries[0]["stat_energy_to"] == "sensor.energa_10000001_syntetyczny_magazyn_l1_ladowanie"
        assert batteries[0]["stat_energy_from"] == "sensor.energa_10000001_syntetyczny_magazyn_l1_rozladowanie"
        assert batteries[1]["stat_energy_to"] == "sensor.energa_10000001_syntetyczny_magazyn_l2_ladowanie"
        assert batteries[1]["stat_energy_from"] == "sensor.energa_10000001_syntetyczny_magazyn_l2_rozladowanie"

        # Check solar source
        solar = next(s for s in sources if s.get("type") == "solar")
        assert solar["stat_energy_from"] == "sensor.solis_total_yield"

    @pytest.mark.asyncio
    async def test_button_press_single_zone_physical_grid(self):
        hass = MagicMock()
        mock_manager = MagicMock()
        mock_manager.data = {"energy_sources": []}
        mock_manager.async_update = AsyncMock()

        entry = MagicMock()
        entry.options = {
            CONF_ENABLE_SYNTHETIC_STORAGE: False,
        }
        meter = {
            "meter_point_id": "10000003",
            "meter_serial": "10000003",
            "ppe": "PPE10000003",
            "zone_count": 1,
            "tariff": "G11",
            "obis_minus": "1.8.0",
        }

        button = EnergaConfigureEnergyDashboardButton(hass=hass, entry=entry, meter=meter)

        with patch("homeassistant.components.energy.data.async_get_manager", AsyncMock(return_value=mock_manager)):
            await button.async_press()

        assert mock_manager.async_update.called
        saved_prefs = mock_manager.async_update.call_args[0][0]
        sources = saved_prefs["energy_sources"]

        # In physical grid mode: no batteries
        batteries = [s for s in sources if s.get("type") == "battery"]
        assert len(batteries) == 0

        # Physical grid flows (modern flat GridSourceType)
        grid_sources = [s for s in sources if s.get("type") == "grid"]
        assert len(grid_sources) == 1
        assert grid_sources[0]["stat_energy_from"] == "sensor.energa_10000003_panel_energia_zuzycie"
        assert grid_sources[0]["stat_energy_to"] == "sensor.energa_10000003_panel_energia_produkcja"


class TestSyntheticSensor:
    """Tests for EnergaSyntheticStatisticsSensor attributes."""

    def test_synthetic_sensor_properties(self):
        coord = MagicMock()
        coord.data = [{"meter_point_id": "123"}]
        entry = MagicMock()
        dev_info = MagicMock()

        sensor = EnergaSyntheticStatisticsSensor(
            coordinator=coord,
            meter_id="12345",
            data_key="syntetyczny_magazyn_l1_ladowanie",
            name="Syntetyczny Magazyn L1 Ładowanie",
            icon="mdi:battery-charging",
            device_info=dev_info,
            entry=entry,
            serial="12345",
        )

        assert sensor.entity_id == "sensor.energa_12345_syntetyczny_magazyn_l1_ladowanie"
        assert sensor.native_value is None
        assert sensor.available is True

    def test_bank_sensor_single_zone_g11_unbound_init_fix(self):
        coord = MagicMock()
        coord.data = [{
            "meter_point_id": "10000003",
            "meter_serial": "10000003",
            "zone_count": 1,
            "tariff": "G11",
            "is_prosumer": True,
            "total_plus": 1000.0,
            "total_minus": 2000.0,
        }]
        coord._meter_totals = {"10000003": {"import": 1000.0, "export": 2000.0}}
        coord._monthly = {"10000003": {}}


        entry = MagicMock()
        entry.options = {
            CONF_PROSUMER_COEFFICIENT: 0.8,
            # baselines are 0.0, initial is 0.0, no init_l1/init_l2 in single-zone
        }
        dev_info = MagicMock()

        sensor = EnergaBankKwhSensor(
            coordinator=coord,
            meter_id="10000003",
            serial="10000003",
            device_info=dev_info,
            entry=entry,
            has_zones=False,
        )

        # Should compute without UnboundLocalError for init_l1 / init_l2
        val = sensor.native_value
        assert val is not None
        assert val == (2000.0 * 0.8) - 1000.0  # 1600 - 1000 = 600

    @pytest.mark.asyncio
    async def test_async_synthesize_storage_from_recorder(self, monkeypatch):
        from custom_components.energa_mobile.synthetic_storage import (
            async_synthesize_storage_from_recorder,
        )

        hass = MagicMock()
        entry = MagicMock()
        entry.options = {
            CONF_ENABLE_SYNTHETIC_STORAGE: True,
            CONF_PROSUMER_COEFFICIENT: 0.8,
        }

        meter = {
            "meter_point_id": "10000003",
            "meter_serial": "10000003",
            "zone_count": 1,
            "tariff": "G11",
            "is_prosumer": True,
        }

        # Mock recorder get_instance and statistics_during_period
        imported_stats = []

        def mock_async_import_statistics(h, meta, stats):
            imported_stats.append((meta.statistic_id, stats))

        import sys
        rec_stat_mod = sys.modules["homeassistant.components.recorder.statistics"]
        rec_stat_mod.async_import_statistics = mock_async_import_statistics

        def mock_executor_job(func, *args):
            stat_ids = func.args[3] if hasattr(func, "args") and len(func.args) > 3 else []
            if "sensor.energa_10000003_syntetyczny_magazyn_ladowanie" in stat_ids:
                return {}
            # raw stats
            ts1 = 1725487200.0
            ts2 = 1725490800.0
            return {
                "sensor.energa_10000003_panel_energia_zuzycie": [
                    {"start": ts1, "state": 1.5},
                    {"start": ts2, "state": 0.5},
                ],
                "sensor.energa_10000003_panel_energia_produkcja": [
                    {"start": ts1, "state": 2.0},
                    {"start": ts2, "state": 0.0},
                ],
            }

        import sys
        rec_mod = sys.modules["homeassistant.components.recorder"]
        rec_instance = rec_mod.get_instance.return_value
        rec_instance.async_add_executor_job = AsyncMock(side_effect=mock_executor_job)

        res = await async_synthesize_storage_from_recorder(hass, entry, meter)
        assert res is True
        assert len(imported_stats) == 4
        stat_ids = [s[0] for s in imported_stats]
        assert "sensor.energa_10000003_syntetyczny_magazyn_ladowanie" in stat_ids
        assert "sensor.energa_10000003_syntetyczny_magazyn_rozladowanie" in stat_ids
        assert "sensor.energa_10000003_syntetyczna_siec_oddanie" in stat_ids
        assert "sensor.energa_10000003_syntetyczna_siec_pobor" in stat_ids

    @pytest.mark.asyncio
    async def test_button_press_uppercase_serial_normalized(self):
        """Ensure uppercase serials (like V705048953698419) produce lowercase entity IDs."""
        hass = MagicMock()
        mock_manager = MagicMock()
        mock_manager.data = {"energy_sources": []}
        mock_manager.async_update = AsyncMock()

        entry = MagicMock()
        entry.options = {
            CONF_ENABLE_SYNTHETIC_STORAGE: True,
        }
        meter = {
            "meter_point_id": "V705048953698419",
            "meter_serial": "V705048953698419",
            "ppe": "PL0037000000000009",
            "zone_count": 2,
            "tariff": "G12",
        }

        button = EnergaConfigureEnergyDashboardButton(hass=hass, entry=entry, meter=meter)

        with patch("homeassistant.components.energy.data.async_get_manager", AsyncMock(return_value=mock_manager)):
            await button.async_press()

        assert mock_manager.async_update.called
        saved_prefs = mock_manager.async_update.call_args[0][0]
        sources = saved_prefs["energy_sources"]

        grid_sources = [s for s in sources if s.get("type") == "grid"]
        for gs in grid_sources:
            assert "v705048953698419" in gs["stat_energy_from"]
            assert "V705048953698419" not in gs["stat_energy_from"]

        battery_sources = [s for s in sources if s.get("type") == "battery"]
        for bs in battery_sources:
            assert "v705048953698419" in bs["stat_energy_to"]
            assert "V705048953698419" not in bs["stat_energy_to"]


