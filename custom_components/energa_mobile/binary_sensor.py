"""Binary sensor platform for Energa My Meter — BESS & Dynamic Tariff Arbitrage.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 10 & 11 (Etap 5).
Provides decision binary sensors for Home Assistant automations:
- BESS charge window (optimal lowest-price hours)
- BESS peak discharge window (highest-price hours)
- Negative RCE price alert (prosumer export protection)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, get_price_for_key
from .projections.arbitrage import ArbitrageAction
from .projections.forecast import DayType, determine_tariff_zone, get_day_type

_LOGGER = logging.getLogger(__name__)
TIMEZONE = ZoneInfo("Europe/Warsaw")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Energa binary sensors from config entry."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    api = entry_data.get("api")
    _LOGGER.debug("Energa binary_sensor: setup called for entry %s", entry.entry_id)

    if not coordinator:
        _LOGGER.debug("Coordinator not yet available for binary sensors")
        return

    meters = coordinator.data
    if not meters and api:
        try:
            meters = await api.async_get_data(force_refresh=False)
        except Exception as err:
            _LOGGER.error("Energa: Failed to fetch meters for binary sensors: %s", err)
            meters = []
    meters = meters or []

    active_meters = [
        m for m in meters
        if m.get("meter_point_id")
        and (
            float(m.get("total_plus", 0) or 0) > 0
            or float(m.get("total_minus", 0) or 0) > 0
        )
    ]
    if not active_meters and meters:
        active_meters = [m for m in meters if m.get("meter_point_id")]

    entities: list[BinarySensorEntity] = []
    for meter in active_meters:
        mid = str(meter.get("meter_point_id"))
        serial = str(meter.get("meter_serial") or mid)
        tariff = str(meter.get("tariff") or "G11")

        entities.extend([
            EnergaTaniaStrefaBinarySensor(coordinator, entry, mid, serial, tariff),
            EnergaBessChargeWindowBinarySensor(coordinator, entry, mid, serial),
            EnergaBessDischargeWindowBinarySensor(coordinator, entry, mid, serial),
            EnergaRceNegativePriceBinarySensor(coordinator, entry, mid, serial),
        ])

    _LOGGER.info("Energa binary_sensor: created %d entities for %d active meters", len(entities), len(active_meters))
    if entities:
        async_add_entities(entities, update_before_add=False)
        _LOGGER.info("Energa binary_sensor: added %d entities successfully", len(entities))


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
            identifiers={(DOMAIN, str(self._meter_serial))},
            name=f"Energa {self._meter_serial}",
            manufacturer="Energa-Operator",
            model="Licznik zdalnego odczytu",
            configuration_url="https://mojlicznik.energa-operator.pl",
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
            identifiers={(DOMAIN, str(self._meter_serial))},
            name=f"Energa {self._meter_serial}",
            manufacturer="Energa-Operator",
            model="Licznik zdalnego odczytu",
            configuration_url="https://mojlicznik.energa-operator.pl",
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
            identifiers={(DOMAIN, str(self._meter_serial))},
            name=f"Energa {self._meter_serial}",
            manufacturer="Energa-Operator",
            model="Licznik zdalnego odczytu",
            configuration_url="https://mojlicznik.energa-operator.pl",
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


class EnergaTaniaStrefaBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating whether off-peak (cheap / T2) zone is active."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        meter_point_id: str,
        meter_serial: str,
        tariff: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._meter_point_id = meter_point_id
        self._meter_serial = meter_serial
        self._tariff = (tariff or "G11").upper()
        self._attr_unique_id = f"energa_{meter_point_id}_tania_strefa"
        self._attr_name = "Tania strefa"

    async def async_added_to_hass(self) -> None:
        """Register hourly time change listener to update zone state on the hour."""
        await super().async_added_to_hass()

        try:
            from homeassistant.helpers.event import async_track_time_change

            @callback
            def _hourly_update(_now: datetime) -> None:
                self.async_write_ha_state()

            self.async_on_remove(
                async_track_time_change(
                    self.hass, _hourly_update, minute=0, second=0
                )
            )
        except Exception as err:  # noqa: BLE001 - must not break entity setup
            # v1.9.2 (P1.5): log instead of a silent pass — a failed hourly
            # subscription otherwise disables refreshes with no trace.
            _LOGGER.debug("Failed to register hourly arbitrage refresh: %s", err)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._meter_serial))},
            name=f"Energa {self._meter_serial}",
            manufacturer="Energa-Operator",
            model="Licznik zdalnego odczytu",
            configuration_url="https://mojlicznik.energa-operator.pl",
        )

    @property
    def is_on(self) -> bool:
        """Return True if off-peak zone (T2) is active."""
        if "G11" in self._tariff:
            return False
        now_local = datetime.now(TIMEZONE)
        return determine_tariff_zone(self._tariff, now_local) == 2

    @property
    def icon(self) -> str:
        """Return dynamic icon based on zone state."""
        return "mdi:clock-check-outline" if self.is_on else "mdi:clock-alert-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return details about current and next tariff zone."""
        now_local = datetime.now(TIMEZONE)
        zone = determine_tariff_zone(self._tariff, now_local)
        is_g11 = "G11" in self._tariff
        zone_id = "T1" if (is_g11 or zone == 1) else "T2"

        if is_g11:
            zone_name = "Taryfa jednostrefowa (stała stawka)"
            next_change_iso = None
            hours_until_change = None
            active_price = get_price_for_key(
                dict(self._entry.options), "import", meter_id=self._meter_serial
            )
        else:
            zone_name = (
                "Strefa pozaszczytowa (T2 - tania)"
                if zone == 2
                else "Strefa szczytowa (T1 - standardowa)"
            )
            price_key = "import_2" if zone == 2 else "import_1"
            active_price = get_price_for_key(
                dict(self._entry.options), price_key, meter_id=self._meter_serial
            )

            # Calculate next zone transition (scan up to 168 hours)
            next_hour = (now_local + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )
            next_change = None
            for step in range(168):
                step_dt = next_hour + timedelta(hours=step)
                if determine_tariff_zone(self._tariff, step_dt) != zone:
                    next_change = step_dt
                    break

            if next_change:
                next_change_iso = next_change.isoformat()
                hours_until_change = round(
                    (next_change - now_local).total_seconds() / 3600.0, 2
                )
            else:
                next_change_iso = None
                hours_until_change = None

        return {
            "tariff": self._tariff,
            "zone_id": zone_id,
            "zone_name": zone_name,
            "next_zone_change": next_change_iso,
            "hours_until_next_zone": hours_until_change,
            "active_price_pln_kwh": active_price,
            "is_weekend_or_holiday": get_day_type(now_local.date())
            == DayType.WEEKEND,
        }

