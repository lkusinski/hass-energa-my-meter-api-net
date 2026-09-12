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
from .settlement import is_export_prosumer, is_net_metering, is_net_billing

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
        s_slug = str(serial).lower()

        inverter_entity = self._entry.options.get(
            CONF_INVERTER_ENERGY_ENTITY,
            self._entry.options.get(f"meter_{serial}_{CONF_INVERTER_ENERGY_ENTITY}", "")
        )

        is_producer = is_export_prosumer(self._meter) or bool(self._meter.get("obis_minus"))
        grid_sources = []
        battery_sources = []

        try:
            coeff = float(self._entry.options.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT))
        except (ValueError, TypeError):
            coeff = DEFAULT_PROSUMER_COEFFICIENT

        if coeff >= 0.7 and enable_synth:
            fee_pct = int(round((1.0 - coeff) * 100))
            if has_zones:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczna_siec_pobor_strefa_1",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczna_siec_oddanie_strefa_1",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_1",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": 0.0,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 1 (Dzień / Prowizja {fee_pct}%)",
                    },
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczna_siec_pobor_strefa_2",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczna_siec_oddanie_strefa_2",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_2",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": 0.0,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 2 (Noc / Prowizja {fee_pct}%)",
                    },
                ]
                battery_sources = [
                    {
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczny_magazyn_l1_rozladowanie",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczny_magazyn_l1_ladowanie",
                        "name": f"Wirtualny Magazyn Energa {serial} - Strefa 1 (Opust {coeff})",
                    },
                    {
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczny_magazyn_l2_rozladowanie",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczny_magazyn_l2_ladowanie",
                        "name": f"Wirtualny Magazyn Energa {serial} - Strefa 2 (Opust {coeff})",
                    },
                ]
            else:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczna_siec_pobor",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczna_siec_oddanie",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": 0.0,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} (Prowizja {fee_pct}%)",
                    },
                ]
                battery_sources = [
                    {
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczny_magazyn_rozladowanie",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczny_magazyn_ladowanie",
                        "name": f"Wirtualny Magazyn Energa {serial} (Opust {coeff})",
                    },
                ]
        elif is_producer and coeff < 0.7:
            if has_zones:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_strefa_1",
                        "stat_energy_to": f"sensor.energa_{s_slug}_panel_energia_produkcja_strefa_1",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_1",
                        "number_energy_price": None,
                        "entity_energy_price_export": f"sensor.energa_{s_slug}_cena_oddania",
                        "number_energy_price_export": None,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 1",
                    },
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_strefa_2",
                        "stat_energy_to": f"sensor.energa_{s_slug}_panel_energia_produkcja_strefa_2",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_2",
                        "number_energy_price": None,
                        "entity_energy_price_export": f"sensor.energa_{s_slug}_cena_oddania",
                        "number_energy_price_export": None,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 2",
                    },
                ]
            else:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_zuzycie",
                        "stat_energy_to": f"sensor.energa_{s_slug}_panel_energia_produkcja",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru",
                        "number_energy_price": None,
                        "entity_energy_price_export": f"sensor.energa_{s_slug}_cena_oddania",
                        "number_energy_price_export": None,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial}",
                    },
                ]
            battery_sources = []
        else:
            if has_zones:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_strefa_1",
                        "stat_energy_to": (f"sensor.energa_{s_slug}_panel_energia_produkcja_strefa_1" if is_producer else None),
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_1",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": (0.0 if is_producer else None),
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 1",
                    },
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_strefa_2",
                        "stat_energy_to": (f"sensor.energa_{s_slug}_panel_energia_produkcja_strefa_2" if is_producer else None),
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_2",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": (0.0 if is_producer else None),
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 2",
                    },
                ]
            else:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_zuzycie",
                        "stat_energy_to": (f"sensor.energa_{s_slug}_panel_energia_produkcja" if is_producer else None),
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": (0.0 if is_producer else None),
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial}",
                    },
                ]
            battery_sources = []

        existing_sources = list(new_prefs.get("energy_sources", []))
        kept_sources = [
            s for s in existing_sources
            if s.get("type") not in ("grid", "battery")
        ]
        kept_sources.extend(grid_sources)
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

        if enable_synth:
            try:
                from .synthetic_storage import async_synthesize_storage_from_recorder
                await async_synthesize_storage_from_recorder(self.hass, self._entry, self._meter)
            except Exception as synth_err:
                _LOGGER.debug("Immediate synthetic backfill during button press skipped: %s", synth_err)

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

