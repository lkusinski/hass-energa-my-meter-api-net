"""Date platform for Energa My Meter (Faza 2).

Two user-editable dates per active meter select the period used by the
``button.energa_<id>_przelicz_okres`` button and the
``sensor.energa_<id>_weryfikacja_rachunku`` result sensor. Values live in
``entry.options`` (``verify_period_start`` / ``verify_period_end``) so they
survive restarts; editing a date updates the entry and therefore reloads the
integration through the existing options listener.
"""

from __future__ import annotations

import logging

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_VERIFY_PERIOD_END,
    CONF_VERIFY_PERIOD_START,
    DOMAIN,
)
from .core.verification import parse_period_date

_LOGGER = logging.getLogger(__name__)

_LABELS = {
    "start": ("Okres Start", "period_start", "okres_start"),
    "end": ("Okres Koniec", "period_end", "okres_koniec"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the verification period date entities."""
    api = hass.data[DOMAIN][entry.entry_id]["api"]
    try:
        meters_list = await api.async_get_data(force_refresh=False)
    except Exception as err:  # noqa: BLE001 - must never break entry setup
        _LOGGER.error("Energa: Failed to fetch meters for date setup: %s", err)
        meters_list = []

    active_meters = [
        m
        for m in (meters_list or [])
        if m.get("total_plus") and float(m.get("total_plus", 0) or 0) > 0
    ]

    entities = []
    for meter in active_meters:
        serial = str(meter.get("meter_serial", meter["meter_point_id"]))
        ppe = meter.get("ppe", meter["meter_point_id"])
        device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=f"Energa {serial}",
            manufacturer="Energa-Operator",
            model=f"PPE: {ppe}",
            configuration_url="https://mojlicznik.energa-operator.pl",
        )
        for kind in ("start", "end"):
            entities.append(EnergaPeriodDate(entry, meter, kind, device_info))

    if entities:
        async_add_entities(entities, update_before_add=True)


class EnergaPeriodDate(DateEntity):
    """One end of the user-selected verification period (YYYY-MM-DD)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-range"

    def __init__(
        self,
        entry: ConfigEntry,
        meter: dict,
        kind: str,
        device_info: DeviceInfo,
    ) -> None:
        if kind not in _LABELS:
            raise ValueError(f"unknown period date kind: {kind!r}")
        self._entry = entry
        self._kind = kind
        self._meter_id = meter.get("meter_point_id")
        self._serial = str(meter.get("meter_serial", self._meter_id))
        self._option_key = (
            CONF_VERIFY_PERIOD_START if kind == "start" else CONF_VERIFY_PERIOD_END
        )
        name, translation_key, entity_suffix = _LABELS[kind]
        self._attr_name = name
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"energa_{self._meter_id}_period_{kind}"
        self.entity_id = f"date.energa_{self._serial}_{entity_suffix}".lower()
        self._attr_device_info = device_info

    @property
    def native_value(self):
        """Configured date, or ``None`` when unset/invalid."""
        return parse_period_date((self._entry.options or {}).get(self._option_key))

    async def async_set_value(self, value) -> None:
        """Persist the chosen date into ``entry.options``."""
        parsed = parse_period_date(value)
        if parsed is None:
            _LOGGER.debug(
                "Energa: ignoring invalid %s value %r", self._option_key, value
            )
            return
        options = dict(self._entry.options or {})
        if options.get(self._option_key) == parsed.isoformat():
            return
        options[self._option_key] = parsed.isoformat()
        self.hass.config_entries.async_update_entry(self._entry, options=options)
        self.async_write_ha_state()
