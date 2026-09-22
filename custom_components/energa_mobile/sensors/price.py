"""Price and tariff sensors for Energa My Meter integration."""

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import (
    CONF_BANK_RCE_PRICE,
    CONF_PROSUMER_COEFFICIENT,
    CONF_RCE_AUTO_FETCH,
    DEFAULT_BANK_RCE_PRICE,
    DEFAULT_PROSUMER_COEFFICIENT,
    DEFAULT_RCE_AUTO_FETCH,
    DOMAIN,
    get_price_for_key,
)

_LOGGER = logging.getLogger(__name__)

class EnergaPriceSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor exposing configured energy price as HA entity.

    Enables Energy Dashboard "Use entity with current price" mode.
    Import prices come from config options; the EXPORT price (v0.3.0)
    is the live sale price in the new net-billing system
    (RCEm×1.23 from the coordinator cache, manual fallback) so the
    Energy panel values the grid return exactly like the deposit.
    In the old net-metering system nothing is sold (export feeds the
    kWh warehouse instead), so the export price is unknown by design —
    wire the export sensors as solar/battery there, not as grid return.
    """

    def __init__(
        self,
        coordinator,
        data_key: str,
        name: str,
        icon: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
        serial: str,
        meter_id: str,
    ) -> None:
        """Initialize price sensor."""
        super().__init__(coordinator)
        self._data_key = data_key
        self._entry = entry
        self._serial = serial
        self._meter_id = meter_id

        self._attr_name = name
        self._attr_unique_id = f"energa_{serial}_{data_key}_price"
        self._attr_has_entity_name = True
        self._attr_icon = icon
        self._attr_device_info = device_info
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

        if data_key == "coefficient":
            self._attr_native_unit_of_measurement = None
            self._attr_state_class = None
        else:
            self._attr_native_unit_of_measurement = "PLN/kWh"
            self._attr_state_class = SensorStateClass.MEASUREMENT

    def _is_old_system(self) -> bool:
        try:
            coeff = float(
                self._entry.options.get(
                    CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT
                )
            )
        except (ValueError, TypeError):
            coeff = DEFAULT_PROSUMER_COEFFICIENT
        return coeff >= 0.7

    def _sale_rce(self) -> float:
        """RCEm used for the sale price (cache first, manual fallback)."""
        opts = self._entry.options
        if opts.get(CONF_RCE_AUTO_FETCH, DEFAULT_RCE_AUTO_FETCH):
            cached = getattr(self.coordinator, "_rce_cache", None)
            if cached is not None:
                try:
                    return float(cached)
                except (ValueError, TypeError):
                    pass
        try:
            return float(opts.get(CONF_BANK_RCE_PRICE, DEFAULT_BANK_RCE_PRICE))
        except (ValueError, TypeError):
            return DEFAULT_BANK_RCE_PRICE

    @property
    def native_value(self):
        """Return price from config options (export = live sale price)."""
        opts = dict(self._entry.options)

        if self._data_key == "coefficient":
            return float(
                opts.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT)
            )

        if self._data_key == "export":
            if self._is_old_system():
                # Warehouse system: export is credited in kWh, not sold.
                self._attr_extra_state_attributes = {
                    "note": "Stary net-metering: nadwyżka trafia do magazynu kWh, nie na sprzedaż — brak ceny.",
                }
                return None
            rce = self._sale_rce()
            self._attr_extra_state_attributes = {
                "rce_price": rce,
                "vat_multiplier": 1.23,
                "rce_source": getattr(self.coordinator, "_rce_source", None) or "manual",
                "note": "Cena sprzedaży nadwyżki = RCEm×1.23 (jak depozyt w Bank PLN). Podepnij jako cenę zwrotu w Panelu Energia.",
                "formula": "RCEm × 1.23",
            }
            return round(rce * 1.23, 5)

        price_val = get_price_for_key(
            opts, self._data_key, meter_id=self._meter_id
        )
        if self._data_key.startswith("import"):
            zone_desc = (
                "Strefa 1 — Dzień (szczyt)"
                if self._data_key == "import_1"
                else "Strefa 2 — Noc (pozaszczyt / weekend)"
                if self._data_key == "import_2"
                else "Taryfa jednostrefowa (G11)"
            )
            self._attr_extra_state_attributes = {
                "stawka_calkowita_brutto": round(price_val, 4),
                "unit": "PLN/kWh",
                "opis": "Łączny koszt 1 kWh brutto (energia czynna + opłaty dystrybucyjne zmienne + VAT 23%)",
                "strefa": zone_desc,
                "uwaga": "Cena w Panelu Energia uwzględnia pełny koszt zmienny (energię i dystrybucję brutto), dzięki czemu kalkulacja kosztów poboru pokrywa się z realną fakturą.",
            }
        return price_val


class EnergaRceSensor(CoordinatorEntity, SensorEntity):
    """RCEm sensor — auto-fetch monthly RCE from PSE or use manual value.

    Displays the current RCEm (PLN/kWh) used for net-billing calculations.
    When auto-fetch is enabled, reads from Coordinator cache (24h) populated
    in _async_update_data. Falls back to manual value if fetch fails.
    """

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry, api, serial: str = "") -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._entry = entry
        self._api = api
        self._attr_name = "RCEm Auto"
        self._attr_unique_id = f"energa_{meter_id}_rcem_auto"
        self._attr_has_entity_name = True
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "PLN/kWh"
        # NOTE: no monetary device_class — PLN/kWh is a price, and
        # monetary+measurement is rejected by HA (v0.2.12 fix).
        self._attr_icon = "mdi:chart-line"
        self._attr_device_info = device_info

    @property
    def native_value(self):
        """Return RCEm — either coordinator-cached or manual fallback."""
        opts = self._entry.options
        auto_fetch = opts.get(CONF_RCE_AUTO_FETCH, DEFAULT_RCE_AUTO_FETCH)
        # Coordinator holds shared 24h cache
        if auto_fetch and getattr(self.coordinator, "_rce_cache", None) is not None:
            return self.coordinator._rce_cache
        return float(opts.get(CONF_BANK_RCE_PRICE, DEFAULT_BANK_RCE_PRICE))

    @property
    def extra_state_attributes(self):
        coord_cache = getattr(self.coordinator, "_rce_cache", None)
        coord_last = getattr(self.coordinator, "_rce_last_fetch", None)
        coord_source = getattr(self.coordinator, "_rce_source", None)
        return {
            "auto_fetch": bool(self._entry.options.get(CONF_RCE_AUTO_FETCH, DEFAULT_RCE_AUTO_FETCH)),
            "cached_rcem": coord_cache,
            "last_fetch": coord_last.isoformat() if coord_last else None,
            "manual_fallback": float(self._entry.options.get(CONF_BANK_RCE_PRICE, DEFAULT_BANK_RCE_PRICE)),
            "source": coord_source or ("PSE API (api.raporty.pse.pl)" if coord_cache else "manual input"),
            "vat_note": "×1.23 w Bank PLN (Dz.U. 1847)",
            "method_note": "Oficjalne RCEm (średnia ważona PSE); fallback: zwykła średnia RCE",
        }

class PseRceDynamicPriceSensor(CoordinatorEntity, SensorEntity):
    """Dynamic RCE market price sensor (PSE OIRE 15-min / hourly intervals)."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "PLN/kWh"
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        meter_point_id: str,
        meter_serial: str,
        meter_name: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._meter_point_id = meter_point_id
        self._meter_serial = meter_serial
        self._meter_name = meter_name or ""
        self._attr_unique_id = f"energa_{meter_point_id}_rce_dynamic_price"
        self._attr_name = "Dynamiczna cena energii RCE"
        self._attr_icon = "mdi:chart-line"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._meter_serial))},
            name=f"Energa {self._meter_name or self._meter_serial}",
            manufacturer="Energa-Operator",
            model="Licznik zdalnego odczytu",
            configuration_url="https://mojlicznik.energa-operator.pl",
        )

    @property
    def native_value(self) -> float | None:
        rec = getattr(self.coordinator, "_rce_current_record", None)
        if rec is not None:
            return float(rec.price_with_vat_multiplier)
        rcem = getattr(self.coordinator, "_rce_cache", None)
        return round(float(rcem) * 1.23, 5) if rcem is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rec = getattr(self.coordinator, "_rce_current_record", None)
        if rec:
            return {
                "price_brutto_pln_kwh": float(rec.price_with_vat_multiplier),
                "price_netto_pln_kwh": float(rec.price_kwh),
                "vat_multiplier": 1.23,
                "price_mwh": float(rec.price_mwh),
                "resolution": rec.resolution,
                "interval_start_utc": rec.interval_start_utc.isoformat() if rec.interval_start_utc else None,
                "interval_end_utc": rec.interval_end_utc.isoformat() if rec.interval_end_utc else None,
                "source": "PSE OIRE API (api.raporty.pse.pl)",
                "note": "Wartość brutto (z VAT 23%) — wycena depozytu prosumenckiego zgodnie z art. 4b ustawy OZE",
            }
        return {"source": "brak danych"}


class PseRceArbitrageSpreadSensor(CoordinatorEntity, SensorEntity):
    """BESS Arbitrage Spread sensor comparing discharge peak vs charge valley."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "PLN/kWh"
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        meter_point_id: str,
        meter_serial: str,
        meter_name: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._meter_point_id = meter_point_id
        self._meter_serial = meter_serial
        self._meter_name = meter_name or ""
        self._attr_unique_id = f"energa_{meter_point_id}_bess_arbitrage_spread"
        self._attr_name = "Spread arbitrażowy BESS (RCE)"
        self._attr_icon = "mdi:swap-vertical-bold"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._meter_serial))},
            name=f"Energa {self._meter_name or self._meter_serial}",
            manufacturer="Energa-Operator",
            model="Licznik zdalnego odczytu",
            configuration_url="https://mojlicznik.energa-operator.pl",
        )

    @property
    def native_value(self) -> float | None:
        plan = getattr(self.coordinator, "_arbitrage_plan", None)
        if plan is not None:
            return round(float(plan.effective_spread_kwh) * 1.23, 5)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = getattr(self.coordinator, "_arbitrage_plan", None)
        if not plan:
            return {"status": "no_plan"}
        return {
            "is_spread_profitable": plan.is_spread_profitable,
            "effective_spread_brutto_pln_kwh": round(float(plan.effective_spread_kwh) * 1.23, 5),
            "effective_spread_netto_pln_kwh": float(plan.effective_spread_kwh),
            "avg_charge_price_brutto_pln_kwh": round(float(plan.avg_charge_price_kwh) * 1.23, 5),
            "avg_discharge_price_brutto_pln_kwh": round(float(plan.avg_discharge_price_kwh) * 1.23, 5),
            "avg_charge_price_pln_kwh": float(plan.avg_charge_price_kwh),
            "avg_discharge_price_pln_kwh": float(plan.avg_discharge_price_kwh),
            "battery_efficiency": float(plan.battery_efficiency),
            "target_date": plan.target_date.isoformat(),
            "vat_multiplier": 1.23,
        }


