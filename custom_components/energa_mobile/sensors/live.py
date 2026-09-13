"""Live meter, statistics and diagnostic sensors for Energa My Meter integration."""

import logging
from typing import Any, override

from homeassistant.components.recorder.models import (
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import (
    CONF_INVERTER_ENERGY_ENTITY,
    DOMAIN,
    get_meter_baseline,
    get_price_for_key,
    get_prosumer_coefficient,
)
from ..coordinator import EnergaCoordinator

_LOGGER = logging.getLogger(__name__)

class EnergaLiveSensor(CoordinatorEntity, SensorEntity):
    """Live sensor showing actual meter readings."""

    def __init__(
        self,
        coordinator,
        meter_id: str,
        data_key: str,
        name: str,
        icon: str,
        device_info: DeviceInfo,
        state_class_override: SensorStateClass = None,
    ) -> None:
        """Initialize live sensor."""
        super().__init__(coordinator)

        self._meter_id = meter_id
        self._data_key = data_key

        # Entity attributes
        self._attr_name = name
        self._attr_unique_id = f"energa_{meter_id}_{data_key}_live"
        self._attr_has_entity_name = True

        # Sensor class attributes
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = (
            state_class_override or SensorStateClass.TOTAL_INCREASING
        )
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

        # Device info
        self._attr_device_info = device_info

        # Icon
        self._attr_icon = icon

    @property
    def native_value(self):
        """Return current meter reading from API."""
        if not self.coordinator.data:
            _LOGGER.debug("LiveSensor %s: No coordinator data", self._attr_name)
            return None

        for meter in self.coordinator.data:
            # Compare as strings to avoid type mismatch
            if str(meter.get("meter_point_id")) == str(self._meter_id):
                value = meter.get(self._data_key)
                _LOGGER.debug(
                    "LiveSensor %s: key=%s, value=%s",
                    self._attr_name,
                    self._data_key,
                    value,
                )
                if value is not None:
                    try:
                        return float(value)
                    except (ValueError, TypeError):
                        return None
                elif self._data_key in ("total_minus", "total_minus_1", "total_minus_2", "daily_produkcja"):
                    return 0.0

        _LOGGER.debug(
            "LiveSensor %s: Meter %s not found in data", self._attr_name, self._meter_id
        )
        return None


class EnergaProsumerBalanceSensor(CoordinatorEntity, SensorEntity):
    """Prosumer balance sensor: (export − baseline_export) × coeff − (import − baseline_import).

    INTERNAL intermediate (v0.3.0: diagnostic, hidden by default): the
    user-facing values are Bank kWh/PLN (state) and Magazyn Poziom (%).
    Bilans is just Bank-minus-initial without the max(0,·) floor —
    showing both next to each other double-counts the same energy and
    confuses (e.g. Bilans 1128 kWh vs Bank 2486 kWh differ by
    exactly initial 1358 kWh). In the new net-billing system (coeff 0.0)
    it degenerates to −import, i.e. zero information.

    Uses real-time meter totals from the API minus user-configured baselines.
    Baselines represent meter readings at the start of the tracking period
    (e.g. the values from a prosumer bill on Feb 1).

    With baselines set to 0 (default), counts from meter installation (lifetime).
    Positive = surplus available. Negative = consumed more than produced.
    """

    def __init__(
        self,
        coordinator,
        meter_id: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
        serial: str = "",
        **kwargs,
    ) -> None:
        """Initialize prosumer balance sensor."""
        super().__init__(coordinator)

        self._meter_id = meter_id
        self._serial = serial
        self._entry = entry

        # Entity attributes (canonical clean Polish name, device-scoped)
        self._attr_name = "Bilans Prosumencki"
        self._attr_unique_id = f"energa_{meter_id}_prosumer_balance"
        self._attr_has_entity_name = True
        # Diagnostic: internal math detail, not a user-facing reading.
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

        # Sensor class attributes — no device_class because balance
        # can be negative (not compatible with SensorDeviceClass.ENERGY
        # which requires state_class 'total' or 'total_increasing')
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

        # Device info
        self._attr_device_info = device_info

        # Icon
        self._attr_icon = "mdi:scale-balance"

    @property
    def native_value(self):
        """Return prosumer balance: (export − baseline) × coeff − (import − baseline)."""
        totals = self.coordinator._meter_totals.get(str(self._meter_id))
        if not totals:
            return None

        current_import = totals.get("import", 0)
        current_export = totals.get("export", 0)

        mid = str(self._meter_id)
        ser = str(getattr(self, "_serial", ""))
        coefficient = get_prosumer_coefficient(self._entry.options, meter_id=mid, serial=ser)
        baseline_import = get_meter_baseline(self._entry.options, "import", meter_id=mid, serial=ser)
        baseline_export = get_meter_baseline(self._entry.options, "export", meter_id=mid, serial=ser)

        net_export = current_export - baseline_export
        net_import = current_import - baseline_import

        balance = (net_export * coefficient) - net_import
        return round(balance, 2)

    @property
    def extra_state_attributes(self):
        """Return extra attributes with breakdown."""
        totals = self.coordinator._meter_totals.get(str(self._meter_id))
        if not totals:
            return {}

        current_import = totals.get("import", 0)
        current_export = totals.get("export", 0)

        mid = str(self._meter_id)
        ser = str(getattr(self, "_serial", ""))
        coefficient = get_prosumer_coefficient(self._entry.options, meter_id=mid, serial=ser)
        baseline_import = get_meter_baseline(self._entry.options, "import", meter_id=mid, serial=ser)
        baseline_export = get_meter_baseline(self._entry.options, "export", meter_id=mid, serial=ser)

        net_export = current_export - baseline_export
        net_import = current_import - baseline_import

        return {
            "meter_import_kwh": round(current_import, 2),
            "meter_export_kwh": round(current_export, 2),
            "baseline_import_kwh": baseline_import,
            "baseline_export_kwh": baseline_export,
            "net_import_kwh": round(net_import, 2),
            "net_export_kwh": round(net_export, 2),
            "coefficient": coefficient,
            "effective_export_kwh": round(net_export * coefficient, 2),
            "calculation_method": "API meter totals minus baselines",
            "formula": "(export − baseline_export) × coefficient − (import − baseline_import)",
            "source": "Energa API: real-time meter readings (lastMeasurements)",
            "note": "Półprodukt do Banku (Bank=max(0,Bilans)+initial). Patrz Bank kWh/PLN i Magazyn Poziom %.",
        }

    @property
    def available(self) -> bool:
        """Sensor is available when we have data."""
        return self.coordinator.data is not None and self.native_value is not None

    @override
    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug(
            "ProsumerBalance %s: Coordinator update, value=%s",
            self._attr_name,
            self.native_value,
        )
        self.async_write_ha_state()


class EnergaFirstDataDateSensor(CoordinatorEntity, SensorEntity):
    """Start of the history window (v0.3.0: blind 730-day auto-backfill).

    Fresh entries store `auto_history_start` (today−730d) in entry data;
    legacy entries keep the hierarchically detected date in options.
    Either way this is the day the Panel Energia history starts from.
    """

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry, serial: str = "") -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._entry = entry
        self._attr_name = "Data Pierwszego Odczytu"
        self._attr_unique_id = f"energa_{meter_id}_first_data_date"
        self._attr_has_entity_name = True
        self._attr_device_class = SensorDeviceClass.DATE
        self._attr_icon = "mdi:calendar-start"
        self._attr_device_info = device_info
        self._attr_entity_category = None

    @property
    def native_value(self):
        # v0.3.0 blind window first, then legacy dates (trio entries
        # historically stored them in entry DATA, not options).
        data = (getattr(self._entry, "data", {}) or {})
        val = (
            data.get("auto_history_start")
            or data.get(f"meter_{self._meter_id}_first_data_date")
            or data.get("first_data_date")
        )
        if not val:
            val = self._entry.options.get(f"meter_{self._meter_id}_first_data_date") or self._entry.options.get("first_data_date")
        if val:
            try:
                from datetime import datetime
                return datetime.strptime(val, "%Y-%m-%d").date()
            except Exception:
                return None
        # Fallback to contract_date from meter data
        for m in self.coordinator.data or []:
            if str(m.get("meter_point_id")) == str(self._meter_id) or str(m.get("meter_serial")) == str(self._meter_id):
                if m.get("contract_date"):
                    try:
                        return datetime.strptime(str(m["contract_date"]), "%Y-%m-%d").date()
                    except Exception:
                        pass
        return None


class EnergaStatisticsSensor(CoordinatorEntity, SensorEntity):
    """Statistics sensor for Energy Dashboard.

    Imports hourly statistics into HA recorder database.
    Supports zone-specific data (import_1, import_2 for G12w).
    """

    def __init__(
        self,
        coordinator: EnergaCoordinator,
        meter_id: str,
        data_key: str,
        name: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
    ) -> None:
        """Initialize statistics sensor."""
        super().__init__(coordinator)

        self._meter_id = meter_id
        self._data_key = data_key
        self._entry = entry

        self._last_sum: float | None = None

        # Entity attributes
        self._attr_name = name
        self._attr_unique_id = f"energa_{meter_id}_{data_key}_stats"
        self._attr_has_entity_name = True

        # Sensor class attributes — state_class is required for Energy
        # Dashboard to list this entity in its configuration dropdown.
        # native_value returns the latest cumulative sum so the entity is
        # available in HA Energy dashboard without warnings.
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

        # Device info
        self._attr_device_info = device_info

        # Icon based on type
        if "import" in data_key:
            self._attr_icon = "mdi:transmission-tower"
        else:
            self._attr_icon = "mdi:solar-power"

    async def async_added_to_hass(self) -> None:
        """Seed last sum from recorder on startup."""
        await super().async_added_to_hass()
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import get_last_statistics

            last_stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, self.entity_id, True, {"sum", "start"}
            )
            if self.entity_id in last_stats and last_stats[self.entity_id]:
                self._last_sum = last_stats[self.entity_id][0].get("sum")
                self.coordinator._pre_fetched_stats[self.entity_id] = last_stats[self.entity_id][0]
                adapter = (
                    self.hass.data.get(DOMAIN, {})
                    .get(self._entry.entry_id, {})
                    .get("recorder_adapter")
                )
                if adapter and self._last_sum:
                    adapter.seed_last_sum(self.entity_id, float(self._last_sum))
                _LOGGER.debug("EnergaStatisticsSensor %s seeded sum=%.3f", self.entity_id, self._last_sum or 0.0)
        except Exception as err:
            _LOGGER.debug("Could not seed stats for %s: %s", self.entity_id, err)

    @property
    def native_value(self):
        """Return None — energy statistics flow exclusively via async_import_statistics.

        Prevents Home Assistant Core's recorder from calculating competing
        statistics from states table deltas, which previously caused sum resets
        and multi-megawatt spikes on the Energy Dashboard.
        """
        return None

    @property
    def available(self) -> bool:
        """Statistics sensor is available when coordinator has data."""
        return self.coordinator.data is not None

    def _get_price(self) -> float:
        """Get price for this sensor's zone/type."""
        return get_price_for_key(dict(self._entry.options), self._data_key, meter_id=self._meter_id)

    @override
    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator update - import energy and cost statistics to recorder.

        Uses EnergaDataUpdater for proper incremental statistics:
        - Queries last sum from database
        - Incrementally adds hourly values
        - Deduplicates already-imported points
        """
        from ..data_updater import EnergaDataUpdater

        _LOGGER.debug("Updating statistics for %s", self.entity_id)

        # Get hourly data from coordinator
        hourly_stats = self.coordinator.get_hourly_stats(self._meter_id, self._data_key)

        if not hourly_stats:
            _LOGGER.debug("No hourly stats available for %s", self.entity_id)
            super()._handle_coordinator_update()
            return

        # Convert coordinator format to DataUpdater format
        hourly_data = []
        for point in hourly_stats:
            try:
                hourly_data.append(
                    {
                        "dt": point["start"],
                        "value": point.get("state", 0),
                        "is_estimated": point.get("is_estimated", False),
                    }
                )
            except (KeyError, TypeError) as err:
                _LOGGER.warning("Invalid hourly point: %s", err)
                continue

        if not hourly_data:
            super()._handle_coordinator_update()
            return

        updater = EnergaDataUpdater(
            self.hass,
            self._entry,
            pre_fetched_stats=self.coordinator.get_pre_fetched_stats(),
        )

        energy_stats, cost_stats = updater.gather_stats_for_sensor(
            meter_id=self._meter_id,
            data_key=self._data_key,
            hourly_data=hourly_data,
            entity_id=self.entity_id,
            last_known_sum=float(self._last_sum or 0.0),
        )

        if not energy_stats:
            _LOGGER.debug("DataUpdater returned no stats for %s", self.entity_id)
            super()._handle_coordinator_update()
            return

        # === IMPORT ENERGY STATISTICS VIA RECORDER ADAPTER ===
        adapter = (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("recorder_adapter")
        )
        if adapter:
            adapter.import_energy_statistics(
                statistic_id=self.entity_id,
                statistics=energy_stats,
                name=self._attr_name,
                unit=self._attr_native_unit_of_measurement,
                last_known_sum=float(self._last_sum or 0.0),
            )
        else:
            energy_metadata = StatisticMetaData(
                source="recorder",
                statistic_id=self.entity_id,
                name=self._attr_name,
                unit_of_measurement=self._attr_native_unit_of_measurement,
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                unit_class="energy",
            )
            async_import_statistics(self.hass, energy_metadata, energy_stats)

        if energy_stats and "sum" in energy_stats[-1]:
            self._last_sum = energy_stats[-1]["sum"]
            self.coordinator._pre_fetched_stats[self.entity_id] = {
                "sum": energy_stats[-1]["sum"],
                "start": energy_stats[-1]["start"],
            }

        # === IMPORT COST STATISTICS (v0.3.0: import only) ===
        if cost_stats and not self._data_key.startswith("export"):
            cost_entity_id = f"{self.entity_id}_cost"
            if self._data_key == "import_1":
                cost_name = "Panel Energia Strefa 1 Koszt"
            elif self._data_key == "import_2":
                cost_name = "Panel Energia Strefa 2 Koszt"
            elif self._data_key == "import":
                cost_name = f"{self._attr_name} Koszt"
            else:
                cost_name = f"{self._attr_name} Rekompensata"

            price = self._get_price()
            _LOGGER.debug(
                "Importing %d cost statistics for %s (price: %.4f PLN/kWh)",
                len(cost_stats),
                cost_entity_id,
                price,
            )
            if adapter:
                adapter.import_cost_statistics(
                    statistic_id=cost_entity_id,
                    statistics=cost_stats,
                    name=cost_name,
                    unit="PLN",
                )
            else:
                cost_metadata = StatisticMetaData(
                    source="recorder",
                    statistic_id=cost_entity_id,
                    name=cost_name,
                    unit_of_measurement="PLN",
                    has_mean=False,
                    has_sum=True,
                    mean_type=StatisticMeanType.NONE,
                    unit_class=None,
                )
                async_import_statistics(self.hass, cost_metadata, cost_stats)

        if energy_stats and hasattr(self.coordinator, "async_request_synthetic_storage"):
            self.coordinator.async_request_synthetic_storage(self._meter_id)

        super()._handle_coordinator_update()


class EnergaInfoSensor(CoordinatorEntity, SensorEntity):
    """Info sensor showing static meter details (Address, Tariff, etc)."""

    def __init__(
        self,
        coordinator,
        meter_id: str,
        data_key: str,
        name: str,
        icon: str,
        device_info: DeviceInfo,
        device_class: str = None,
    ) -> None:
        """Initialize info sensor."""
        super().__init__(coordinator)

        self._meter_id = meter_id
        self._data_key = data_key

        # Entity attributes
        self._attr_name = name
        self._attr_unique_id = f"energa_{meter_id}_{data_key}_info"
        self._attr_has_entity_name = True
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attr_device_info = device_info

    @property
    def native_value(self):
        """Return the value from coordinator data."""
        if not self.coordinator.data:
            return None

        for meter in self.coordinator.data:
            if str(meter.get("meter_point_id")) == str(self._meter_id):
                return meter.get(self._data_key)
        return None


class EnergaCostStatisticsSensor(CoordinatorEntity, SensorEntity):
    """Pure placeholder entity for cost statistics.

    Required so that HA's state machine recognizes the statistic_id
    used by async_import_statistics in EnergaStatisticsSensor.
    This sensor does NOT import statistics itself — all cost import
    logic lives in EnergaStatisticsSensor._handle_coordinator_update.
    """

    def __init__(
        self,
        coordinator: EnergaCoordinator,
        meter_id: str,
        data_key: str,
        name: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
        serial: str = "",
    ) -> None:
        """Initialize cost statistics placeholder sensor."""
        super().__init__(coordinator)

        self._meter_id = meter_id
        self._data_key = data_key
        self._entry = entry

        # Entity attributes
        self._attr_name = name
        self._attr_unique_id = f"energa_{serial}_{data_key}_cost_stats"
        self._attr_has_entity_name = True

        # Force entity_id to match statistic_id used by EnergaStatisticsSensor
        suffix_to_name = {
            "import": "panel_energia_zuzycie",
            "import_1": "panel_energia_strefa_1",
            "import_2": "panel_energia_strefa_2",
            "export": "panel_energia_produkcja",
            "export_1": "panel_energia_produkcja_strefa_1",
            "export_2": "panel_energia_produkcja_strefa_2",
        }
        energy_slug = suffix_to_name.get(data_key, f"panel_{data_key}")
        self.entity_id = f"sensor.energa_{serial}_{energy_slug}_cost".lower()

        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = "PLN"

        # Device info
        self._attr_device_info = device_info

        # Icon
        self._attr_icon = (
            "mdi:currency-usd" if "import" in data_key else "mdi:piggy-bank"
        )

    @property
    def native_value(self):
        """Return None — cost data flows via async_import_statistics."""
        return None

    @property
    def available(self) -> bool:
        """Cost placeholder is available when coordinator has data."""
        return self.coordinator.data is not None


class EnergaSyntheticStatisticsSensor(CoordinatorEntity, SensorEntity):
    """Synthetic statistics sensor for virtual storage or net grid flows in Energy Dashboard (v1.6.0)."""

    def __init__(
        self,
        coordinator: EnergaCoordinator,
        meter_id: str,
        data_key: str,
        name: str,
        icon: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
        serial: str = "",
    ) -> None:
        """Initialize synthetic statistics sensor."""
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._data_key = data_key
        self._entry = entry
        self._serial = str(serial or meter_id)

        self._attr_name = name
        self._attr_unique_id = f"energa_{self._serial}_{data_key}_stats"
        self._attr_has_entity_name = True
        self.entity_id = f"sensor.energa_{self._serial}_{data_key}".lower()

        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_info = device_info
        self._attr_icon = icon

    @property
    def native_value(self):
        """Return None — energy statistics flow exclusively via async_import_statistics.

        Prevents Home Assistant Core's recorder from calculating competing
        statistics from states table deltas.
        """
        return None

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None


class EnergaAutoconsumptionSensor(CoordinatorEntity, SensorEntity):
    """Sensor for reporting hour-synchronized PV autoconsumption metrics."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnergaCoordinator,
        meter_id: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
        metric_key: str,
        name: str,
        icon: str,
        unit: str | None = "kWh",
        device_class: SensorDeviceClass | None = SensorDeviceClass.ENERGY,
        serial: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._meter_id = str(meter_id)
        self._entry = entry
        self._metric_key = metric_key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_unique_id = f"energa_{meter_id}_autoconsumption_{metric_key}"
        self._attr_device_info = device_info
        self._attr_state_class = None

    @property
    def native_value(self) -> float | None:
        summary = self.coordinator._autoconsumption_summary.get(self._meter_id)
        if summary is None:
            return None
        return getattr(summary, self._metric_key, None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        summary = self.coordinator._autoconsumption_summary.get(self._meter_id)
        if not summary:
            return {
                "status": "waiting_for_sync",
                "inverter_entity": self._entry.options.get(CONF_INVERTER_ENERGY_ENTITY, ""),
            }
        attrs: dict[str, Any] = {
            "synced_hours_count": summary.synced_hours_count,
            "synced_until": summary.synced_until.isoformat() if summary.synced_until else None,
            "inverter_entity": self._entry.options.get(CONF_INVERTER_ENERGY_ENTITY, ""),
        }
        if self._metric_key in ("today_kwh", "today_home_consumption_kwh"):
            attrs.update({
                "today_autoconsumption_kwh": summary.today_kwh,
                "today_home_consumption_kwh": summary.today_home_consumption_kwh,
                "savings_today_pln": summary.savings_today_pln,
            })
        elif self._metric_key in ("yesterday_kwh", "yesterday_home_consumption_kwh"):
            attrs.update({
                "yesterday_autoconsumption_kwh": summary.yesterday_kwh,
                "yesterday_home_consumption_kwh": summary.yesterday_home_consumption_kwh,
                "savings_yesterday_pln": summary.savings_yesterday_pln,
            })
        elif self._metric_key in ("mtd_kwh", "mtd_home_consumption_kwh", "savings_mtd_pln", "autoconsumption_ratio_mtd", "self_sufficiency_ratio_mtd"):
            attrs.update({
                "mtd_autoconsumption_kwh": summary.mtd_kwh,
                "mtd_home_consumption_kwh": summary.mtd_home_consumption_kwh,
                "mtd_pv_kwh": summary.mtd_pv_kwh,
                "autoconsumption_ratio_mtd": summary.autoconsumption_ratio_mtd,
                "self_sufficiency_ratio_mtd": summary.self_sufficiency_ratio_mtd,
                "savings_mtd_pln": summary.savings_mtd_pln,
            })
        return attrs




