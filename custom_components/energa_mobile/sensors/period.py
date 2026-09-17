"""Period verification result sensor for Energa My Meter (Faza 2).

Renders the latest ``energa_mobile.verify_period`` result computed by the
``Przelicz Okres`` button. The state is the amount payable (PLN) for the
chosen period; the full invoice breakdown lives in the attributes. No
``state_class``/``device_class`` energy is set, so this entity can never
pollute the Home Assistant Energy Dashboard statistics.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

_LOGGER = logging.getLogger(__name__)

# Invoice result keys copied verbatim into the sensor attributes.
_BREAKDOWN_KEYS = (
    "sale_energy_day",
    "sale_energy_night",
    "sale_total",
    "sale_gross",
    "excise",
    "excise_day",
    "excise_night",
    "excise_added",
    "excise_note",
    "trade_fee",
    "distr_var_day",
    "distr_var_night",
    "distr_quality",
    "distr_oze",
    "distr_cogen",
    "distr_fixed",
    "distr_total",
    "distr_gross",
    "netto",
    "vat",
    "brutto",
    "deposit",
    "deposit_applied",
    "do_zaplaty",
    "old_system",
    "kwh_source",
    "months",
)


class EnergaPeriodVerificationSensor(CoordinatorEntity, SensorEntity):
    """Latest arbitrary-period invoice verification result."""

    _attr_has_entity_name = True
    _attr_name = "Weryfikacja Rachunku"
    _attr_translation_key = "period_verification"
    _attr_icon = "mdi:receipt-text-check-outline"
    _attr_native_unit_of_measurement = "PLN"
    # Deliberately no state_class / energy device_class: this is a computed
    # period result, never an Energy Dashboard source (Faza 2 requirement).
    _attr_state_class = None

    def __init__(
        self,
        coordinator,
        meter_id: str,
        serial: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._meter_id = str(meter_id)
        self._serial = str(serial or meter_id)
        self._entry = entry
        self._attr_unique_id = f"energa_{self._meter_id}_period_verification"
        self.entity_id = (
            f"sensor.energa_{self._serial}_weryfikacja_rachunku".lower()
        )
        self._attr_device_info = device_info

    def _result(self) -> dict | None:
        store = getattr(self.coordinator, "_verify_result", None)
        if not isinstance(store, dict):
            return None
        result = store.get(self._meter_id)
        if result is None:
            result = store.get(self._serial)
        return result if isinstance(result, dict) else None

    @property
    def native_value(self):
        """Payable amount for the period, or ``None`` when unavailable."""
        result = self._result()
        if not result or result.get("empty"):
            return None
        try:
            return round(float(result.get("do_zaplaty")), 2)
        except (ValueError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> dict:
        result = self._result()
        if not result:
            return {"status": "no_result"}
        attrs: dict = {
            "status": "empty" if result.get("empty") else "ok",
            "period_start": result.get("period_start"),
            "period_end": result.get("period_end"),
            "source": result.get("source"),
            "cached": bool(result.get("cached", False)),
            "rcem": result.get("rcem"),
            "kwh": result.get("kwh"),
            "error": result.get("error"),
        }
        for key in _BREAKDOWN_KEYS:
            if key in result:
                attrs[key] = result.get(key)
        return attrs

    @property
    def available(self) -> bool:
        """A result exists (even an empty one reports status in attributes)."""
        return self._result() is not None
