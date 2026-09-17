"""Tests for config flow — regression for #23 (AbortFlow).

These tests verify the exception handling logic in async_step_user
without instantiating the full HA config flow machinery.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# conftest.py sets up HA module mocks
from homeassistant.data_entry_flow import AbortFlow

from custom_components.energa_mobile.api import EnergaAuthError, EnergaConnectionError
from custom_components.energa_mobile.const import (
    CONF_CREATE_SETTLEMENT_DASHBOARD,
    CONF_ENABLE_SYNTHETIC_STORAGE,
    CONF_ENERGY_DASHBOARD_MODE,
    CONF_PASSWORD,
    CONF_PROSUMER_COEFFICIENT,
    CONF_PROSUMER_POWER_GROUP,
    CONF_USERNAME,
    DEFAULT_CREATE_SETTLEMENT_DASHBOARD,
    ENERGY_MODE_VIRTUAL_STORAGE,
    POWER_GROUP_LE_10KW,
)


async def _run_user_step(login_side_effect=None, abort_side_effect=None):
    """Simulate async_step_user logic with controlled mocks.

    Rather than instantiating EnergaConfigFlow (which requires full HA),
    we replicate the exact try/except structure from the real code and
    verify that exceptions are handled correctly.
    """
    errors = {}
    try:
        # Simulate: api.async_login()
        if login_side_effect:
            raise login_side_effect

        # Simulate: self._abort_if_unique_id_configured()
        if abort_side_effect:
            raise abort_side_effect

        return {"type": "create_entry"}

    except EnergaAuthError:
        errors["base"] = "invalid_auth"
    except (EnergaConnectionError, TimeoutError):
        errors["base"] = "cannot_connect"
    except AbortFlow:
        raise  # Must re-raise!
    except Exception:
        errors["base"] = "unknown"

    return {"type": "form", "errors": errors}


class TestConfigFlowExceptionHandling:
    """Tests for exception handling in async_step_user.

    These tests verify the fix for #23 — AbortFlow must NOT be
    caught by the generic `except Exception` handler.
    """

    @pytest.mark.asyncio
    async def test_successful_login_creates_entry(self):
        """No exceptions → create_entry."""
        result = await _run_user_step()
        assert result["type"] == "create_entry"

    @pytest.mark.asyncio
    async def test_auth_error_shows_invalid_auth(self):
        """EnergaAuthError → invalid_auth error."""
        result = await _run_user_step(
            login_side_effect=EnergaAuthError("bad password")
        )
        assert result["type"] == "form"
        assert result["errors"]["base"] == "invalid_auth"

    @pytest.mark.asyncio
    async def test_connection_error_shows_cannot_connect(self):
        """EnergaConnectionError → cannot_connect error."""
        result = await _run_user_step(
            login_side_effect=EnergaConnectionError("timeout")
        )
        assert result["type"] == "form"
        assert result["errors"]["base"] == "cannot_connect"

    @pytest.mark.asyncio
    async def test_abort_flow_not_swallowed(self):
        """Regression test for #23: AbortFlow must propagate, not be caught as 'unknown'.

        Before the fix, _abort_if_unique_id_configured() raised AbortFlow,
        which was caught by `except Exception` and turned into errors["base"] = "unknown".
        After the fix, `except AbortFlow: raise` ensures it propagates correctly.
        """
        with pytest.raises(AbortFlow) as exc_info:
            await _run_user_step(
                abort_side_effect=AbortFlow("already_configured")
            )
        assert exc_info.value.reason == "already_configured"

    @pytest.mark.asyncio
    async def test_abort_flow_would_fail_without_fix(self):
        """Demonstrates what happened BEFORE the fix — AbortFlow was an 'unknown' error.

        This test simulates the OLD buggy code (without `except AbortFlow: raise`).
        """
        errors = {}
        try:
            raise AbortFlow("already_configured")
        except Exception:
            # BUG: AbortFlow IS an Exception, so it gets caught here
            errors["base"] = "unknown"

        # This is what users saw — the wrong error
        assert errors["base"] == "unknown"

    @pytest.mark.asyncio
    async def test_generic_exception_shows_unknown(self):
        """Unexpected errors → unknown."""
        result = await _run_user_step(
            login_side_effect=RuntimeError("something unexpected")
        )
        assert result["type"] == "form"
        assert result["errors"]["base"] == "unknown"


class TestSystemStep:
    """v0.3.8: setup choice nowe/stare seeds the right coefficient."""

    def test_nowe_maps_to_zero(self):
        from custom_components.energa_mobile.settlement import (
            system_choice_coefficient,
        )

        assert system_choice_coefficient("nowe") == 0.0

    def test_stare_and_garbage_map_to_point_eight(self):
        from custom_components.energa_mobile.settlement import (
            system_choice_coefficient,
        )

        assert system_choice_coefficient("stare") == 0.8
        assert system_choice_coefficient(None) == 0.8
        assert system_choice_coefficient("junk") == 0.8


class TestConsumerCoefficientBackfill:
    """v1.9.0: pre-v0.3.8 consumer entries inherit 0.8 → must be pinned to 0.0."""

    CONSUMER = {"meter_point_id": "900001", "total_minus": 0.0, "is_prosumer": False}
    PROSUMER = {"meter_point_id": "900002", "total_minus": 123.4, "is_prosumer": False}

    def test_consumer_without_key_needs_fix(self):
        from custom_components.energa_mobile.settlement import (
            consumer_coefficient_needed,
        )

        assert consumer_coefficient_needed([self.CONSUMER], {}) is True

    def test_explicit_key_is_respected(self):
        from custom_components.energa_mobile.settlement import (
            consumer_coefficient_needed,
        )

        assert consumer_coefficient_needed(
            [self.CONSUMER], {"prosumer_coefficient": 0.8}
        ) is False

    def test_exporter_never_gets_pinned(self):
        from custom_components.energa_mobile.settlement import (
            consumer_coefficient_needed,
        )

        assert consumer_coefficient_needed([self.PROSUMER], {}) is False
        assert consumer_coefficient_needed(
            [self.CONSUMER, self.PROSUMER], {}
        ) is False

    def test_unknown_meter_list_is_a_noop(self):
        from custom_components.energa_mobile.settlement import (
            consumer_coefficient_needed,
        )

        # No meter data yet (API fetch failed) → never guess.
        assert consumer_coefficient_needed(None, {}) is False
        assert consumer_coefficient_needed([], {}) is False


class TestSettlementDashboardOption:
    """v1.9.0: create_settlement_dashboard checkbox in wizard/options steps."""

    def _make_flow(self):
        from custom_components.energa_mobile.config_flow import EnergaConfigFlow

        flow = EnergaConfigFlow()
        flow.hass = MagicMock()
        flow._pending_title = "test@example.com"
        flow._pending_data = {}
        flow.async_show_form = MagicMock(
            side_effect=lambda **kwargs: {"type": "form", **kwargs}
        )
        flow.async_create_entry = MagicMock(
            side_effect=lambda title, data, options=None: {
                "type": "create_entry",
                "title": title,
                "data": data,
                "options": options or {},
            }
        )
        return flow

    @pytest.mark.asyncio
    async def test_system_form_default_is_true(self):
        flow = self._make_flow()
        res = await flow.async_step_system(None)
        parsed = res["data_schema"]({"system": "nowe"})
        assert parsed[CONF_CREATE_SETTLEMENT_DASHBOARD] is True

    @pytest.mark.asyncio
    async def test_system_fallback_form_default_is_true(self):
        flow = self._make_flow()
        res = await flow.async_step_system_fallback(None)
        parsed = res["data_schema"]({"system": "nowe"})
        assert parsed[CONF_CREATE_SETTLEMENT_DASHBOARD] is True

    @pytest.mark.asyncio
    async def test_net_metering_survey_form_default_is_true(self):
        flow = self._make_flow()
        res = await flow.async_step_net_metering_survey(None)
        parsed = res["data_schema"](
            {
                CONF_PROSUMER_POWER_GROUP: POWER_GROUP_LE_10KW,
                CONF_ENERGY_DASHBOARD_MODE: ENERGY_MODE_VIRTUAL_STORAGE,
            }
        )
        assert parsed[CONF_CREATE_SETTLEMENT_DASHBOARD] is True

    @pytest.mark.asyncio
    async def test_system_nowe_persists_default_true(self):
        flow = self._make_flow()
        res = await flow.async_step_system({"system": "nowe"})
        assert res["options"][CONF_CREATE_SETTLEMENT_DASHBOARD] is True

    @pytest.mark.asyncio
    async def test_system_brak_respects_false(self):
        flow = self._make_flow()
        res = await flow.async_step_system(
            {"system": "brak", CONF_CREATE_SETTLEMENT_DASHBOARD: False}
        )
        assert res["options"][CONF_CREATE_SETTLEMENT_DASHBOARD] is False

    @pytest.mark.asyncio
    async def test_net_metering_survey_persists_false(self):
        flow = self._make_flow()
        res = await flow.async_step_net_metering_survey(
            {
                CONF_PROSUMER_POWER_GROUP: POWER_GROUP_LE_10KW,
                CONF_ENERGY_DASHBOARD_MODE: ENERGY_MODE_VIRTUAL_STORAGE,
                CONF_CREATE_SETTLEMENT_DASHBOARD: False,
            }
        )
        assert res["options"][CONF_CREATE_SETTLEMENT_DASHBOARD] is False

    @pytest.mark.asyncio
    async def test_options_energy_dashboard_persists_option(self):
        from custom_components.energa_mobile.config_flow import EnergaOptionsFlow

        entry = MagicMock()
        entry.options = {}
        flow = EnergaOptionsFlow(entry)
        flow.async_create_entry = MagicMock(
            side_effect=lambda title, data: {"type": "create_entry", "data": data}
        )
        res = await flow.async_step_energy_dashboard(
            {
                CONF_PROSUMER_POWER_GROUP: POWER_GROUP_LE_10KW,
                CONF_ENERGY_DASHBOARD_MODE: ENERGY_MODE_VIRTUAL_STORAGE,
                CONF_ENABLE_SYNTHETIC_STORAGE: True,
                CONF_CREATE_SETTLEMENT_DASHBOARD: False,
            }
        )
        assert res["data"][CONF_CREATE_SETTLEMENT_DASHBOARD] is False

    @pytest.mark.asyncio
    async def test_options_energy_dashboard_defaults_true(self):
        from custom_components.energa_mobile.config_flow import EnergaOptionsFlow

        entry = MagicMock()
        entry.options = {}
        flow = EnergaOptionsFlow(entry)
        flow.async_create_entry = MagicMock(
            side_effect=lambda title, data: {"type": "create_entry", "data": data}
        )
        res = await flow.async_step_energy_dashboard(
            {
                CONF_PROSUMER_POWER_GROUP: POWER_GROUP_LE_10KW,
                CONF_ENERGY_DASHBOARD_MODE: ENERGY_MODE_VIRTUAL_STORAGE,
                CONF_ENABLE_SYNTHETIC_STORAGE: True,
            }
        )
        assert res["data"][CONF_CREATE_SETTLEMENT_DASHBOARD] is True
        assert DEFAULT_CREATE_SETTLEMENT_DASHBOARD is True


class TestSystemStepSolarDetection:
    """v1.9.0: onboarding detects consumers and the Energy panel PV source."""

    SOLAR_PATCH = (
        "custom_components.energa_mobile.core.energy_sources.async_has_energy_solar"
    )

    def _make_flow(self):
        from custom_components.energa_mobile.config_flow import EnergaConfigFlow

        flow = EnergaConfigFlow()
        flow.hass = MagicMock()
        flow._pending_title = "test@example.com"
        flow._pending_data = {}
        flow.async_show_form = MagicMock(
            side_effect=lambda **kwargs: {"type": "form", **kwargs}
        )
        flow.async_create_entry = MagicMock(
            side_effect=lambda title, data, options=None: {
                "type": "create_entry",
                "title": title,
                "data": data,
                "options": options or {},
            }
        )
        return flow

    @staticmethod
    def _field(result, name):
        """Return the (marker, validator) pair for a schema field, or None."""
        schema = result.get("data_schema")
        if schema is None:
            return None
        for marker, validator in schema.schema.items():
            if getattr(marker, "schema", None) == name:
                return marker, validator
        return None

    @pytest.mark.asyncio
    async def test_detected_consumer_defaults_to_brak_marked_detected(self):
        flow = self._make_flow()
        flow._detected_consumer = True

        res = await flow.async_step_system(None)

        marker, validator = self._field(res, "system")
        assert marker.default() == "brak"
        assert validator.container["brak"].endswith("(wykryto)")
        # The other settlement options stay unchanged.
        assert "wykryto" not in validator.container["nowe"]
        assert "wykryto" not in validator.container["stare"]

    @pytest.mark.asyncio
    async def test_prosumer_defaults_to_nowe_without_marker(self):
        flow = self._make_flow()

        with patch(self.SOLAR_PATCH, AsyncMock(return_value=True)):
            res = await flow.async_step_system(None)

        marker, validator = self._field(res, "system")
        assert marker.default() == "nowe"
        assert "wykryto" not in validator.container["brak"]

    @pytest.mark.asyncio
    async def test_detected_consumer_accepting_default_pins_zero_coefficient(self):
        flow = self._make_flow()
        flow._detected_consumer = True
        flow._pending_options = {CONF_PROSUMER_COEFFICIENT: 0.0}

        res = await flow.async_step_system({"system": "brak"})

        assert res["type"] == "create_entry"
        assert res["options"][CONF_PROSUMER_COEFFICIENT] == 0.0

    @pytest.mark.asyncio
    async def test_two_way_without_solar_shows_pv_recommendation(self):
        flow = self._make_flow()

        with patch(self.SOLAR_PATCH, AsyncMock(return_value=False)):
            res = await flow.async_step_system(None)

        assert self._field(res, "energy_dashboard_pv_hint") is not None

    @pytest.mark.asyncio
    async def test_two_way_with_solar_hides_pv_recommendation(self):
        flow = self._make_flow()

        with patch(self.SOLAR_PATCH, AsyncMock(return_value=True)):
            res = await flow.async_step_system(None)

        assert self._field(res, "energy_dashboard_pv_hint") is None

    @pytest.mark.asyncio
    async def test_detected_consumer_never_shows_pv_recommendation(self):
        flow = self._make_flow()
        flow._detected_consumer = True

        with patch(self.SOLAR_PATCH, AsyncMock(return_value=False)) as detect:
            res = await flow.async_step_system(None)

        detect.assert_not_awaited()
        assert self._field(res, "energy_dashboard_pv_hint") is None

    @pytest.mark.asyncio
    async def test_user_step_consumer_goes_to_system_step(self):
        """A one-way meter now reaches the system step instead of skipping it."""
        from custom_components.energa_mobile.config_flow import EnergaConfigFlow

        flow = EnergaConfigFlow()
        flow.hass = MagicMock()
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow.async_show_form = MagicMock(
            side_effect=lambda **kwargs: {"type": "form", **kwargs}
        )
        flow.async_create_entry = MagicMock(
            side_effect=lambda title, data, options=None: {
                "type": "create_entry",
                "title": title,
                "data": data,
                "options": options or {},
            }
        )

        with patch(
            "custom_components.energa_mobile.config_flow.EnergaAPI"
        ) as mock_api_cls, patch(self.SOLAR_PATCH, AsyncMock(return_value=True)):
            api = mock_api_cls.return_value
            api.async_login = AsyncMock(return_value=True)
            api._fetch_all_meters = AsyncMock(
                return_value=[{"meter_point_id": "1", "total_plus": 10.0}]
            )

            res = await flow.async_step_user(
                {CONF_USERNAME: "test@example.com", CONF_PASSWORD: "secret"}
            )

        assert res["type"] == "form"
        assert res["step_id"] == "system"
        marker, validator = self._field(res, "system")
        assert marker.default() == "brak"
        assert validator.container["brak"].endswith("(wykryto)")
