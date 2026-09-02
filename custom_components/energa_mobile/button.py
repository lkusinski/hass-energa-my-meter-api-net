"""Button platform for Energa My Meter — Wykryj pierwszy odczyt."""

import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up button platform."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    api = data.get("api") if isinstance(data, dict) else data
    if not api or not hasattr(api, "_meters_data"):
        return

    try:
        meters = await api.async_get_data()
    except Exception:
        meters = api._meters_data or []

    coordinator = data.get("coordinator") if isinstance(data, dict) else None
    entities = []
    for meter in meters:
        meter_id = meter.get("meter_serial", meter["meter_point_id"])
        has_zones = meter.get("zone_count", 1) > 1
        device_info = DeviceInfo(
            identifiers={(DOMAIN, str(meter_id))},
            name=meter.get("address") or f"Energa {meter_id}",
            manufacturer="Energa Operator",
            model=meter.get("tariff") or "G12W" if has_zones else "G11",
        )
        # Use coordinator if available, else entry as fallback for CoordinatorEntity
        coord = coordinator if coordinator else entry
        entities.append(EnergaDetectFirstDataButton(coord, meter_id, device_info, entry, api))

    async_add_entities(entities)


class EnergaDetectFirstDataButton(CoordinatorEntity, ButtonEntity):
    """Pushbutton — hierarchically detect first data date (year→half→month→day, ~14 req)."""

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry, api):
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._api = api
        self._entry = entry
        self._attr_name = "Wykryj pierwszy odczyt"
        self._attr_unique_id = f"energa_{meter_id}_detect_first_data"
        self._attr_has_entity_name = True
        self._attr_icon = "mdi:calendar-search"
        self._attr_device_info = device_info
        self._attr_entity_category = None

    async def async_press(self) -> None:
        """Triggered by user — find first day with data and store in options."""
        _LOGGER.info("Button Wykryj pierwszy odczyt pressed for %s", self._meter_id)
        try:
            detected = await self._api.async_find_first_data_date(self._meter_id)
            if detected:
                date_str = detected.strftime("%Y-%m-%d")
                # Store per-meter and global for sensor/history default
                new_options = dict(self._entry.options)
                new_options[f"meter_{self._meter_id}_first_data_date"] = date_str
                new_options["first_data_date"] = date_str
                self.hass.config_entries.async_update_entry(self._entry, options=new_options)
                _LOGGER.info("First data date for %s: %s", self._meter_id, date_str)
            else:
                _LOGGER.warning("No first data date found for %s", self._meter_id)
        except Exception as err:
            _LOGGER.error("Detect first data failed for %s: %s", self._meter_id, err, exc_info=True)
