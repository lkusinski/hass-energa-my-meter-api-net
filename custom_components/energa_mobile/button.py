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
    CONF_PROSUMER_COEFFICIENT,
    DEFAULT_PROSUMER_COEFFICIENT,
    DOMAIN,
)
from .dashboard_generator import (
    DEFAULT_ICON,
    DEFAULT_TITLE,
    DEFAULT_URL_PATH,
    async_provision_dashboard,
)

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
