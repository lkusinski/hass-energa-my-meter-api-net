"""Unit tests for onboarding resilience, timeout fallback, and auto-backfill recovery (v1.4.1)."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo
import pytest

from custom_components.energa_mobile.settlement import system_choice_coefficient
from custom_components.energa_mobile.const import (
    CONF_PROSUMER_COEFFICIENT,
    CONF_USERNAME,
    CONF_PASSWORD,
    DEFAULT_PROSUMER_COEFFICIENT,
)
from custom_components.energa_mobile.config_flow import EnergaConfigFlow
from custom_components.energa_mobile import (
    _has_history_statistics,
    _maybe_auto_backfill,
    TIMEZONE,
)


class TestSystemChoiceCoefficient:
    """Test prosumer system selection parsing."""

    def test_nowe_returns_zero(self):
        assert system_choice_coefficient("nowe") == 0.0
        assert system_choice_coefficient(" NOWE ") == 0.0

    def test_brak_returns_zero(self):
        assert system_choice_coefficient("brak") == 0.0
        assert system_choice_coefficient(" BRAK ") == 0.0

    def test_stare_returns_point_eight(self):
        assert system_choice_coefficient("stare") == 0.8
        assert system_choice_coefficient("opusty") == 0.8

    def test_invalid_or_none_defaults_to_point_eight(self):
        assert system_choice_coefficient(None) == 0.8
        assert system_choice_coefficient(123) == 0.8


class TestConfigFlowProsumerFallback:
    """Test prosumer detection timeout fallback in ConfigFlow."""

    @pytest.mark.asyncio
    async def test_fetch_meters_timeout_presents_fallback_step(self):
        flow = EnergaConfigFlow()
        flow.hass = MagicMock()
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow.async_show_form = MagicMock(side_effect=lambda **kwargs: {"type": "form", **kwargs})
        flow.async_create_entry = MagicMock(side_effect=lambda title, data, options=None: {"type": "create_entry", "title": title, "data": data, "options": options or {}})

        with patch("custom_components.energa_mobile.config_flow.EnergaAPI") as mock_api_cls:
            api_instance = mock_api_cls.return_value
            api_instance.async_login = AsyncMock(return_value=True)
            # Simulate timeout during _fetch_all_meters
            api_instance._fetch_all_meters = AsyncMock(side_effect=TimeoutError("Energa API timeout"))

            res = await flow.async_step_user({CONF_USERNAME: "test@example.com", CONF_PASSWORD: "secret"})

            # Should transition to system_fallback step
            assert res["type"] == "form"
            assert res["step_id"] == "system_fallback"

    @pytest.mark.asyncio
    async def test_system_fallback_submission(self):
        flow = EnergaConfigFlow()
        flow.hass = MagicMock()
        flow._pending_title = "test@example.com"
        flow._pending_data = {CONF_USERNAME: "test@example.com"}
        flow.async_create_entry = MagicMock(side_effect=lambda title, data, options=None: {"type": "create_entry", "title": title, "data": data, "options": options or {}})

        # Choice: brak -> no prosumer coeff set
        res_brak = await flow.async_step_system_fallback({"system": "brak"})
        assert res_brak["type"] == "create_entry"
        assert CONF_PROSUMER_COEFFICIENT not in res_brak["options"]

        # Choice: nowe -> coeff 0.0
        res_nowe = await flow.async_step_system_fallback({"system": "nowe"})
        assert res_nowe["type"] == "create_entry"
        assert res_nowe["options"][CONF_PROSUMER_COEFFICIENT] == 0.0

        # Choice: stare -> coeff 0.8
        res_stare = await flow.async_step_system_fallback({"system": "stare"})
        assert res_stare["type"] == "create_entry"
        assert res_stare["options"][CONF_PROSUMER_COEFFICIENT] == 0.8


class TestAutoBackfillResilience:
    """Test auto-backfill completion tracking and history checking."""

    @pytest.mark.asyncio
    async def test_has_history_statistics_true_when_historical_data_exists(self):
        import sys
        hass = MagicMock()
        meters = [{"meter_point_id": "12345", "zone_count": 1}]

        mock_registry = MagicMock()
        mock_entity = MagicMock()
        mock_entity.unique_id = "energa_12345_import_stats"
        mock_entity.entity_id = "sensor.energa_12345_panel_energia_zuzycie"
        mock_registry.entities = {mock_entity.entity_id: mock_entity}

        er_mod = sys.modules["homeassistant.helpers"].entity_registry
        rec_mod = sys.modules["homeassistant.components.recorder"]

        with patch.object(er_mod, "async_get", return_value=mock_registry), \
             patch.object(rec_mod, "get_instance") as mock_get_rec:
            rec_instance = mock_get_rec.return_value
            rec_instance.async_add_executor_job = AsyncMock(
                return_value={"sensor.energa_12345_panel_energia_zuzycie": [{"sum": 120.5}]}
            )

            start_date = datetime(2024, 1, 1, tzinfo=TIMEZONE)
            has_history = await _has_history_statistics(hass, meters, start_date)
            assert has_history is True

    @pytest.mark.asyncio
    async def test_has_history_statistics_false_when_historical_data_empty(self):
        import sys
        hass = MagicMock()
        meters = [{"meter_point_id": "12345", "zone_count": 1}]

        mock_registry = MagicMock()
        mock_entity = MagicMock()
        mock_entity.unique_id = "energa_12345_import_stats"
        mock_entity.entity_id = "sensor.energa_12345_panel_energia_zuzycie"
        mock_registry.entities = {mock_entity.entity_id: mock_entity}

        er_mod = sys.modules["homeassistant.helpers"].entity_registry
        rec_mod = sys.modules["homeassistant.components.recorder"]

        with patch.object(er_mod, "async_get", return_value=mock_registry), \
             patch.object(rec_mod, "get_instance") as mock_get_rec:
            rec_instance = mock_get_rec.return_value
            rec_instance.async_add_executor_job = AsyncMock(
                return_value={"sensor.energa_12345_panel_energia_zuzycie": []}
            )

            start_date = datetime(2024, 1, 1, tzinfo=TIMEZONE)
            has_history = await _has_history_statistics(hass, meters, start_date)
            assert has_history is False

    @pytest.mark.asyncio
    async def test_maybe_auto_backfill_skips_when_completed_flag_set(self):
        hass = MagicMock()
        api = MagicMock()
        api.async_get_data = AsyncMock(return_value=[{"total_plus": 1000.0, "meter_point_id": "12345"}])

        entry = MagicMock()
        entry.data = {"auto_backfill_completed": True}
        entry.options = {}

        with patch("custom_components.energa_mobile.async_provision_dashboard", AsyncMock()) as mock_prov, \
             patch("custom_components.energa_mobile._import_meter_history", AsyncMock()) as mock_import:
            await _maybe_auto_backfill(hass, api, entry)
            # Dashboard is provisioned
            mock_prov.assert_awaited_once()
            # History import is skipped
            mock_import.assert_not_called()

    @pytest.mark.asyncio
    async def test_maybe_auto_backfill_marks_completed_when_history_found(self):
        hass = MagicMock()
        api = MagicMock()
        api.async_get_data = AsyncMock(return_value=[{"total_plus": 1000.0, "meter_point_id": "12345"}])

        entry = MagicMock()
        entry.data = {"auto_backfill_completed": False, "auto_history_start": "2024-01-01"}
        entry.options = {}

        with patch("custom_components.energa_mobile.async_provision_dashboard", AsyncMock()), \
             patch("custom_components.energa_mobile._has_history_statistics", AsyncMock(return_value=True)), \
             patch("custom_components.energa_mobile._import_meter_history", AsyncMock()) as mock_import:
            await _maybe_auto_backfill(hass, api, entry)
            # Backfill import skipped because history already present
            mock_import.assert_not_called()
            # Entry data updated to mark completed
            hass.config_entries.async_update_entry.assert_called_once()
            updated_data = hass.config_entries.async_update_entry.call_args[1]["data"]
            assert updated_data["auto_backfill_completed"] is True
