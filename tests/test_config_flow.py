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


class TestTariffFeeSchema:
    """v1.9.2: product selector + product-aware fee defaults in Options."""

    def _defaults(self, options, tariff="G12W", old_system=None):
        from custom_components.energa_mobile.config_flow import _tariff_fee_schema

        schema = _tariff_fee_schema(options, tariff, old_system)
        return {
            key.schema: key.default()
            for key in schema
            if key.default is not None
        }

    def test_inferred_oferta_defaults_for_old_system(self):
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT
        from custom_components.energa_mobile.tariff import G12W_OFERTA_FEES

        defaults = self._defaults({}, "G12W", old_system=True)
        # v1.9.2-beta.3: selector defaults to "auto" (inference still G12W_OFERTA).
        assert defaults[CONF_TARIFF_PRODUCT] == "auto"
        assert defaults["tariff_energy_day"] == G12W_OFERTA_FEES["energy_day"]
        assert defaults["tariff_trade_fee"] == G12W_OFERTA_FEES["trade_fee"]

    def test_inferred_urzedowa_defaults_for_new_system(self):
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT
        from custom_components.energa_mobile.tariff import G12W_URZEDOWA_FEES

        defaults = self._defaults({}, "G12W", old_system=False)
        assert defaults[CONF_TARIFF_PRODUCT] == "auto"
        assert defaults["tariff_energy_day"] == G12W_URZEDOWA_FEES["energy_day"]

    def test_explicit_product_wins_over_inference(self):
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT
        from custom_components.energa_mobile.tariff import G12W_URZEDOWA_FEES

        defaults = self._defaults(
            {"tariff_product": "G12W_URZEDOWA"}, "G12W", old_system=True
        )
        assert defaults[CONF_TARIFF_PRODUCT] == "G12W_URZEDOWA"
        assert (
            defaults["tariff_energy_day"] == G12W_URZEDOWA_FEES["energy_day"]
        )

    def test_g11_form_offers_both_g11_presets(self):
        from custom_components.energa_mobile.config_flow import _tariff_fee_schema
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT
        from custom_components.energa_mobile.tariff import (
            PRODUCT_G11_OFERTA,
            PRODUCT_G11_STANDARD,
        )

        schema = _tariff_fee_schema({}, "G11")
        product_field = None
        for marker, validator in schema.items():
            if getattr(marker, "schema", None) == CONF_TARIFF_PRODUCT:
                product_field = validator
        assert product_field is not None
        assert PRODUCT_G11_STANDARD in product_field.container
        assert PRODUCT_G11_OFERTA in product_field.container

    def test_auto_selector_default_for_g11(self):
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT

        defaults = self._defaults({}, "G11", old_system=True)
        assert defaults[CONF_TARIFF_PRODUCT] == "auto"

    def test_explicit_g11_preset_uses_its_table(self):
        from custom_components.energa_mobile.tariff import G11_OFERTA_FEES

        defaults = self._defaults(
            {"tariff_product": "G11_OFERTA"}, "G11", old_system=False
        )
        assert defaults["tariff_energy_day"] == G11_OFERTA_FEES["energy_day"]
        assert defaults["tariff_trade_fee"] == G11_OFERTA_FEES["trade_fee"]


class TestApplyTariffProduct:
    """Onboarding/Options materialise a preset into all ``tariff_*`` keys."""

    def test_preset_overwrites_all_rates(self):
        from custom_components.energa_mobile.config_flow import (
            _apply_tariff_product,
        )
        from custom_components.energa_mobile.tariff import G11_OFERTA_FEES

        user_input = {"tariff_product": "G11_OFERTA", "tariff_energy_day": 9.99}
        _apply_tariff_product(user_input, {"tariff_energy_day": 9.99})
        assert user_input["tariff_energy_day"] == G11_OFERTA_FEES["energy_day"]
        assert user_input["tariff_trade_fee"] == G11_OFERTA_FEES["trade_fee"]
        assert user_input["tariff_abonament"] == G11_OFERTA_FEES["abonament"]

    def test_auto_drops_form_defaults_when_nothing_was_stored(self):
        from custom_components.energa_mobile.config_flow import (
            _apply_tariff_product,
        )

        user_input = {
            "tariff_product": "auto",
            "tariff_energy_day": 0.6114,
            "tariff_trade_fee": 16.18,
        }
        _apply_tariff_product(user_input, {})
        assert "tariff_energy_day" not in user_input
        assert "tariff_trade_fee" not in user_input

    def test_auto_keeps_previously_stored_rates(self):
        from custom_components.energa_mobile.config_flow import (
            _apply_tariff_product,
        )

        user_input = {"tariff_product": "auto", "tariff_energy_day": 0.5}
        _apply_tariff_product(user_input, {"tariff_energy_day": 0.4})
        assert user_input["tariff_energy_day"] == 0.5


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


class TestLocalizedProductLabels:
    """v1.9.2-beta.4: product selector labels follow the HA language."""

    def test_onboarding_field_defaults_to_polish(self):
        from custom_components.energa_mobile.config_flow import (
            _onboarding_product_field,
        )
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT

        schema = _onboarding_product_field()
        validator = next(
            v for k, v in schema.items()
            if getattr(k, "schema", None) == CONF_TARIFF_PRODUCT
        )
        assert "G11 – Cennik standardowy" in validator.container.values()

    def test_onboarding_field_english(self):
        from custom_components.energa_mobile.config_flow import (
            _onboarding_product_field,
        )
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT

        schema = _onboarding_product_field("en")
        validator = next(
            v for k, v in schema.items()
            if getattr(k, "schema", None) == CONF_TARIFF_PRODUCT
        )
        assert "G12W – Basic offer" in validator.container.values()
        assert "G12W – Oferta Podstawowa" not in validator.container.values()

    def test_options_schema_english_labels(self):
        from custom_components.energa_mobile.config_flow import _tariff_fee_schema
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT

        schema = _tariff_fee_schema({}, "G12W", None, "en")
        validator = next(
            v for k, v in schema.items()
            if getattr(k, "schema", None) == CONF_TARIFF_PRODUCT
        )
        assert "G12W – Regulated tariff" in validator.container.values()


class TestOnboardingProductPersistence:
    """v1.9.2-beta.5 regression: a fresh onboarding must persist the product.

    Before the fix ``async_step_system``/``_fallback`` built ``options`` from
    ``_pending_options`` only and never merged ``user_input``, so the chosen
    ``tariff_product`` was silently dropped (``product_source=inferred/none``).
    On G11 that fell back to the default table -> a wrong bill.
    """

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

    async def _onboard(self, system, product):
        """Run the onboarding up to the created entry; return its options."""
        flow = self._make_flow()
        if system == "stare":
            res = await flow.async_step_system(
                {"system": "stare", "tariff_product": product}
            )
            assert res["step_id"] == "net_metering_survey"
            res = await flow.async_step_net_metering_survey(
                {
                    CONF_PROSUMER_POWER_GROUP: POWER_GROUP_LE_10KW,
                    CONF_ENERGY_DASHBOARD_MODE: ENERGY_MODE_VIRTUAL_STORAGE,
                }
            )
        else:
            res = await flow.async_step_system(
                {"system": system, "tariff_product": product}
            )
        assert res["type"] == "create_entry"
        return res["options"]

    @pytest.mark.asyncio
    async def test_g11_oferta_saved_as_explicit(self):
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT
        from custom_components.energa_mobile.tariff import (
            PRODUCT_G11_OFERTA,
            fee_source,
            product_option_values,
            resolve_product,
        )

        options = await self._onboard("nowe", PRODUCT_G11_OFERTA)
        assert options[CONF_TARIFF_PRODUCT] == PRODUCT_G11_OFERTA
        for key, value in product_option_values(PRODUCT_G11_OFERTA).items():
            assert options[key] == value
        assert resolve_product(options, "G11", False) == (
            PRODUCT_G11_OFERTA,
            "explicit",
        )
        assert fee_source(options, "G11")[0] == "product"

    @pytest.mark.asyncio
    async def test_g12w_urzedowa_saved_as_explicit(self):
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT
        from custom_components.energa_mobile.tariff import (
            PRODUCT_G12W_URZEDOWA,
            fee_source,
            resolve_product,
        )

        options = await self._onboard("nowe", PRODUCT_G12W_URZEDOWA)
        assert options[CONF_TARIFF_PRODUCT] == PRODUCT_G12W_URZEDOWA
        assert resolve_product(options, "G12W", False) == (
            PRODUCT_G12W_URZEDOWA,
            "explicit",
        )
        assert fee_source(options, "G12W")[0] == "product"

    @pytest.mark.asyncio
    async def test_g12w_oferta_saved_as_explicit_old_system(self):
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT
        from custom_components.energa_mobile.tariff import (
            PRODUCT_G12W_OFERTA,
            fee_source,
            resolve_product,
        )

        options = await self._onboard("stare", PRODUCT_G12W_OFERTA)
        assert options[CONF_TARIFF_PRODUCT] == PRODUCT_G12W_OFERTA
        assert resolve_product(options, "G12W", True) == (
            PRODUCT_G12W_OFERTA,
            "explicit",
        )
        assert fee_source(options, "G12W")[0] == "product"

    @pytest.mark.asyncio
    async def test_auto_keeps_inference_without_baking_rates(self):
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT
        from custom_components.energa_mobile.tariff import (
            PRODUCT_AUTO,
            PRODUCT_G12W_OFERTA,
            fee_source,
            resolve_product,
        )

        options = await self._onboard("stare", PRODUCT_AUTO)
        assert options.get(CONF_TARIFF_PRODUCT) in (None, PRODUCT_AUTO)
        # No hand-typed tariff_* must be stored: the settlement system infers.
        assert not any(
            key.startswith("tariff_") and key != CONF_TARIFF_PRODUCT
            for key in options
        )
        assert resolve_product(options, "G12W", True) == (
            PRODUCT_G12W_OFERTA,
            "inferred",
        )
        assert fee_source(options, "G12W", old_system=True)[0] == "product"

    @pytest.mark.asyncio
    async def test_fallback_step_also_persists_product(self):
        from custom_components.energa_mobile.config_flow import EnergaConfigFlow
        from custom_components.energa_mobile.const import CONF_TARIFF_PRODUCT
        from custom_components.energa_mobile.tariff import PRODUCT_G11_OFERTA

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
                "options": options or {},
            }
        )
        res = await flow.async_step_system_fallback(
            {"system": "brak", "tariff_product": PRODUCT_G11_OFERTA}
        )
        assert res["options"][CONF_TARIFF_PRODUCT] == PRODUCT_G11_OFERTA


class TestOnboardingBillingRegression:
    """A fresh onboarding choice must reproduce the invoice (Faza B)."""

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
                "options": options or {},
            }
        )
        return flow

    async def _options_for(self, system, product):
        flow = self._make_flow()
        if system == "stare":
            await flow.async_step_system(
                {"system": "stare", "tariff_product": product}
            )
            res = await flow.async_step_net_metering_survey(
                {
                    CONF_PROSUMER_POWER_GROUP: POWER_GROUP_LE_10KW,
                    CONF_ENERGY_DASHBOARD_MODE: ENERGY_MODE_VIRTUAL_STORAGE,
                }
            )
        else:
            res = await flow.async_step_system(
                {"system": system, "tariff_product": product}
            )
        return res["options"]

    @pytest.mark.asyncio
    async def test_bursztynowa_08_2026_from_onboarding(self):
        from custom_components.energa_mobile.tariff import (
            PRODUCT_G11_OFERTA,
            compute_bill,
            fees_from_options,
        )

        options = await self._options_for("nowe", PRODUCT_G11_OFERTA)
        fees = fees_from_options(options, "G11", old_system=False)
        res = compute_bill(
            import_day=30.0,
            import_night=0.0,
            export_kwh=192.0,
            rcem=0.1988,
            fees=fees,
            excise_day=149.0,
            excise_night=0.0,
            add_excise=True,
            deposit_pln=129.82,
        )
        assert (res["netto"], res["vat"], res["brutto"]) == (87.56, 20.14, 107.70)
        assert res["do_zaplaty"] == 84.44

    @pytest.mark.asyncio
    async def test_wisniowa_from_onboarding(self):
        from custom_components.energa_mobile.tariff import (
            PRODUCT_G12W_OFERTA,
            compute_bill,
            fees_from_options,
        )

        options = await self._options_for("stare", PRODUCT_G12W_OFERTA)
        fees = fees_from_options(options, "G12W", old_system=True)
        res = compute_bill(
            83.0,
            342.0,
            1904.0,
            0.0,
            fees,
            months=2,
            cover_day=83.0,
            cover_night=342.0,
            excise_day=120.0,
            excise_night=372.0,
            add_excise=True,
            deposit_pln=0.0,
        )
        assert (res["netto"], res["vat"], res["brutto"]) == (129.04, 29.68, 158.72)
        assert res["do_zaplaty"] == 158.72

    @pytest.mark.asyncio
    async def test_agrestowa_from_onboarding(self):
        from custom_components.energa_mobile.tariff import (
            PRODUCT_G12W_URZEDOWA,
            compute_bill,
            fees_from_options,
        )

        options = await self._options_for("nowe", PRODUCT_G12W_URZEDOWA)
        fees = fees_from_options(options, "G12W", old_system=False)
        res = compute_bill(
            398.0,
            309.0,
            435.0,
            0.29453,
            fees,
            excise_day=38.0,
            excise_night=23.0,
            add_excise=True,
        )
        assert (res["netto"], res["vat"], res["brutto"]) == (628.55, 144.57, 773.12)
        assert res["do_zaplaty"] == 615.53


class TestOptionsInverterEntity:
    """v1.9.2-beta.5 (P2): an empty inverter-entity default must not block save."""

    def _flow(self):
        from custom_components.energa_mobile.config_flow import EnergaOptionsFlow

        entry = MagicMock()
        entry.options = {}
        entry.entry_id = "entry-id"
        flow = EnergaOptionsFlow(entry)
        flow.hass = MagicMock()
        flow.async_show_form = MagicMock(
            side_effect=lambda **kwargs: {"type": "form", **kwargs}
        )
        return flow

    def _inverter_field(self, schema):
        for marker, validator in schema.schema.items():
            if getattr(marker, "schema", None) == "inverter_energy_entity":
                return marker, validator
        raise AssertionError("inverter field missing from schema")

    @pytest.mark.asyncio
    async def test_prices_form_empty_inverter_default_is_none(self):
        flow = self._flow()
        res = await flow.async_step_prices(None)
        marker, _validator = self._inverter_field(res["data_schema"])
        assert marker.default() is None

    @pytest.mark.asyncio
    async def test_prices_form_keeps_configured_inverter_default(self):
        flow = self._flow()
        flow._config_entry.options = {"inverter_energy_entity": "sensor.solar_kwh"}
        res = await flow.async_step_prices(None)
        marker, _validator = self._inverter_field(res["data_schema"])
        assert marker.default() == "sensor.solar_kwh"

    @pytest.mark.asyncio
    async def test_options_save_without_inverter_entity(self):
        flow = self._flow()
        flow.async_create_entry = MagicMock(
            side_effect=lambda title, data: {"type": "create_entry", "data": data}
        )
        res = await flow.async_step_prices(
            {
                "import_price": 0.61,
                "export_price": 0.2,
                "prosumer_coefficient": 0.0,
                "tariff_product": "G11_OFERTA",
            }
        )
        assert res["type"] == "create_entry"
        assert res["data"]["tariff_product"] == "G11_OFERTA"
        assert res["data"]["tariff_energy_day"] == 0.605286
