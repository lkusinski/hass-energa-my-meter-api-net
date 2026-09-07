"""Binary sensor platform for Energa My Meter — BESS & Dynamic Tariff Arbitrage.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 10 & 11 (Etap 5).
Provides decision binary sensors for Home Assistant automations:
- BESS charge window (optimal lowest-price hours)
- BESS peak discharge window (highest-price hours)
- Negative RCE price alert (prosumer export protection)
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .projections.arbitrage import ArbitrageAction

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Energa binary sensors from config entry."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get("coordinator")

    if not coordinator:
        _LOGGER.debug("Coordinator not yet available for binary sensors")
        return

    meters = coordinator.data or []
    active_meters = [
        m for m in meters
        if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
    ]

    entities: list[BinarySensorEntity] = []
    for meter in active_meters:
        mid = str(meter.get("meter_point_id"))
        serial = str(meter.get("meter_serial") or mid)

        entities.extend([
            EnergaBessChargeWindowBinarySensor(coordinator, entry, mid, serial),
            EnergaBessDischargeWindowBinarySensor(coordinator, entry, mid, serial),
            EnergaRceNegativePriceBinarySensor(coordinator, entry, mid, serial),
        ])

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Energa binary sensors for arbitrage & BESS", len(entities))


class EnergaBessChargeWindowBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating whether the current hour is within the optimal BESS charge window."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, meter_point_id: str, meter_serial: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._meter_point_id = meter_point_id
        self._meter_serial = meter_serial
        self._attr_unique_id = f"energa_{meter_point_id}_bess_charge_window"
        self._attr_name = "Okno ładowania BESS (Arbitraż RCE)"
        self._attr_icon = "mdi:battery-charging"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._meter_point_id)},
            name=f"Licznik {self._meter_serial}",
            manufacturer="Energa-Operator",
            model="Licznik zdalnego odczytu",
        )

    @property
    def is_on(self) -> bool:
        """Return True if the current timestamp is inside any charge window."""
        plan = getattr(self.coordinator, "_arbitrage_plan", None)
        if not plan or not plan.is_spread_profitable:
            return False
        now_utc = datetime.now(timezone.utc)
        return plan.action_at(now_utc) == ArbitrageAction.CHARGE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return details about charging schedule and spread."""
        plan = getattr(self.coordinator, "_arbitrage_plan", None)
        if not plan:
            return {"status": "no_plan_available"}

        windows_data = [
            {
                "start": w.start_utc.isoformat(),
                "end": w.end_utc.isoformat(),
                "avg_price_pln_kwh": float(w.avg_price_kwh),
                "duration_hours": w.duration_hours,
            }
            for w in plan.charge_windows
        ]

        return {
            "target_date": plan.target_date.isoformat(),
            "is_spread_profitable": plan.is_spread_profitable,
            "avg_charge_price_pln_kwh": float(plan.avg_charge_price_kwh),
            "effective_spread_pln_kwh": float(plan.effective_spread_kwh),
            "battery_efficiency": float(plan.battery_efficiency),
            "windows": windows_data,
        }


class EnergaBessDischargeWindowBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating whether the current hour is within the peak BESS discharge window."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, meter_point_id: str, meter_serial: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._meter_point_id = meter_point_id
        self._meter_serial = meter_serial
        self._attr_unique_id = f"energa_{meter_point_id}_bess_discharge_window"
        self._attr_name = "Okno rozładowania BESS (Szczyt RCE)"
        self._attr_icon = "mdi:battery-arrow-down"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._meter_point_id)},
            name=f"Licznik {self._meter_serial}",
            manufacturer="Energa-Operator",
            model="Licznik zdalnego odczytu",
        )

    @property
    def is_on(self) -> bool:
        """Return True if current timestamp is inside any peak discharge window."""
        plan = getattr(self.coordinator, "_arbitrage_plan", None)
        if not plan or not plan.is_spread_profitable:
            return False
        now_utc = datetime.now(timezone.utc)
        return plan.action_at(now_utc) == ArbitrageAction.DISCHARGE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return details about peak discharging schedule."""
        plan = getattr(self.coordinator, "_arbitrage_plan", None)
        if not plan:
            return {"status": "no_plan_available"}

        windows_data = [
            {
                "start": w.start_utc.isoformat(),
                "end": w.end_utc.isoformat(),
                "avg_price_pln_kwh": float(w.avg_price_kwh),
                "duration_hours": w.duration_hours,
            }
            for w in plan.discharge_windows
        ]

        return {
            "target_date": plan.target_date.isoformat(),
            "is_spread_profitable": plan.is_spread_profitable,
            "avg_discharge_price_pln_kwh": float(plan.avg_discharge_price_kwh),
            "effective_spread_pln_kwh": float(plan.effective_spread_kwh),
            "windows": windows_data,
        }


class EnergaRceNegativePriceBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor alerting when current or upcoming RCE price is negative (< 0 PLN/kWh)."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, entry: ConfigEntry, meter_point_id: str, meter_serial: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._meter_point_id = meter_point_id
        self._meter_serial = meter_serial
        self._attr_unique_id = f"energa_{meter_point_id}_rce_negative_price"
        self._attr_name = "Cena ujemna RCE (Zagrożenie eksportu)"
        self._attr_icon = "mdi:alert-decagram"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._meter_point_id)},
            name=f"Licznik {self._meter_serial}",
            manufacturer="Energa-Operator",
            model="Licznik zdalnego odczytu",
        )

    @property
    def is_on(self) -> bool:
        """Return True if current price is strictly below 0 PLN."""
        curr_rec = getattr(self.coordinator, "_rce_current_record", None)
        if curr_rec and curr_rec.price_kwh < 0:
            return True
        plan = getattr(self.coordinator, "_arbitrage_plan", None)
        if plan:
            now_utc = datetime.now(timezone.utc)
            return plan.action_at(now_utc) == ArbitrageAction.NEGATIVE_ALERT
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return information about negative intervals in the day-ahead schedule."""
        plan = getattr(self.coordinator, "_arbitrage_plan", None)
        curr_rec = getattr(self.coordinator, "_rce_current_record", None)
        attrs: dict[str, Any] = {
            "current_price_pln_kwh": float(curr_rec.price_kwh) if curr_rec else None,
            "negative_intervals_count": len(plan.negative_intervals) if plan else 0,
        }
        if plan and plan.negative_intervals:
            attrs["negative_intervals"] = [
                {
                    "start": r.interval_start_utc.isoformat() if r.interval_start_utc else None,
                    "end": r.interval_end_utc.isoformat() if r.interval_end_utc else None,
                    "price_pln_kwh": float(r.price_kwh),
                }
                for r in plan.negative_intervals
            ]
        return attrs
