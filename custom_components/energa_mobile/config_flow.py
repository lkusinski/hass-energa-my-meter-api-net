"""Config flow for Energa My Meter integration."""

import asyncio
import logging
import secrets
from datetime import datetime

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EnergaAPI, EnergaAuthError, EnergaConnectionError
from .settlement import is_export_prosumer, system_choice_coefficient
from .const import (
    CONF_BALANCE_BASELINE_EXPORT,
    CONF_BALANCE_BASELINE_IMPORT,
    CONF_BANK_INITIAL_KWH,
    CONF_BANK_INITIAL_PLN,
    CONF_BANK_RCE_PRICE,
    CONF_DEVICE_TOKEN,
    CONF_ENABLE_AUTO_SETTLEMENT,
    CONF_EXPORT_PRICE,
    CONF_IMPORT_PRICE,
    CONF_IMPORT_PRICE_1,
    CONF_IMPORT_PRICE_2,
    CONF_PASSWORD,
    CONF_PROSUMER_COEFFICIENT,
    CONF_RCE_AUTO_FETCH,
    CONF_SETTLEMENT_DATE,
    CONF_TARIFF_ABONAMENT,
    CONF_TARIFF_CAPACITY,
    CONF_TARIFF_COGEN,
    CONF_TARIFF_ENERGY_DAY,
    CONF_TARIFF_ENERGY_NIGHT,
    CONF_TARIFF_EXCISE_MWH,
    CONF_TARIFF_GRID_FIXED,
    CONF_TARIFF_GRID_VAR_DAY,
    CONF_TARIFF_GRID_VAR_NIGHT,
    CONF_TARIFF_OZE,
    CONF_TARIFF_QUALITY,
    CONF_TARIFF_TRADE_FEE,
    CONF_USERNAME,
    CONF_USE_ROLLING_365D,
    DEFAULT_BALANCE_BASELINE,
    DEFAULT_BANK_INITIAL_KWH,
    DEFAULT_BANK_INITIAL_PLN,
    DEFAULT_BANK_RCE_PRICE,
    DEFAULT_ENABLE_AUTO_SETTLEMENT,
    DEFAULT_EXPORT_PRICE,
    DEFAULT_IMPORT_PRICE,
    DEFAULT_IMPORT_PRICE_1,
    DEFAULT_IMPORT_PRICE_2,
    DEFAULT_PROSUMER_COEFFICIENT,
    DEFAULT_RCE_AUTO_FETCH,
    DEFAULT_SETTLEMENT_DATE,
    DEFAULT_USE_ROLLING_365D,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _tariff_fee_schema(options: dict, tariff: str | None = None) -> dict:
    """Optional tariff fee overrides for the full-bill forecast (v0.2.14).

    Shared by the G12W and G11 price forms. Defaults follow the meter
    tariff (v0.3.0: G11 has its own invoice-verified table); an
    empty/unchanged field keeps the default via fees_from_options.
    """
    from .tariff import FEE_TABLES, tariff_family

    table = FEE_TABLES.get(tariff_family(tariff))
    key_map = {
        CONF_TARIFF_ENERGY_DAY: "energy_day",
        CONF_TARIFF_ENERGY_NIGHT: "energy_night",
        CONF_TARIFF_EXCISE_MWH: "excise_mwh",
        CONF_TARIFF_TRADE_FEE: "trade_fee",
        CONF_TARIFF_ABONAMENT: "abonament",
        CONF_TARIFF_GRID_FIXED: "grid_fixed",
        CONF_TARIFF_GRID_VAR_DAY: "grid_var_day",
        CONF_TARIFF_GRID_VAR_NIGHT: "grid_var_night",
        CONF_TARIFF_QUALITY: "quality",
        CONF_TARIFF_OZE: "oze",
        CONF_TARIFF_COGEN: "cogen",
        CONF_TARIFF_CAPACITY: "capacity",
    }
    return {
        vol.Optional(key, default=options.get(key, table[fee])): vol.Coerce(float)
        for key, fee in key_map.items()
    }


class EnergaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for Energa My Meter."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get options flow handler."""
        return EnergaOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        """Handle initial user setup.

        v0.3.0: fast, non-blocking. Login → create entry immediately.
        The last-730-day history backfills itself in the background
        right after setup (notification "Energa: Import Historii");
        no hierarchical detection freezes the UI anymore.
        """
        errors = {}
        if user_input is not None:
            original_username = user_input[CONF_USERNAME].strip()
            normalized_username = original_username.lower()
            session = async_get_clientsession(self.hass)
            device_token = secrets.token_hex(32)
            # Try original first, fallback to lowercase on invalid_auth
            for attempt_username in [original_username, normalized_username] if original_username != normalized_username else [original_username]:
                api = EnergaAPI(
                    attempt_username,
                    user_input[CONF_PASSWORD],
                    device_token,
                    session,
                )
                try:
                    await api.async_login()
                    # Blind 730-day window: the Energa API holds ~2 years.
                    # The backfill task (see __init__.py) imports it in
                    # the background; the First-Data sensor shows this
                    # window start until real statistics land.
                    from datetime import timedelta

                    from homeassistant.util import dt as dt_util

                    window_start = (
                        dt_util.now() - timedelta(days=730)
                    ).date().isoformat()
                    entry_data = {
                        **user_input,
                        CONF_USERNAME: attempt_username,
                        CONF_DEVICE_TOKEN: device_token,
                        "auto_history_start": window_start,
                    }
                    await self.async_set_unique_id(attempt_username.lower())
                    self._abort_if_unique_id_configured()
                    # v0.3.8: prosumers pick the settlement system up
                    # front — no API field tells opusty apart from
                    # net-billing (activation date is the app date,
                    # dealer.start the supply contract). One bounded
                    # meter fetch; fail-open to the old direct create.
                    try:
                        async with asyncio.timeout(20):
                            _meters = await api._fetch_all_meters()
                        _prosumer = any(
                            is_export_prosumer(m) for m in (_meters or [])
                        )
                    except Exception:
                        _prosumer = False
                    if _prosumer:
                        self._pending_title = attempt_username
                        self._pending_data = entry_data
                        return await self.async_step_system()
                    return self.async_create_entry(
                        title=attempt_username,
                        data=entry_data,
                    )
                except EnergaAuthError:
                    if attempt_username == normalized_username:
                        errors["base"] = "invalid_auth"
                    else:
                        continue  # Try normalized
                except (EnergaConnectionError, aiohttp.ClientError, TimeoutError):
                    errors["base"] = "cannot_connect"
                    break
                except AbortFlow:
                    raise
                except Exception:
                    _LOGGER.exception("Unexpected error during setup")
                    errors["base"] = "unknown"
                    break

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_system(self, user_input=None):
        """Ask the settlement system for prosumer accounts (v0.3.8).

        Opusty (old) vs net-billing (new) cannot be told apart from API
        data, so the user picks once; the choice seeds the entry options
        (still changeable later in Options → Ceny).
        """
        if user_input is not None:
            coeff = system_choice_coefficient(user_input.get("system"))
            return self.async_create_entry(
                title=getattr(self, "_pending_title", "Energa My Meter"),
                data=getattr(self, "_pending_data", {}),
                options={CONF_PROSUMER_COEFFICIENT: coeff},
            )
        return self.async_show_form(
            step_id="system",
            data_schema=vol.Schema(
                {
                    vol.Required("system", default="nowe"): vol.In(
                        {
                            "nowe": "Nowe zasady (net-billing, rozliczenie miesięczne w PLN)",
                            "stare": "Stare zasady (net-metering, magazyn kWh 0.8, instalacje do 03.2022)",
                        }
                    )
                }
            ),
        )

    async def async_step_reauth(self, entry_data):
        """Handle reauth when credentials expire."""
        self.reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Handle reauth confirmation."""
        errors = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            username = self.reauth_entry.data[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            # Preserve existing device token or generate new one
            device_token = self.reauth_entry.data.get(
                CONF_DEVICE_TOKEN
            ) or secrets.token_hex(32)
            api = EnergaAPI(username, password, device_token, session)
            try:
                await api.async_login()
                self.hass.config_entries.async_update_entry(
                    self.reauth_entry,
                    data={
                        **dict(self.reauth_entry.data),
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        CONF_DEVICE_TOKEN: device_token,
                    },
                )
                await self.hass.config_entries.async_reload(self.reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")
            except EnergaAuthError:
                errors["base"] = "invalid_auth"
            except (EnergaConnectionError, aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except AbortFlow:
                raise
            except Exception:
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={
                "username": self.reauth_entry.data[CONF_USERNAME]
            },
            errors=errors,
        )


class EnergaOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Energa My Meter."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Show options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["credentials", "prices", "history", "clear_stats"],
        )

    async def async_step_credentials(self, user_input=None):
        """Handle credential update."""
        errors = {}
        if user_input is not None:
            original_username = user_input[CONF_USERNAME].strip()
            normalized_username = original_username.lower()
            session = async_get_clientsession(self.hass)
            device_token = self._config_entry.data.get(
                CONF_DEVICE_TOKEN
            ) or secrets.token_hex(32)
            for attempt_username in [original_username, normalized_username] if original_username != normalized_username else [original_username]:
                api = EnergaAPI(
                    attempt_username,
                    user_input[CONF_PASSWORD],
                    device_token,
                    session,
                )
                try:
                    await api.async_login()
                    user_input[CONF_USERNAME] = attempt_username
                    entry_data = {
                        **dict(self._config_entry.data),
                        **user_input,
                        CONF_DEVICE_TOKEN: device_token,
                    }
                    self.hass.config_entries.async_update_entry(
                        self._config_entry,
                        data=entry_data,
                    )
                    await self.hass.config_entries.async_reload(self._config_entry.entry_id)
                    return self.async_create_entry(title="", data=dict(self._config_entry.options))
                except EnergaAuthError:
                    if attempt_username == normalized_username:
                        errors["base"] = "invalid_auth"
                    else:
                        continue
                except (EnergaConnectionError, aiohttp.ClientError, TimeoutError):
                    errors["base"] = "cannot_connect"
                    break
                except AbortFlow:
                    raise
                except Exception:
                    _LOGGER.exception("Unexpected error during credential update")
                    errors["base"] = "unknown"
                    break

        current_user = self._config_entry.data.get(CONF_USERNAME)
        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=current_user): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    def _has_multi_zone_meters(self) -> bool:
        """Check if any meter uses multi-zone tariff (G12w).

        Checks in priority order:
        1. Persistent hint stored in options from previous session
        2. Zone-specific price keys already saved in options (fix for issue #34:
           API may not be loaded yet when entering options after restart)
        3. Live API data (if available)
        """
        # 1. Persistent hint saved when prices were last configured
        if self._config_entry.options.get("has_multi_zone"):
            return True

        # 2. Zone-specific price already saved → must be G12w
        if self._config_entry.options.get(CONF_IMPORT_PRICE_1) is not None:
            return True

        # 3. Live API data
        entry_data = self.hass.data.get(DOMAIN, {}).get(
            self._config_entry.entry_id, {}
        )
        api = entry_data.get("api") if isinstance(entry_data, dict) else None
        if api and hasattr(api, "has_multi_zone_meters"):
            return api.has_multi_zone_meters()

        return False

    def _get_active_meters(self) -> list:
        """Get list of active meters from API."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(
            self._config_entry.entry_id, {}
        )
        api = entry_data.get("api") if isinstance(entry_data, dict) else None
        if api and api._meters_data:
            return [
                m for m in api._meters_data
                if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
            ]
        return []

    def _dominant_tariff(self) -> str:
        """Fee-table tariff for the prices form (v0.3.0).

        G11 only when every active meter is single-zone G11 (its own
        invoice table); otherwise G12W. Mixed accounts keep G12W and
        fine-tune per-field.
        """
        meters = self._get_active_meters()
        if not meters:
            return "G12W"
        from .tariff import tariff_family

        families = {tariff_family(m.get("tariff")) for m in meters}
        if families == {"G11"}:
            return "G11"
        return "G12W"

    async def async_step_prices(self, user_input=None):
        """Handle energy price configuration."""
        if user_input is not None:
            # Save global prices
            new_options = {**self._config_entry.options, **user_input}

            # Also save per-meter prices for each active meter
            meters = self._get_active_meters()
            for meter in meters:
                serial = meter.get("meter_serial", meter["meter_point_id"])
                for key, val in user_input.items():
                    meter_key = f"meter_{serial}_{key}"
                    new_options[meter_key] = val

            # Persist multi-zone hint so options form shows correct fields
            # even if API is not loaded on next entry (fix for issue #34)
            has_zones_now = any(
                m.get("zone_count", 1) > 1 for m in meters
            ) if meters else False
            if has_zones_now or CONF_IMPORT_PRICE_1 in user_input:
                new_options["has_multi_zone"] = True

            _LOGGER.debug("Saving options with %d keys: %s", len(new_options), list(new_options.keys()))
            return self.async_create_entry(title="", data=new_options)

        has_zones = self._has_multi_zone_meters()

        # Get current values from options
        current_export = self._config_entry.options.get(CONF_EXPORT_PRICE, DEFAULT_EXPORT_PRICE)
        current_coeff = self._config_entry.options.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT)
        current_bl_import = self._config_entry.options.get(CONF_BALANCE_BASELINE_IMPORT, DEFAULT_BALANCE_BASELINE)
        current_bl_export = self._config_entry.options.get(CONF_BALANCE_BASELINE_EXPORT, DEFAULT_BALANCE_BASELINE)

        if has_zones:
            # G12w: show zone-specific prices
            current_price_1 = self._config_entry.options.get(CONF_IMPORT_PRICE_1, DEFAULT_IMPORT_PRICE_1)
            current_price_2 = self._config_entry.options.get(CONF_IMPORT_PRICE_2, DEFAULT_IMPORT_PRICE_2)
            current_rce = self._config_entry.options.get(CONF_BANK_RCE_PRICE, DEFAULT_BANK_RCE_PRICE)
            current_initial_kwh = self._config_entry.options.get(CONF_BANK_INITIAL_KWH, DEFAULT_BANK_INITIAL_KWH)
            current_initial_pln = self._config_entry.options.get(CONF_BANK_INITIAL_PLN, DEFAULT_BANK_INITIAL_PLN)
            current_rce_auto = self._config_entry.options.get(CONF_RCE_AUTO_FETCH, DEFAULT_RCE_AUTO_FETCH)
            current_settlement = self._config_entry.options.get(CONF_SETTLEMENT_DATE, DEFAULT_SETTLEMENT_DATE)
            current_auto_settle = self._config_entry.options.get(CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT)
            current_rolling = self._config_entry.options.get(CONF_USE_ROLLING_365D, DEFAULT_USE_ROLLING_365D)

            return self.async_show_form(
                step_id="prices",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_IMPORT_PRICE_1, default=current_price_1
                        ): vol.Coerce(float),
                        vol.Required(
                            CONF_IMPORT_PRICE_2, default=current_price_2
                        ): vol.Coerce(float),
                        vol.Required(
                            CONF_EXPORT_PRICE, default=current_export
                        ): vol.Coerce(float),
                        vol.Required(
                            CONF_PROSUMER_COEFFICIENT, default=current_coeff
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BALANCE_BASELINE_IMPORT, default=current_bl_import
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BALANCE_BASELINE_EXPORT, default=current_bl_export
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BANK_RCE_PRICE, default=current_rce
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BANK_INITIAL_KWH, default=current_initial_kwh
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BANK_INITIAL_PLN, default=current_initial_pln
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_RCE_AUTO_FETCH, default=current_rce_auto
                        ): bool,
                        vol.Optional(
                            CONF_SETTLEMENT_DATE, default=current_settlement
                        ): str,
                        vol.Optional(
                            CONF_ENABLE_AUTO_SETTLEMENT, default=current_auto_settle
                        ): bool,
                        vol.Optional(
                            CONF_USE_ROLLING_365D, default=current_rolling
                        ): bool,
                        **_tariff_fee_schema(self._config_entry.options, self._dominant_tariff()),
                    }
                ),
            )
        else:
            # Single-zone: show single import price
            current_import = self._config_entry.options.get(CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE)
            current_rce = self._config_entry.options.get(CONF_BANK_RCE_PRICE, DEFAULT_BANK_RCE_PRICE)
            current_initial_kwh = self._config_entry.options.get(CONF_BANK_INITIAL_KWH, DEFAULT_BANK_INITIAL_KWH)
            current_initial_pln = self._config_entry.options.get(CONF_BANK_INITIAL_PLN, DEFAULT_BANK_INITIAL_PLN)
            current_rce_auto = self._config_entry.options.get(CONF_RCE_AUTO_FETCH, DEFAULT_RCE_AUTO_FETCH)
            current_settlement = self._config_entry.options.get(CONF_SETTLEMENT_DATE, DEFAULT_SETTLEMENT_DATE)
            current_auto_settle = self._config_entry.options.get(CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT)
            current_rolling = self._config_entry.options.get(CONF_USE_ROLLING_365D, DEFAULT_USE_ROLLING_365D)

            return self.async_show_form(
                step_id="prices",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_IMPORT_PRICE, default=current_import
                        ): vol.Coerce(float),
                        vol.Required(
                            CONF_EXPORT_PRICE, default=current_export
                        ): vol.Coerce(float),
                        vol.Required(
                            CONF_PROSUMER_COEFFICIENT, default=current_coeff
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BALANCE_BASELINE_IMPORT, default=current_bl_import
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BALANCE_BASELINE_EXPORT, default=current_bl_export
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BANK_RCE_PRICE, default=current_rce
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BANK_INITIAL_KWH, default=current_initial_kwh
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BANK_INITIAL_PLN, default=current_initial_pln
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_RCE_AUTO_FETCH, default=current_rce_auto
                        ): bool,
                        vol.Optional(
                            CONF_SETTLEMENT_DATE, default=current_settlement
                        ): str,
                        vol.Optional(
                            CONF_ENABLE_AUTO_SETTLEMENT, default=current_auto_settle
                        ): bool,
                        vol.Optional(
                            CONF_USE_ROLLING_365D, default=current_rolling
                        ): bool,
                        **_tariff_fee_schema(self._config_entry.options, self._dominant_tariff()),
                    }
                ),
            )

    async def async_step_history(self, user_input=None):
        """Handle history import from options."""
        from . import _import_meter_history

        entry_data = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id, {})
        api = entry_data.get("api") if isinstance(entry_data, dict) else entry_data
        if not api:
            return self.async_abort(reason="integration_not_ready")

        # Get contract date hint
        contract_str = "Nieznana"
        default_date = None
        if api._meters_data:
            first_meter = api._meters_data[0]
            if first_meter.get("contract_date"):
                contract_str = str(first_meter["contract_date"])
                default_date = str(first_meter["contract_date"])

        if user_input is not None:
            start_date = datetime.strptime(user_input["start_date"], "%Y-%m-%d")
            days = (datetime.now() - start_date).days
            if days < 1:
                days = 1

            # Get active meters - handle token expiry
            try:
                meters = await api.async_get_data()
            except Exception as err:
                # Token expired or other API error - try to re-login
                from .api import EnergaAuthError, EnergaTokenExpiredError

                if isinstance(err, (EnergaTokenExpiredError, EnergaAuthError)):
                    try:
                        await api.async_login()
                        meters = await api.async_get_data()
                    except Exception as login_err:
                        return self.async_abort(
                            reason="cannot_connect",
                            description_placeholders={"error": str(login_err)},
                        )
                else:
                    return self.async_abort(
                        reason="cannot_connect",
                        description_placeholders={"error": str(err)},
                    )

            active_meters = [
                m
                for m in meters
                if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
            ]

            # FIX: Pass full meter dict, not just ID
            for meter in active_meters:
                self.hass.async_create_task(
                    _import_meter_history(
                        self.hass, api, meter, start_date, days, self._config_entry
                    )
                )
            return self.async_create_entry(title="", data=dict(self._config_entry.options))

        return self.async_show_form(
            step_id="history",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "start_date", default=default_date
                    ): selector.DateSelector(),
                }
            ),
            description_placeholders={"contract_date": contract_str},
        )

    async def async_step_clear_stats(self, user_input=None):
        """Clear Energy Panel statistics for Energa sensors.

        This removes all historical statistics from Home Assistant's recorder
        for Energa energy/production sensors. Use this if:
        - Statistics show incorrect spikes or anomalies
        - After updating the integration to fix data format issues

        Note: After clearing, use 'Pobierz Historię' to reimport clean data.
        """
        from homeassistant.components import recorder
        from homeassistant.helpers import entity_registry as er

        if user_input is not None:
            rec = recorder.get_instance(self.hass)
            entity_registry = er.async_get(self.hass)

            # Find all Energa Panel Energia sensors (energy statistics only)
            # Matched by entity_id substrings: panel_energia_zuzycie, panel_energia_produkcja, panel_energia_strefa
            statistic_ids = [
                entity.entity_id
                for entity in entity_registry.entities.values()
                if entity.platform == DOMAIN
                and (
                    "panel_energia_zuzycie" in entity.entity_id
                    or "panel_energia_produkcja" in entity.entity_id
                    or "panel_energia_strefa" in entity.entity_id
                )
            ]

            if statistic_ids:
                cost_statistic_ids = [f"{sid}_cost" for sid in statistic_ids]
                all_statistic_ids = statistic_ids + cost_statistic_ids

                # HA 2026.4+ removed async_clear_statistics from Recorder
                if hasattr(rec, "async_clear_statistics"):
                    rec.async_clear_statistics(all_statistic_ids)
                    _LOGGER.info(
                        "Cleared Energy Panel statistics for %d Energa sensors: %s",
                        len(statistic_ids),
                        all_statistic_ids,
                    )
                else:
                    _LOGGER.warning(
                        "async_clear_statistics not available (HA 2026.4+). "
                        "Use Developer Tools → Statistics to clear manually: %s",
                        all_statistic_ids,
                    )
                    from homeassistant.components import persistent_notification
                    persistent_notification.async_create(
                        self.hass,
                        "Funkcja czyszczenia statystyk nie jest dostępna w tej wersji Home Assistant.\n\n"
                        "Użyj **Narzędzia deweloperskie → Statystyki** aby ręcznie wyczyścić:\n"
                        + "\n".join(f"- `{sid}`" for sid in all_statistic_ids),
                        title="Energa: Czyszczenie niedostępne",
                        notification_id="energa_clear_stats_unavailable",
                    )
            else:
                _LOGGER.warning("No Energa Panel Energia sensors found to clear")

            return self.async_create_entry(title="", data=dict(self._config_entry.options))

        return self.async_show_form(
            step_id="clear_stats",
            description_placeholders={
                "warning": "⚠️ To **nieodwracalnie wyczyści** wszystkie statystyki energii i kosztów dla Panelu Energia.\n\nPo wyczyszczeniu użyj 'Pobierz Historię' aby ponownie zaimportować dane."
            },
        )
