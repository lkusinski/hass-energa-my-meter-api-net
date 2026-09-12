"""Button platform for Energa My Meter integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ENABLE_SYNTHETIC_STORAGE,
    CONF_INVERTER_ENERGY_ENTITY,
    CONF_PROSUMER_COEFFICIENT,
    DEFAULT_ENABLE_SYNTHETIC_STORAGE,
    DEFAULT_PROSUMER_COEFFICIENT,
    DOMAIN,
    get_price_for_key,
)
from .dashboard_generator import (
    DEFAULT_ICON,
    DEFAULT_TITLE,
    DEFAULT_URL_PATH,
    async_provision_dashboard,
)
from .settlement import is_export_prosumer

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Energa button entities."""
    api = hass.data[DOMAIN][entry.entry_id]["api"]
    try:
        meters_list = await api.async_get_data(force_refresh=False)
    except Exception as err:
        _LOGGER.error("Energa: Failed to fetch meters for button setup: %s", err)
        meters_list = []

    active_meters = [
        m
        for m in meters_list
        if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
    ]

    buttons = []
    for meter in active_meters:
        buttons.append(
            EnergaCreateDashboardButton(
                hass=hass,
                entry=entry,
                meter=meter,
                all_meters=active_meters,
            )
        )
        buttons.append(
            EnergaConfigureEnergyDashboardButton(
                hass=hass,
                entry=entry,
                meter=meter,
            )
        )


    if buttons:
        async_add_entities(buttons)


class EnergaCreateDashboardButton(ButtonEntity):
    """Button to generate or refresh the Energa Lovelace dashboard."""

    _attr_has_entity_name = True
    _attr_name = "Utwórz Pulpit Rozliczeń"
    _attr_icon = "mdi:view-dashboard-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        meter: dict[str, Any],
        all_meters: list[dict[str, Any]],
    ) -> None:
        """Initialize the button."""
        self.hass = hass
        self._entry = entry
        self._meter = meter
        self._all_meters = all_meters

        meter_id = meter["meter_point_id"]
        serial = str(meter.get("meter_serial", meter_id))
        ppe = meter.get("ppe", meter_id)

        self._serial = serial
        self._attr_unique_id = f"energa_{serial}_create_dashboard"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(serial))},
            name=f"Energa {serial}",
            manufacturer="Energa-Operator",
            model=f"PPE: {ppe}",
            configuration_url="https://mojlicznik.energa-operator.pl",
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        coeff = float(
            self._entry.options.get(
                CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT
            )
        )
        _LOGGER.info(
            "Energa button pressed: provisioning dashboard for %d meters",
            len(self._all_meters),
        )
        success = await async_provision_dashboard(
            self.hass,
            self._all_meters,
            url_path=DEFAULT_URL_PATH,
            title=DEFAULT_TITLE,
            icon=DEFAULT_ICON,
            coeff=coeff,
        )
        if success:
            _LOGGER.info(
                "Energa dashboard /%s successfully created/updated", DEFAULT_URL_PATH
            )


class EnergaConfigureEnergyDashboardButton(ButtonEntity):
    """Button to automatically configure Home Assistant Energy Dashboard for Energa."""

    _attr_has_entity_name = True
    _attr_name = "Skonfiguruj Panel Energia"
    _attr_icon = "mdi:solar-power-variant-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        meter: dict[str, Any],
    ) -> None:
        """Initialize the button."""
        self.hass = hass
        self._entry = entry
        self._meter = meter

        meter_id = meter["meter_point_id"]
        serial = str(meter.get("meter_serial", meter_id))
        ppe = meter.get("ppe", meter_id)

        self._serial = serial
        self._attr_unique_id = f"energa_{serial}_configure_energy_dashboard"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(serial))},
            name=f"Energa {serial}",
            manufacturer="Energa-Operator",
            model=f"PPE: {ppe}",
            configuration_url="https://mojlicznik.energa-operator.pl",
        )

    async def async_press(self) -> None:
        """Handle the button press to configure Home Assistant Energy Dashboard."""
        try:
            from homeassistant.components.energy.data import async_get_manager
            manager = await async_get_manager(self.hass)
        except Exception as err:
            _LOGGER.error("Energy component or manager not available: %s", err)
            return

        if not manager:
            _LOGGER.error("Energy manager is None")
            return

        prefs = manager.data or manager.default_preferences()
        new_prefs = dict(prefs) if prefs else {}

        enable_synth = self._entry.options.get(
            CONF_ENABLE_SYNTHETIC_STORAGE, DEFAULT_ENABLE_SYNTHETIC_STORAGE
        )
        has_zones = self._meter.get("zone_count", 1) > 1
        serial = self._serial

        inverter_entity = self._entry.options.get(
            CONF_INVERTER_ENERGY_ENTITY,
            self._entry.options.get(f"meter_{serial}_{CONF_INVERTER_ENERGY_ENTITY}", "")
        )

        battery_sources = []
        flow_from = []
        flow_to = []

        if enable_synth:
            if has_zones:
                battery_sources = [
                    {
                        "stat_energy_from": f"sensor.energa_{serial}_syntetyczny_magazyn_l1_rozladowanie",
                        "stat_energy_to": f"sensor.energa_{serial}_syntetyczny_magazyn_l1_ladowanie",
                    },
                    {
                        "stat_energy_from": f"sensor.energa_{serial}_syntetyczny_magazyn_l2_rozladowanie",
                        "stat_energy_to": f"sensor.energa_{serial}_syntetyczny_magazyn_l2_ladowanie",
                    },
                ]
                flow_from = [
                    {
                        "stat_energy_from": f"sensor.energa_{serial}_syntetyczna_siec_pobor_strefa_1",
                        "stat_cost": None,
                        "entity_energy_price": f"sensor.energa_{serial}_cena_poboru_strefa_1",
                        "number_energy_price": None,
                    },
                    {
                        "stat_energy_from": f"sensor.energa_{serial}_syntetyczna_siec_pobor_strefa_2",
                        "stat_cost": None,
                        "entity_energy_price": f"sensor.energa_{serial}_cena_poboru_strefa_2",
                        "number_energy_price": None,
                    },
                ]
                flow_to = [
                    {
                        "stat_energy_to": f"sensor.energa_{serial}_syntetyczna_siec_oddanie_strefa_1",
                        "stat_compensation": None,
                        "entity_energy_price": None,
                        "number_energy_price": 0.0,
                    },
                    {
                        "stat_energy_to": f"sensor.energa_{serial}_syntetyczna_siec_oddanie_strefa_2",
                        "stat_compensation": None,
                        "entity_energy_price": None,
                        "number_energy_price": 0.0,
                    },
                ]
            else:
                battery_sources = [
                    {
                        "stat_energy_from": f"sensor.energa_{serial}_syntetyczny_magazyn_rozladowanie",
                        "stat_energy_to": f"sensor.energa_{serial}_syntetyczny_magazyn_ladowanie",
                    },
                ]
                flow_from = [
                    {
                        "stat_energy_from": f"sensor.energa_{serial}_syntetyczna_siec_pobor",
                        "stat_cost": None,
                        "entity_energy_price": f"sensor.energa_{serial}_cena_poboru",
                        "number_energy_price": None,
                    },
                ]
                flow_to = [
                    {
                        "stat_energy_to": f"sensor.energa_{serial}_syntetyczna_siec_oddanie",
                        "stat_compensation": None,
                        "entity_energy_price": None,
                        "number_energy_price": 0.0,
                    },
                ]
        else:
            if has_zones:
                flow_from = [
                    {
                        "stat_energy_from": f"sensor.energa_{serial}_panel_energia_strefa_1",
                        "stat_cost": None,
                        "entity_energy_price": f"sensor.energa_{serial}_cena_poboru_strefa_1",
                        "number_energy_price": None,
                    },
                    {
                        "stat_energy_from": f"sensor.energa_{serial}_panel_energia_strefa_2",
                        "stat_cost": None,
                        "entity_energy_price": f"sensor.energa_{serial}_cena_poboru_strefa_2",
                        "number_energy_price": None,
                    },
                ]
                flow_to = [
                    {
                        "stat_energy_to": f"sensor.energa_{serial}_panel_energia_produkcja_strefa_1",
                        "stat_compensation": None,
                        "entity_energy_price": None,
                        "number_energy_price": 0.0,
                    },
                    {
                        "stat_energy_to": f"sensor.energa_{serial}_panel_energia_produkcja_strefa_2",
                        "stat_compensation": None,
                        "entity_energy_price": None,
                        "number_energy_price": 0.0,
                    },
                ]
            else:
                flow_from = [
                    {
                        "stat_energy_from": f"sensor.energa_{serial}_panel_energia_zuzycie",
                        "stat_cost": None,
                        "entity_energy_price": f"sensor.energa_{serial}_cena_poboru",
                        "number_energy_price": None,
                    },
                ]
                flow_to = [
                    {
                        "stat_energy_to": f"sensor.energa_{serial}_panel_energia_produkcja",
                        "stat_compensation": None,
                        "entity_energy_price": None,
                        "number_energy_price": 0.0,
                    },
                ]

        grid_source = {
            "type": "grid",
            "flow_from": flow_from,
            "flow_to": flow_to,
            "cost_adjustment_day": 0.0,
        }

        existing_sources = list(new_prefs.get("energy_sources", []))
        kept_sources = [
            s for s in existing_sources
            if s.get("type") not in ("grid", "battery")
        ]
        kept_sources.append(grid_source)
        for b in battery_sources:
            kept_sources.append({"type": "battery", **b})

        if inverter_entity:
            has_solar = any(
                s.get("type") == "solar" and s.get("stat_energy_from") == inverter_entity
                for s in kept_sources
            )
            if not has_solar:
                kept_sources.append({
                    "type": "solar",
                    "stat_energy_from": inverter_entity,
                    "config_entry_solar_forecast": None,
                })

        new_prefs["energy_sources"] = kept_sources
        await manager.async_update(new_prefs)

        try:
            from homeassistant.components import persistent_notification
            mode_name = (
                "Wirtualny Magazyn Energii (Net-metering)"
                if enable_synth
                else "Licznik Fizyczny"
            )
            persistent_notification.async_create(
                self.hass,
                f"Panel Energia został pomyślnie zaktualizowany o konfigurację dla licznika {serial}!\n\n"
                f"Tryb: **{mode_name}**.\n"
                f"Wykresy są dostępne w zakładce [Panel Energia](/energy).",
                title="Energa: Panel Energia Skonfigurowany",
                notification_id=f"energa_energy_config_{serial}",
            )
        except Exception:
            pass

