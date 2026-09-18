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
from .const import (
    CONF_BALANCE_BASELINE_EXPORT,
    CONF_BALANCE_BASELINE_IMPORT,
    CONF_BANK_INITIAL_KWH,
    CONF_BANK_INITIAL_KWH_L1,
    CONF_BANK_INITIAL_KWH_L2,
    CONF_BANK_INITIAL_PLN,
    CONF_BANK_RCE_PRICE,
    CONF_CREATE_SETTLEMENT_DASHBOARD,
    CONF_DEVICE_TOKEN,
    CONF_ENABLE_AUTO_SETTLEMENT,
    CONF_ENABLE_SYNTHETIC_STORAGE,
    CONF_ENERGY_DASHBOARD_MODE,
    CONF_EXPORT_PRICE,
    CONF_IMPORT_PRICE,
    CONF_IMPORT_PRICE_1,
    CONF_IMPORT_PRICE_2,
    CONF_INVERTER_ENERGY_ENTITY,
    CONF_PASSWORD,
    CONF_PROSUMER_COEFFICIENT,
    CONF_PROSUMER_POWER_GROUP,
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
    CONF_TARIFF_PRODUCT,
    CONF_TARIFF_QUALITY,
    CONF_TARIFF_TRADE_FEE,
    CONF_USE_ROLLING_365D,
    CONF_USERNAME,
    DEFAULT_BALANCE_BASELINE,
    DEFAULT_BANK_INITIAL_KWH,
    DEFAULT_BANK_INITIAL_KWH_L1,
    DEFAULT_BANK_INITIAL_KWH_L2,
    DEFAULT_BANK_INITIAL_PLN,
    DEFAULT_BANK_RCE_PRICE,
    DEFAULT_CREATE_SETTLEMENT_DASHBOARD,
    DEFAULT_ENABLE_AUTO_SETTLEMENT,
    DEFAULT_ENABLE_SYNTHETIC_STORAGE,
    DEFAULT_ENERGY_DASHBOARD_MODE,
    DEFAULT_EXPORT_PRICE,
    DEFAULT_IMPORT_PRICE,
    DEFAULT_IMPORT_PRICE_1,
    DEFAULT_IMPORT_PRICE_2,
    DEFAULT_INVERTER_ENERGY_ENTITY,
    DEFAULT_PROSUMER_COEFFICIENT,
    DEFAULT_PROSUMER_POWER_GROUP,
    DEFAULT_RCE_AUTO_FETCH,
    DEFAULT_SETTLEMENT_DATE,
    DEFAULT_USE_ROLLING_365D,
    DOMAIN,
    ENERGY_MODE_PHYSICAL_GRID,
    ENERGY_MODE_VIRTUAL_STORAGE,
    POWER_GROUP_GT_10KW,
    POWER_GROUP_LE_10KW,
)
from .settlement import is_export_prosumer, system_choice_coefficient

_LOGGER = logging.getLogger(__name__)

# Settlement-system option labels shown in the onboarding wizard. ``vol.In``
# renders literal labels, so they live here, with the matching PL/EN texts
# mirrored in strings.json and translations for translators.
_SYSTEM_CHOICE_LABELS: dict[str, dict[str, str]] = {
    "pl": {
        "nowe": "Nowe zasady (net-billing, rozliczenie miesięczne w PLN)",
        "stare": "Stare zasady (net-metering, magazyn kWh 0.8, instalacje do 03.2022)",
        "brak": "Tylko konsument — brak instalacji PV",
        "brak_detected": "Tylko konsument — brak instalacji PV (wykryto)",
    },
    "en": {
        "nowe": "New rules (net-billing, monthly settlement in PLN)",
        "stare": "Old rules (net-metering, 0.8 kWh storage, installations before 03.2022)",
        "brak": "Consumer only — no PV installation",
        "brak_detected": "Consumer only — no PV (detected)",
    },
}


def _wizard_language(hass) -> str:
    """Return ``pl`` unless Home Assistant is explicitly configured for English."""
    try:
        language = getattr(getattr(hass, "config", None), "language", None)
    except Exception:  # noqa: BLE001 - a locale must never break the wizard
        language = None
    if isinstance(language, str) and language.lower().startswith("en"):
        return "en"
    return "pl"


_TARIFF_KEY_MAP = {
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


def _tariff_fee_schema(
    options: dict, tariff: str | None = None, old_system: bool | None = None
) -> dict:
    """Tariff product + fee overrides for the full-bill forecast.

    Shared by the G12W and G11 price forms. Defaults follow the selected
    product (``tariff_product``) or, if none is stored, the product inferred
    from the settlement system (v1.9.2), else the meter tariff table (v0.3.0:
    G11 has its own invoice-verified table). An empty/unchanged field keeps the
    default via fees_from_options.
    """
    from .tariff import (
        FEE_TABLES,
        PRODUCT_FEE_TABLES,
        PRODUCT_LABELS,
        normalized_product,
        product_for_system,
        tariff_family,
    )

    opts = options or {}
    family = tariff_family(tariff)
    explicit = normalized_product(opts.get(CONF_TARIFF_PRODUCT))
    inferred = product_for_system(old_system) if family == "G12W" else None
    current_product = explicit or inferred or ""
    product_table = PRODUCT_FEE_TABLES.get(explicit or inferred or "")
    table = product_table or FEE_TABLES.get(family)
    schema: dict = {}
    if family == "G12W":
        choices = {"": "Automatycznie / własne stawki"}
        choices.update(PRODUCT_LABELS)
        schema[
            vol.Optional(CONF_TARIFF_PRODUCT, default=current_product)
        ] = vol.In(choices)
    schema.update(
        {
            vol.Optional(key, default=opts.get(key, table[fee])): vol.Coerce(float)
            for key, fee in _TARIFF_KEY_MAP.items()
        }
    )
    return schema


class EnergaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for Energa My Meter."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get options flow handler."""
        return EnergaOptionsFlow(config_entry)

    async def _async_scan_ergo5(self) -> list:
        """Return detected copies of the base ergo5 integration; never raises."""
        try:
            from .settlement import scan_for_ergo5

            return await self.hass.async_add_executor_job(
                scan_for_ergo5, self.hass.config.path("custom_components")
            )
        except Exception:  # noqa: BLE001 - detection must never break the flow
            _LOGGER.debug("ergo5 scan during config flow failed", exc_info=True)
            return []

    def _system_choice_labels(self, detected_consumer: bool) -> dict:
        """Return localised settlement-system option labels for the wizard."""
        labels = _SYSTEM_CHOICE_LABELS[_wizard_language(self.hass)]
        return {
            "nowe": labels["nowe"],
            "stare": labels["stare"],
            "brak": labels["brak_detected"] if detected_consumer else labels["brak"],
        }

    async def _async_energy_solar_present(self) -> bool:
        """Detect a PV source in the native Energy panel; never raises."""
        try:
            from .core.energy_sources import async_has_energy_solar

            return await async_has_energy_solar(self.hass)
        except Exception:  # noqa: BLE001 - detection must never break onboarding
            _LOGGER.debug(
                "Energy panel PV detection failed during onboarding", exc_info=True
            )
            return False

    async def _async_create_entry_with_ergo5_check(self, title, data, options):
        """Create the entry, unless a a copy of the base ergo5 integration must be acknowledged."""
        if not getattr(self, "_ergo5_acknowledged", False):
            hits = await self._async_scan_ergo5()
            if hits:
                self._pending_title = title
                self._pending_data = data
                self._pending_options = options
                self._ergo5_paths = "\n".join(f"- {hit.get('path')}" for hit in hits)
                return await self.async_step_ergo5_warning()
        return self.async_create_entry(title=title, data=data, options=options)

    async def async_step_ergo5_warning(self, user_input=None):
        """Warn about a a copy of the base ergo5 integration before creating the entry.

        The user must tick the acknowledgement; otherwise the form is shown
        again with an error and no entry is created.
        """
        errors = {}
        if user_input is not None:
            if user_input.get("acknowledge"):
                self._ergo5_acknowledged = True
                return self.async_create_entry(
                    title=getattr(self, "_pending_title", "Energa My Meter"),
                    data=getattr(self, "_pending_data", {}),
                    options=getattr(self, "_pending_options", {}),
                )
            errors["base"] = "ergo5_not_acknowledged"
        return self.async_show_form(
            step_id="ergo5_warning",
            data_schema=vol.Schema({vol.Required("acknowledge", default=False): bool}),
            description_placeholders={"paths": getattr(self, "_ergo5_paths", "")},
            errors=errors,
        )

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
                    _prosumer = False
                    _fetch_failed = False
                    try:
                        async with asyncio.timeout(20):
                            _meters = await api._fetch_all_meters()
                        _prosumer = any(
                            is_export_prosumer(m) for m in (_meters or [])
                        )
                    except Exception as ex:
                        _LOGGER.warning(
                            "Could not auto-detect prosumer status from Energa API (%s), presenting fallback step",
                            ex,
                        )
                        _fetch_failed = True

                    if _prosumer:
                        self._pending_title = attempt_username
                        self._pending_data = entry_data
                        return await self.async_step_system()
                    if _fetch_failed:
                        self._pending_title = attempt_username
                        self._pending_data = entry_data
                        return await self.async_step_system_fallback()

                    # Confirmed NON-prosumer (no export): still show the
                    # settlement-system step, with "consumer only" pre-selected
                    # and marked as detected so the user sees the justification.
                    # Seeding the pending options with coefficient 0.0 keeps
                    # _is_old_system() from mislabelling a plain consumer as old
                    # net-metering (Warzywna G11) when the default is accepted.
                    self._pending_title = attempt_username
                    self._pending_data = entry_data
                    self._detected_consumer = True
                    self._pending_options = {CONF_PROSUMER_COEFFICIENT: 0.0}
                    return await self.async_step_system()
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
            choice = user_input.get("system")
            # Seed from any pre-selected options (a detected one-way consumer
            # arrives with prosumer_coefficient pinned to 0.0).
            options = dict(getattr(self, "_pending_options", {}) or {})
            options[CONF_CREATE_SETTLEMENT_DASHBOARD] = bool(
                user_input.get(
                    CONF_CREATE_SETTLEMENT_DASHBOARD,
                    DEFAULT_CREATE_SETTLEMENT_DASHBOARD,
                )
            )
            if choice == "stare":
                self._pending_options = options
                return await self.async_step_net_metering_survey()
            if choice == "nowe":
                options[CONF_PROSUMER_COEFFICIENT] = 0.0
                options[CONF_ENABLE_SYNTHETIC_STORAGE] = False
                options[CONF_ENERGY_DASHBOARD_MODE] = ENERGY_MODE_PHYSICAL_GRID
                options[CONF_RCE_AUTO_FETCH] = True
            elif choice == "brak":
                options[CONF_ENABLE_SYNTHETIC_STORAGE] = False
                options[CONF_ENERGY_DASHBOARD_MODE] = ENERGY_MODE_PHYSICAL_GRID
            else:
                options[CONF_PROSUMER_COEFFICIENT] = system_choice_coefficient(choice)
            return await self._async_create_entry_with_ergo5_check(
                getattr(self, "_pending_title", "Energa My Meter"),
                getattr(self, "_pending_data", {}),
                options,
            )

        detected_consumer = bool(getattr(self, "_detected_consumer", False))
        schema = {
            vol.Required(
                "system", default="brak" if detected_consumer else "nowe"
            ): vol.In(self._system_choice_labels(detected_consumer)),
            vol.Optional(
                CONF_CREATE_SETTLEMENT_DASHBOARD,
                default=DEFAULT_CREATE_SETTLEMENT_DASHBOARD,
            ): bool,
        }
        # A two-way meter with no PV source yet in the Energy panel: nudge the
        # user to add one. Detection runs once per form render and never blocks
        # continuing (informational, optional field only).
        if not detected_consumer and not await self._async_energy_solar_present():
            schema[vol.Optional("energy_dashboard_pv_hint", default=False)] = bool
        return self.async_show_form(
            step_id="system",
            data_schema=vol.Schema(schema),
        )

    async def async_step_system_fallback(self, user_input=None):
        """Prompt user for prosumer system when auto-detection timed out."""
        if user_input is not None:
            choice = user_input.get("system")
            options = {}
            options[CONF_CREATE_SETTLEMENT_DASHBOARD] = bool(
                user_input.get(
                    CONF_CREATE_SETTLEMENT_DASHBOARD,
                    DEFAULT_CREATE_SETTLEMENT_DASHBOARD,
                )
            )
            if choice == "stare":
                self._pending_options = options
                return await self.async_step_net_metering_survey()
            if choice == "nowe":
                options[CONF_PROSUMER_COEFFICIENT] = 0.0
                options[CONF_ENABLE_SYNTHETIC_STORAGE] = False
                options[CONF_ENERGY_DASHBOARD_MODE] = ENERGY_MODE_PHYSICAL_GRID
                options[CONF_RCE_AUTO_FETCH] = True
            elif choice == "brak":
                options[CONF_ENABLE_SYNTHETIC_STORAGE] = False
                options[CONF_ENERGY_DASHBOARD_MODE] = ENERGY_MODE_PHYSICAL_GRID
            else:
                options[CONF_PROSUMER_COEFFICIENT] = system_choice_coefficient(choice)
            return await self._async_create_entry_with_ergo5_check(
                getattr(self, "_pending_title", "Energa My Meter"),
                getattr(self, "_pending_data", {}),
                options,
            )
        return self.async_show_form(
            step_id="system_fallback",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "system", default="nowe"
                    ): vol.In(self._system_choice_labels(False)),
                    vol.Optional(
                        CONF_CREATE_SETTLEMENT_DASHBOARD,
                        default=DEFAULT_CREATE_SETTLEMENT_DASHBOARD,
                    ): bool,
                }
            ),
        )

    async def async_step_net_metering_survey(self, user_input=None):
        """Survey and explanation for Net-metering Energy Dashboard mode."""
        if user_input is not None:
            power_group = user_input.get(
                CONF_PROSUMER_POWER_GROUP, DEFAULT_PROSUMER_POWER_GROUP
            )
            dashboard_mode = user_input.get(
                CONF_ENERGY_DASHBOARD_MODE, DEFAULT_ENERGY_DASHBOARD_MODE
            )

            coeff = 0.8 if power_group == POWER_GROUP_LE_10KW else 0.7
            enable_synth = dashboard_mode == ENERGY_MODE_VIRTUAL_STORAGE

            options = getattr(self, "_pending_options", {})
            options[CONF_PROSUMER_COEFFICIENT] = coeff
            options[CONF_PROSUMER_POWER_GROUP] = power_group
            options[CONF_ENERGY_DASHBOARD_MODE] = dashboard_mode
            options[CONF_ENABLE_SYNTHETIC_STORAGE] = enable_synth
            options[CONF_CREATE_SETTLEMENT_DASHBOARD] = bool(
                user_input.get(
                    CONF_CREATE_SETTLEMENT_DASHBOARD,
                    DEFAULT_CREATE_SETTLEMENT_DASHBOARD,
                )
            )

            return await self._async_create_entry_with_ergo5_check(
                getattr(self, "_pending_title", "Energa My Meter"),
                getattr(self, "_pending_data", {}),
                options,
            )

        pending_dash = getattr(self, "_pending_options", {}).get(
            CONF_CREATE_SETTLEMENT_DASHBOARD, DEFAULT_CREATE_SETTLEMENT_DASHBOARD
        )
        return self.async_show_form(
            step_id="net_metering_survey",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROSUMER_POWER_GROUP, default=DEFAULT_PROSUMER_POWER_GROUP
                    ): vol.In(
                        {
                            POWER_GROUP_LE_10KW: "Moc do 10 kW (współczynnik 0.8 - odbierasz 80%, operator pobiera 20%)",
                            POWER_GROUP_GT_10KW: "Moc powyżej 10 kW (współczynnik 0.7 - odbierasz 70%, operator pobiera 30%)",
                        }
                    ),
                    vol.Required(
                        CONF_ENERGY_DASHBOARD_MODE, default=DEFAULT_ENERGY_DASHBOARD_MODE
                    ): vol.In(
                        {
                            ENERGY_MODE_VIRTUAL_STORAGE: "Wirtualny Magazyn Energii (Rekomendowany) — magazyn w Panelu Energia, prowizja 20% jako oddanie bezpłatne, pobór za 0 zł",
                            ENERGY_MODE_PHYSICAL_GRID: "Model Tradycyjny — surowy licznik fizyczny (całość importu i eksportu w sekcji Sieć)",
                        }
                    ),
                    vol.Optional(
                        CONF_CREATE_SETTLEMENT_DASHBOARD, default=pending_dash
                    ): bool,
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
            menu_options=[
                "credentials",
                "prices",
                "energy_dashboard",
                "history",
                "clear_stats",
            ],
        )

    async def async_step_energy_dashboard(self, user_input=None):
        """Manage Energy Dashboard presentation mode and prosumer settings."""
        if user_input is not None:
            power_group = user_input.get(
                CONF_PROSUMER_POWER_GROUP, DEFAULT_PROSUMER_POWER_GROUP
            )
            dashboard_mode = user_input.get(
                CONF_ENERGY_DASHBOARD_MODE, DEFAULT_ENERGY_DASHBOARD_MODE
            )
            enable_synth = user_input.get(
                CONF_ENABLE_SYNTHETIC_STORAGE,
                dashboard_mode == ENERGY_MODE_VIRTUAL_STORAGE,
            )
            coeff = 0.8 if power_group == POWER_GROUP_LE_10KW else 0.7
            create_dash = user_input.get(
                CONF_CREATE_SETTLEMENT_DASHBOARD,
                self._config_entry.options.get(
                    CONF_CREATE_SETTLEMENT_DASHBOARD,
                    DEFAULT_CREATE_SETTLEMENT_DASHBOARD,
                ),
            )

            new_options = {
                **dict(self._config_entry.options),
                CONF_PROSUMER_POWER_GROUP: power_group,
                CONF_PROSUMER_COEFFICIENT: coeff,
                CONF_ENERGY_DASHBOARD_MODE: dashboard_mode,
                CONF_ENABLE_SYNTHETIC_STORAGE: enable_synth,
                CONF_CREATE_SETTLEMENT_DASHBOARD: bool(create_dash),
            }
            return self.async_create_entry(title="", data=new_options)

        curr_options = dict(self._config_entry.options)
        curr_mode = curr_options.get(
            CONF_ENERGY_DASHBOARD_MODE, DEFAULT_ENERGY_DASHBOARD_MODE
        )
        curr_power = curr_options.get(
            CONF_PROSUMER_POWER_GROUP, DEFAULT_PROSUMER_POWER_GROUP
        )
        curr_synth = curr_options.get(
            CONF_ENABLE_SYNTHETIC_STORAGE, DEFAULT_ENABLE_SYNTHETIC_STORAGE
        )
        curr_create = curr_options.get(
            CONF_CREATE_SETTLEMENT_DASHBOARD, DEFAULT_CREATE_SETTLEMENT_DASHBOARD
        )

        return self.async_show_form(
            step_id="energy_dashboard",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENERGY_DASHBOARD_MODE, default=curr_mode
                    ): vol.In(
                        {
                            ENERGY_MODE_VIRTUAL_STORAGE: "Wirtualny Magazyn Energii (Rekomendowany)",
                            ENERGY_MODE_PHYSICAL_GRID: "Model Tradycyjny (Fizyczny licznik)",
                        }
                    ),
                    vol.Required(
                        CONF_PROSUMER_POWER_GROUP, default=curr_power
                    ): vol.In(
                        {
                            POWER_GROUP_LE_10KW: "Moc do 10 kW (współczynnik 0.8)",
                            POWER_GROUP_GT_10KW: "Moc powyżej 10 kW (współczynnik 0.7)",
                        }
                    ),
                    vol.Required(
                        CONF_ENABLE_SYNTHETIC_STORAGE, default=curr_synth
                    ): bool,
                    vol.Optional(
                        CONF_CREATE_SETTLEMENT_DASHBOARD, default=curr_create
                    ): bool,
                }
            ),
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

    def _old_system_hint(self) -> bool | None:
        """Best-effort settlement-system hint for product defaults (v1.9.2)."""
        coeff = self._config_entry.options.get(CONF_PROSUMER_COEFFICIENT)
        if coeff is None:
            return None
        try:
            return float(coeff) >= 0.7
        except (ValueError, TypeError):
            return None

    async def async_step_prices(self, user_input=None):
        """Handle energy price configuration."""
        if user_input is not None:
            # Auto-calculate total initial kWh if zone initial values are provided
            init_l1 = float(user_input.get(CONF_BANK_INITIAL_KWH_L1, 0.0) or 0.0)
            init_l2 = float(user_input.get(CONF_BANK_INITIAL_KWH_L2, 0.0) or 0.0)
            if init_l1 > 0 or init_l2 > 0:
                cur_total = float(user_input.get(CONF_BANK_INITIAL_KWH, 0.0) or 0.0)
                old_total = float(self._config_entry.options.get(CONF_BANK_INITIAL_KWH, DEFAULT_BANK_INITIAL_KWH))
                if cur_total == 0.0 or cur_total == old_total:
                    user_input[CONF_BANK_INITIAL_KWH] = round(init_l1 + init_l2, 2)

            # Save global prices
            new_options = {**self._config_entry.options, **user_input}

            if CONF_PROSUMER_COEFFICIENT in user_input:
                try:
                    c_val = float(user_input[CONF_PROSUMER_COEFFICIENT])
                    if c_val < 0.7:
                        new_options[CONF_ENABLE_SYNTHETIC_STORAGE] = False
                        new_options[CONF_ENERGY_DASHBOARD_MODE] = ENERGY_MODE_PHYSICAL_GRID
                except (ValueError, TypeError):
                    pass

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
            current_initial_kwh_1 = self._config_entry.options.get(CONF_BANK_INITIAL_KWH_L1, DEFAULT_BANK_INITIAL_KWH_L1)
            current_initial_kwh_2 = self._config_entry.options.get(CONF_BANK_INITIAL_KWH_L2, DEFAULT_BANK_INITIAL_KWH_L2)
            current_initial_pln = self._config_entry.options.get(CONF_BANK_INITIAL_PLN, DEFAULT_BANK_INITIAL_PLN)
            current_rce_auto = self._config_entry.options.get(CONF_RCE_AUTO_FETCH, DEFAULT_RCE_AUTO_FETCH)
            current_settlement = self._config_entry.options.get(CONF_SETTLEMENT_DATE, DEFAULT_SETTLEMENT_DATE)
            current_auto_settle = self._config_entry.options.get(CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT)
            current_rolling = self._config_entry.options.get(CONF_USE_ROLLING_365D, DEFAULT_USE_ROLLING_365D)
            current_inverter = self._config_entry.options.get(CONF_INVERTER_ENERGY_ENTITY, DEFAULT_INVERTER_ENERGY_ENTITY)

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
                            CONF_BANK_INITIAL_KWH_L1, default=current_initial_kwh_1
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BANK_INITIAL_KWH_L2, default=current_initial_kwh_2
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_BANK_INITIAL_PLN, default=current_initial_pln
                        ): vol.Coerce(float),
                        vol.Optional(
                            CONF_INVERTER_ENERGY_ENTITY, default=current_inverter
                        ): selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="sensor", device_class="energy")
                        ) if hasattr(selector, "EntitySelector") else str,
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
                        **_tariff_fee_schema(
                            self._config_entry.options,
                            self._dominant_tariff(),
                            self._old_system_hint(),
                        ),
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
            current_inverter = self._config_entry.options.get(CONF_INVERTER_ENERGY_ENTITY, DEFAULT_INVERTER_ENERGY_ENTITY)

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
                            CONF_INVERTER_ENERGY_ENTITY, default=current_inverter
                        ): selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="sensor", device_class="energy")
                        ) if hasattr(selector, "EntitySelector") else str,
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
                        **_tariff_fee_schema(
                            self._config_entry.options,
                            self._dominant_tariff(),
                            self._old_system_hint(),
                        ),
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
                for entity in list(entity_registry.entities.values())
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
