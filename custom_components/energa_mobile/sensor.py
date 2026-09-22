"""Sensor platform for Energa My Meter.

Clean rebuild based on thedeemling/hass-energa-my-meter architecture.
Implements invisible statistics sensors for Energy Dashboard integration.
Supports multi-zone tariffs (G12w: strefa 1 + strefa 2).
"""

import asyncio
import logging
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.loader import async_get_integration

from .const import (
    CONF_ENABLE_AUTO_SETTLEMENT,
    CONF_ENABLE_SYNTHETIC_STORAGE,
    CONF_INVERTER_ENERGY_ENTITY,
    CONF_PROSUMER_COEFFICIENT,
    DEFAULT_ENABLE_AUTO_SETTLEMENT,
    DEFAULT_ENABLE_SYNTHETIC_STORAGE,
    DEFAULT_PROSUMER_COEFFICIENT,
    DOMAIN,
    get_prosumer_coefficient,
)
from .coordinator import EnergaCoordinator
from .sensors.bank import (
    EnergaBankFlowSensor,
    EnergaBankKwhSensor,
    EnergaBankLevelSensor,
    EnergaBankPlnSensor,
    EnergaBankZoneSensor,
    _fifo_bank_from_monthly,
)
from .sensors.bill import (
    EnergaBillComponentSensor,
    EnergaBillCurrentSensor,
    EnergaBillForecastSensor,
)
from .sensors.completeness import EnergaPeriodCompletenessSensor
from .sensors.live import (
    EnergaAutoconsumptionSensor,
    EnergaCostStatisticsSensor,
    EnergaDataQualitySensor,
    EnergaFirstDataDateSensor,
    EnergaInfoSensor,
    EnergaLiveSensor,
    EnergaProsumerBalanceSensor,
    EnergaStatisticsSensor,
    EnergaSyntheticStatisticsSensor,
)
from .sensors.period import EnergaPeriodVerificationSensor
from .sensors.price import (
    EnergaPriceSensor,
    EnergaRceSensor,
    PseRceArbitrageSpreadSensor,
    PseRceDynamicPriceSensor,
)
from .settlement import (
    is_export_prosumer,
    orphan_bank_uids,
    orphan_removed_uids,
)
from .tariff import compute_bill

__all__ = [
    "EnergaAutoconsumptionSensor",
    "EnergaBankFlowSensor",
    "EnergaBankKwhSensor",
    "EnergaBankLevelSensor",
    "EnergaBankPlnSensor",
    "EnergaBankZoneSensor",
    "EnergaBillComponentSensor",
    "EnergaBillCurrentSensor",
    "EnergaBillForecastSensor",
    "EnergaCoordinator",
    "EnergaCostStatisticsSensor",
    "EnergaDataQualitySensor",
    "EnergaFirstDataDateSensor",
    "EnergaInfoSensor",
    "EnergaLiveSensor",
    "EnergaPriceSensor",
    "EnergaProsumerBalanceSensor",
    "EnergaRceSensor",
    "EnergaStatisticsSensor",
    "EnergaSyntheticStatisticsSensor",
    "EnergaPeriodCompletenessSensor",
    "EnergaPeriodVerificationSensor",
    "PseRceArbitrageSpreadSensor",
    "PseRceDynamicPriceSensor",
    "_fifo_bank_from_monthly",
    "async_setup_entry",
    "compute_bill",
]

_LOGGER = logging.getLogger(__name__)

# Timezone for Energa data
TIMEZONE = ZoneInfo("Europe/Warsaw")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Energa sensors from config entry."""
    api = hass.data[DOMAIN][entry.entry_id]["api"]

    # Get integration version for device info
    integration = await async_get_integration(hass, DOMAIN)
    sw_version = str(integration.version)  # Must be string for AwesomeVersion

    # Get storage instance
    storage = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("storage")

    # Get or create coordinator
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("coordinator")
    if not coordinator:
        coordinator = EnergaCoordinator(hass, api, entry, storage=storage)
        hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["coordinator"] = coordinator
        try:
            await coordinator.async_config_entry_first_refresh()
            _LOGGER.debug("Energa: Initial refresh successful")
        except Exception as err:
            _LOGGER.warning("Energa: Initial fetch failed, will retry: %s", err)

    # Prefer the data already fetched by ``async_config_entry_first_refresh``
    # in ``__init__.py`` (awaited before platforms). Calling the API here again
    # is unnecessary, and the cache read (``force_refresh=False``) still grabs
    # ``api._data_lock`` — during a concurrent coordinator refresh that lock is
    # held for the whole meter+chart fetch, which used to push sensor platform
    # setup over HA's 10 s warning on agrestowa. The API is only consulted as a
    # fallback when the coordinator genuinely has no data.
    meters_list = coordinator.data
    if not meters_list:
        try:
            meters_list = await api.async_get_data(force_refresh=False)
        except Exception as err:
            _LOGGER.error("Energa: Failed to fetch meters for setup: %s", err)
            meters_list = []
    _LOGGER.info(
        "Energa: Using %d meters for sensor setup",
        len(meters_list) if meters_list else 0,
    )

    # Filter active meters (total_plus > 0)
    meters_to_process = (
        [
            m
            for m in meters_list
            if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
        ]
        if meters_list
        else []
    )

    _LOGGER.info(
        "Energa: Creating sensors for %d active meters", len(meters_to_process)
    )

    # Create sensors for each meter
    sensors = []

    for meter in meters_to_process:
        meter_id = meter["meter_point_id"]
        serial = meter.get("meter_serial", meter_id)
        meter_name = meter.get("name") or ""
        ppe = meter.get("ppe", meter_id)
        has_zones = meter.get("zone_count", 1) > 1

        device_info = DeviceInfo(
            identifiers={(DOMAIN, str(serial))},
            name=f"Energa {meter.get('name') or serial}",
            manufacturer="Energa-Operator",
            model=f"PPE: {ppe}",
            configuration_url="https://mojlicznik.energa-operator.pl",
            sw_version=sw_version,
        )

        # === LIVE SENSORS ===

        # 1. Total Import (Grid consumption - lifetime counter)
        sensors.append(
            EnergaLiveSensor(
                coordinator=coordinator,
                meter_id=meter_id,
                data_key="total_plus",
                name="Stan Licznika Import",
                icon="mdi:counter",
                device_info=device_info,
            )
        )

        # 1b. Zone-specific Import totals for G12w (#29)
        if has_zones:
            sensors.append(
                EnergaLiveSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="total_plus_1",
                    name="Stan Licznika Import Strefa 1",
                    icon="mdi:counter",
                    device_info=device_info,
                )
            )
            sensors.append(
                EnergaLiveSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="total_plus_2",
                    name="Stan Licznika Import Strefa 2",
                    icon="mdi:counter",
                    device_info=device_info,
                )
            )

        # 2. Total Export (Production to grid - lifetime counter)
        if meter.get("total_minus") is not None or is_export_prosumer(meter):
            sensors.append(
                EnergaLiveSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="total_minus",
                    name="Stan Licznika Export",
                    icon="mdi:counter",
                    device_info=device_info,
                )
            )

        # 2b. Zone-specific Export totals for G12w prosumers (#29)
        if has_zones and (meter.get("total_minus") is not None or is_export_prosumer(meter)):
            sensors.append(
                EnergaLiveSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="total_minus_1",
                    name="Stan Licznika Export Strefa 1",
                    icon="mdi:counter",
                    device_info=device_info,
                )
            )
            sensors.append(
                EnergaLiveSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="total_minus_2",
                    name="Stan Licznika Export Strefa 2",
                    icon="mdi:counter",
                    device_info=device_info,
                )
            )

        # 3. Daily Import (Today's consumption - resets at midnight)
        sensors.append(
            EnergaLiveSensor(
                coordinator=coordinator,
                meter_id=meter_id,
                data_key="daily_pobor",
                name="Zużycie Dziś",
                icon="mdi:flash",
                device_info=device_info,
                state_class_override=SensorStateClass.TOTAL_INCREASING,
            )
        )

        # 4. Daily Export (Today's production - resets at midnight)
        if is_export_prosumer(meter):
            sensors.append(
                EnergaLiveSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="daily_produkcja",
                    name="Produkcja Dziś",
                    icon="mdi:solar-power",
                    device_info=device_info,
                    state_class_override=SensorStateClass.TOTAL_INCREASING,
                )
            )

        # === STATISTICS SENSORS (for Energy Dashboard) ===

        if has_zones:
            # G12w: Two zone-specific statistics sensors
            _LOGGER.info(
                "Energa: Creating zone-specific stats sensors for meter %s (G12w)",
                serial,
            )

            # Strefa 1 (droga)
            sensors.append(
                EnergaStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="import_1",
                    name="Panel Energia Strefa 1",
                    device_info=device_info,
                    entry=entry,
                )
            )
            sensors.append(
                EnergaCostStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="import_1",
                    name="Panel Energia Strefa 1 Koszt",
                    device_info=device_info,
                    entry=entry,
                    serial=serial,
                )
            )

            # Strefa 2 (tania)
            sensors.append(
                EnergaStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="import_2",
                    name="Panel Energia Strefa 2",
                    device_info=device_info,
                    entry=entry,
                )
            )
            sensors.append(
                EnergaCostStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="import_2",
                    name="Panel Energia Strefa 2 Koszt",
                    device_info=device_info,
                    entry=entry,
                    serial=serial,
                )
            )
        else:
            # Single-zone tariff: one statistics sensor
            sensors.append(
                EnergaStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="import",
                    name="Panel Energia Zużycie",
                    device_info=device_info,
                    entry=entry,
                )
            )
            sensors.append(
                EnergaCostStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="import",
                    name="Panel Energia Zużycie Koszt",
                    device_info=device_info,
                    entry=entry,
                    serial=serial,
                )
            )

        # Export statistics
        # v0.3.0: energy stats only (solar wiring for old net-metering,
        # grid return for new net-billing). Cost stats are NOT created:
        # old system sells nothing, new system pays live RCEm×1.23 via
        # the Cena Oddania price entity (a frozen 0.95 stat would lie).
        if is_export_prosumer(meter) and has_zones:
            # Per-zone export for G12w
            sensors.append(
                EnergaStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="export_1",
                    name="Panel Energia Produkcja Strefa 1",
                    device_info=device_info,
                    entry=entry,
                )
            )
            sensors.append(
                EnergaStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="export_2",
                    name="Panel Energia Produkcja Strefa 2",
                    device_info=device_info,
                    entry=entry,
                )
            )
        elif is_export_prosumer(meter):
            sensors.append(
                EnergaStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="export",
                    name="Panel Energia Produkcja",
                    device_info=device_info,
                    entry=entry,
                )
            )

        # === PROSUMER & BANK SENSORS ===
        # Auto-detect old (net-metering, coeff >= 0.7) vs new (net-billing, coeff < 0.7)
        if is_export_prosumer(meter):
            coeff = get_prosumer_coefficient(
                entry.options, str(meter_id), serial=str(serial)
            )
            is_old_system = coeff >= 0.7  # 0.8 or 0.7 = old net-metering

            if is_old_system:
                # Old net-metering: virtual warehouse in kWh, bilans, fill level, and battery flows
                sensors.append(
                    EnergaProsumerBalanceSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        has_zones=has_zones,
                        serial=serial,
                    )
                )
                sensors.append(
                    EnergaBankKwhSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        has_zones=has_zones,
                        serial=serial,
                    )
                )
                if has_zones:
                    sensors.append(
                        EnergaBankZoneSensor(
                            coordinator=coordinator,
                            meter_id=meter_id,
                            device_info=device_info,
                            entry=entry,
                            zone=1,
                            serial=serial,
                        )
                    )
                    sensors.append(
                        EnergaBankZoneSensor(
                            coordinator=coordinator,
                            meter_id=meter_id,
                            device_info=device_info,
                            entry=entry,
                            zone=2,
                            serial=serial,
                        )
                    )
                # v0.3.0: warehouse fill level % (needs FIFO history;
                # unknown until ~11 months of statistics exist).
                sensors.append(
                    EnergaBankLevelSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        serial=serial,
                    )
                )
                # Native bank flows (Energy battery, live stock)
                for direction in ("charge", "discharge"):
                    sensors.append(
                        EnergaBankFlowSensor(
                            coordinator=coordinator,
                            meter_id=meter_id,
                            device_info=device_info,
                            entry=entry,
                            has_zones=has_zones,
                            direction=direction,
                            serial=serial,
                        )
                    )

                # Synthetic virtual storage & grid sensors for Energy Dashboard (v1.6.0)
                enable_synth = entry.options.get(
                    CONF_ENABLE_SYNTHETIC_STORAGE, DEFAULT_ENABLE_SYNTHETIC_STORAGE
                )
                if enable_synth:
                    if has_zones:
                        synth_defs = [
                            ("syntetyczny_magazyn_l1_ladowanie", "Syntetyczny Magazyn L1 Ładowanie", "mdi:battery-charging"),
                            ("syntetyczny_magazyn_l1_rozladowanie", "Syntetyczny Magazyn L1 Rozładowanie", "mdi:battery-arrow-down"),
                            ("syntetyczny_magazyn_l2_ladowanie", "Syntetyczny Magazyn L2 Ładowanie", "mdi:battery-charging"),
                            ("syntetyczny_magazyn_l2_rozladowanie", "Syntetyczny Magazyn L2 Rozładowanie", "mdi:battery-arrow-down"),
                            ("syntetyczna_siec_oddanie_strefa_1", "Syntetyczna Sieć Oddanie Strefa 1", "mdi:transmission-tower-export"),
                            ("syntetyczna_siec_oddanie_strefa_2", "Syntetyczna Sieć Oddanie Strefa 2", "mdi:transmission-tower-export"),
                            ("syntetyczna_siec_pobor_strefa_1", "Syntetyczna Sieć Pobór Strefa 1", "mdi:transmission-tower-import"),
                            ("syntetyczna_siec_pobor_strefa_2", "Syntetyczna Sieć Pobór Strefa 2", "mdi:transmission-tower-import"),
                        ]
                    else:
                        synth_defs = [
                            ("syntetyczny_magazyn_ladowanie", "Syntetyczny Magazyn Ładowanie", "mdi:battery-charging"),
                            ("syntetyczny_magazyn_rozladowanie", "Syntetyczny Magazyn Rozładowanie", "mdi:battery-arrow-down"),
                            ("syntetyczna_siec_oddanie", "Syntetyczna Sieć Oddanie", "mdi:transmission-tower-export"),
                            ("syntetyczna_siec_pobor", "Syntetyczna Sieć Pobór", "mdi:transmission-tower-import"),
                        ]
                    for s_key, s_name, s_icon in synth_defs:
                        sensors.append(
                            EnergaSyntheticStatisticsSensor(
                                coordinator=coordinator,
                                meter_id=meter_id,
                                data_key=s_key,
                                name=s_name,
                                icon=s_icon,
                                device_info=device_info,
                                entry=entry,
                                serial=serial,
                            )
                        )
            else:
                # New net-billing: monetary deposit in PLN, RCEm auto-fetch
                # (No virtual battery/warehouse or kWh bilans in net-billing)
                sensors.append(
                    EnergaBankPlnSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        has_zones=has_zones,
                        serial=serial,
                    )
                )
                sensors.append(
                    EnergaRceSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        api=api,
                        serial=serial,
                    )
                )

        # === HISTORY WINDOW START (v0.3.2: every meter, not just prosumers) ===
        sensors.append(
            EnergaFirstDataDateSensor(
                coordinator=coordinator,
                meter_id=meter_id,
                device_info=device_info,
                entry=entry,
                serial=serial,
            )
        )

        # === BILL FORECAST (v0.2.17 / v1.5.0: every meter, active by default) ===
        # New net-billing: deposit lowers the payable. Old net-metering:
        # warehouse coverage lowers the energy charge. Plain consumers:
        # full import bill (export 0, cover 0) — same compute_bill math.
        if entry.options.get(CONF_ENABLE_AUTO_SETTLEMENT, True) is not False:
            sensors.append(
                EnergaBillForecastSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    device_info=device_info,
                    entry=entry,
                    has_zones=has_zones,
                    serial=serial,
                )
            )
            sensors.append(
                EnergaBillCurrentSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    device_info=device_info,
                    entry=entry,
                    has_zones=has_zones,
                    serial=serial,
                )
            )
            # Dedicated breakdown sensors (v1.0.4)
            sensors.append(
                EnergaBillComponentSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    device_info=device_info,
                    entry=entry,
                    component_key="brutto",
                    name="Koszt Brutto MTD",
                    icon="mdi:receipt-text-outline",
                    has_zones=has_zones,
                    serial=serial,
                )
            )
            sensors.append(
                EnergaBillComponentSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    device_info=device_info,
                    entry=entry,
                    component_key="sale_total",
                    name="Koszt Energii Czynnej MTD",
                    icon="mdi:flash-outline",
                    has_zones=has_zones,
                    serial=serial,
                )
            )
            sensors.append(
                EnergaBillComponentSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    device_info=device_info,
                    entry=entry,
                    component_key="distr_total",
                    name="Koszt Dystrybucji MTD",
                    icon="mdi:transmission-tower",
                    has_zones=has_zones,
                    serial=serial,
                )
            )


            # MTD energy volume breakdown sensors (v1.0.8)
            if has_zones:
                sensors.append(
                    EnergaBillComponentSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        component_key="energy_import_1",
                        name="Pobór Energii Strefa 1 MTD",
                        icon="mdi:transmission-tower",
                        unit="kWh",
                        device_class=SensorDeviceClass.ENERGY,
                        has_zones=has_zones,
                        serial=serial,
                    )
                )
                sensors.append(
                    EnergaBillComponentSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        component_key="energy_import_2",
                        name="Pobór Energii Strefa 2 MTD",
                        icon="mdi:transmission-tower",
                        unit="kWh",
                        device_class=SensorDeviceClass.ENERGY,
                        has_zones=has_zones,
                        serial=serial,
                    )
                )
                if is_export_prosumer(meter):
                    sensors.append(
                        EnergaBillComponentSensor(
                            coordinator=coordinator,
                            meter_id=meter_id,
                            device_info=device_info,
                            entry=entry,
                            component_key="energy_export_1",
                            name="Oddanie Energii Strefa 1 MTD",
                            icon="mdi:solar-power",
                            unit="kWh",
                            device_class=SensorDeviceClass.ENERGY,
                            has_zones=has_zones,
                            serial=serial,
                        )
                    )
                    sensors.append(
                        EnergaBillComponentSensor(
                            coordinator=coordinator,
                            meter_id=meter_id,
                            device_info=device_info,
                            entry=entry,
                            component_key="energy_export_2",
                            name="Oddanie Energii Strefa 2 MTD",
                            icon="mdi:solar-power",
                            unit="kWh",
                            device_class=SensorDeviceClass.ENERGY,
                            has_zones=has_zones,
                            serial=serial,
                        )
                    )
            else:
                sensors.append(
                    EnergaBillComponentSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        component_key="energy_import",
                        name="Pobór Energii MTD",
                        icon="mdi:transmission-tower",
                        unit="kWh",
                        device_class=SensorDeviceClass.ENERGY,
                        has_zones=has_zones,
                        serial=serial,
                    )
                )
                if is_export_prosumer(meter):
                    sensors.append(
                        EnergaBillComponentSensor(
                            coordinator=coordinator,
                            meter_id=meter_id,
                            device_info=device_info,
                            entry=entry,
                            component_key="energy_export",
                            name="Oddanie Energii MTD",
                            icon="mdi:solar-power",
                            unit="kWh",
                            device_class=SensorDeviceClass.ENERGY,
                            has_zones=has_zones,
                            serial=serial,
                        )
                    )
            if is_export_prosumer(meter):
                coeff = float(
                    entry.options.get(
                        CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT
                    )
                )
                if coeff < 0.7:
                    # Net-billing: deposit generated & applied in PLN
                    sensors.append(
                        EnergaBillComponentSensor(
                            coordinator=coordinator,
                            meter_id=meter_id,
                            device_info=device_info,
                            entry=entry,
                            component_key="deposit",
                            name="Depozyt Wygenerowany MTD",
                            icon="mdi:solar-power-variant",
                            has_zones=has_zones,
                            serial=serial,
                        )
                    )
                    sensors.append(
                        EnergaBillComponentSensor(
                            coordinator=coordinator,
                            meter_id=meter_id,
                            device_info=device_info,
                            entry=entry,
                            component_key="deposit_applied",
                            name="Odzyskano z Depozytu MTD",
                            icon="mdi:cash-minus",
                            has_zones=has_zones,
                            serial=serial,
                        )
                    )
                else:
                    # Net-metering: warehouse coverage in kWh
                    sensors.append(
                        EnergaBillComponentSensor(
                            coordinator=coordinator,
                            meter_id=meter_id,
                            device_info=device_info,
                            entry=entry,
                            component_key="cover_day",
                            name="Pokrycie z Magazynu Dzień MTD",
                            icon="mdi:weather-sunny",
                            unit="kWh",
                            device_class=SensorDeviceClass.ENERGY,
                            has_zones=has_zones,
                            serial=serial,
                        )
                    )
                    if has_zones:
                        sensors.append(
                            EnergaBillComponentSensor(
                                coordinator=coordinator,
                                meter_id=meter_id,
                                device_info=device_info,
                                entry=entry,
                                component_key="cover_night",
                                name="Pokrycie z Magazynu Noc MTD",
                                icon="mdi:weather-night",
                                unit="kWh",
                                device_class=SensorDeviceClass.ENERGY,
                                has_zones=has_zones,
                                serial=serial,
                            )
                        )

            # === PERIOD VERIFICATION RESULT (Faza 2) ===
            # State = do_zaplaty from the latest "Przelicz okres
            # rozliczeniowy" run; full breakdown in attributes. No energy
            # statistics (deliberate).
            sensors.append(
                EnergaPeriodVerificationSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    serial=serial,
                    device_info=device_info,
                    entry=entry,
                )
            )

            # Completeness status gates the "Przelicz okres" button: the
            # invoice may only be computed once the window has no gaps.
            sensors.append(
                EnergaPeriodCompletenessSensor(
                    coordinator=coordinator,
                    meter=meter,
                    meter_id=meter_id,
                    serial=serial,
                    device_info=device_info,
                    entry=entry,
                )
            )

            # Dynamic PSE RCE and BESS Arbitrage Spread sensors (Etap 5)
            sensors.append(
                PseRceDynamicPriceSensor(
                    coordinator=coordinator,
                    entry=entry,
                    meter_point_id=meter_id,
                    meter_serial=serial,
                    meter_name=meter_name,
                )
            )
            sensors.append(
                PseRceArbitrageSpreadSensor(
                    coordinator=coordinator,
                    entry=entry,
                    meter_point_id=meter_id,
                    meter_serial=serial,
                    meter_name=meter_name,
                )
            )

            # Hour-Synchronized PV Autoconsumption & Microgrid sensors (v1.6.9: only when inverter entity configured)
            if entry.options.get(CONF_INVERTER_ENERGY_ENTITY) and is_export_prosumer(meter):
                autoconsumption_specs = [
                    ("today_kwh", "Autokonsumpcja Dziś", "mdi:solar-power-variant", "kWh", SensorDeviceClass.ENERGY),
                    ("yesterday_kwh", "Autokonsumpcja Wczoraj", "mdi:solar-power", "kWh", SensorDeviceClass.ENERGY),
                    ("mtd_kwh", "Autokonsumpcja MTD", "mdi:home-lightning-bolt", "kWh", SensorDeviceClass.ENERGY),
                    ("autoconsumption_ratio_mtd", "Stopień Autokonsumpcji MTD", "mdi:percent-outline", "%", None),
                    ("self_sufficiency_ratio_mtd", "Samowystarczalność Energetyczna MTD", "mdi:home-battery", "%", None),
                    ("today_home_consumption_kwh", "Realne Zużycie Domu Dziś", "mdi:home-clock", "kWh", SensorDeviceClass.ENERGY),
                    ("mtd_home_consumption_kwh", "Realne Zużycie Domu MTD", "mdi:home-analytics", "kWh", SensorDeviceClass.ENERGY),
                    ("savings_mtd_pln", "Oszczędność Autokonsumpcja MTD", "mdi:piggy-bank-outline", "PLN", SensorDeviceClass.MONETARY),
                ]
                for m_key, m_name, m_icon, m_unit, m_devclass in autoconsumption_specs:
                    sensors.append(
                        EnergaAutoconsumptionSensor(
                            coordinator=coordinator,
                            meter_id=meter_id,
                            device_info=device_info,
                            entry=entry,
                            metric_key=m_key,
                            name=m_name,
                            icon=m_icon,
                            unit=m_unit,
                            device_class=m_devclass,
                            serial=serial,
                        )
                    )

        # === PRICE SENSORS (F1: v4.14) ===


        if has_zones:
            price_keys = [
                ("import_1", "Cena Poboru Strefa 1", "mdi:cash-multiple"),
                ("import_2", "Cena Poboru Strefa 2", "mdi:cash-multiple"),
            ]
        else:
            price_keys = [
                ("import", "Cena Poboru", "mdi:cash-multiple"),
            ]

        if is_export_prosumer(meter):
            price_keys.append(("export", "Cena Oddania", "mdi:cash-refund"))
            price_keys.append(
                ("coefficient", "Współczynnik Prosumencki", "mdi:percent")
            )

        for p_key, p_name, p_icon in price_keys:
            sensors.append(
                EnergaPriceSensor(
                    coordinator=coordinator,
                    data_key=p_key,
                    name=p_name,
                    icon=p_icon,
                    device_info=device_info,
                    entry=entry,
                    serial=serial,
                    meter_id=meter_id,
                )
            )

        # === INFO SENSORS ===

        info_types = [
            ("address", "Adres", "mdi:map-marker", None),
            ("tariff", "Taryfa", "mdi:cash-multiple", None),
            ("ppe", "PPE", "mdi:identifier", None),
            ("meter_serial", "Numer Licznika", "mdi:counter", None),
            ("contract_date", "Data Aktywacji", "mdi:calendar", None),
        ]

        for key, name, icon, device_class in info_types:
            if meter.get(key):
                sensors.append(
                    EnergaInfoSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        data_key=key,
                        name=f"{name}",
                        icon=icon,
                        device_info=device_info,
                        device_class=device_class,
                    )
                )

        # 16. Data Quality & Freshness Sensor
        sensors.append(
            EnergaDataQualitySensor(
                coordinator=coordinator,
                meter_id=meter_id,
                name="Jakość danych",
                icon="mdi:check-network-outline",
                device_info=device_info,
                storage=storage,
                ppe=str(ppe),
                serial=str(serial),
                tariff=str(meter.get("tariff") or "G11"),
            )
        )

    # === CLEANUP CONSUMER LEFTOVERS (v0.2.15+) + v0.3.0 REMOVALS ===
    # Consumer meters (no export) no longer get prosumer sensors, and
    # prosumer meters drop the bank of the inactive settlement system
    # (v0.2.19). v0.3.0 also drops the Wykryj button (auto-backfill) and
    # export cost placeholders (live RCEm pricing). Remove orphans so
    # they don't linger as unavailable (replaces the manual jq
    # entity_registry cleanup from v0.2.10).
    try:
        from homeassistant.helpers import entity_registry as er

        _doomed: set = set()
        for _m in meters_to_process:
            _pros = is_export_prosumer(_m)
            _mid_str = str(_m["meter_point_id"])
            _ser_str = str(_m.get("meter_serial", _mid_str))
            _coeff = get_prosumer_coefficient(entry.options, _mid_str, serial=_ser_str)
            _doomed.update(
                orphan_bank_uids(
                    str(_m["meter_point_id"]),
                    str(_m.get("meter_serial", _m["meter_point_id"])),
                    _pros,
                    _coeff,
                )
            )
            _doomed.update(
                orphan_removed_uids(
                    str(_m["meter_point_id"]),
                    str(_m.get("meter_serial", _m["meter_point_id"])),
                )
            )
        _LOGGER.info("Energa cleanup for entry %s: doomed=%s", entry.entry_id, _doomed)
        if _doomed:
            _ent_reg = er.async_get(hass)
            for _ent in list(_ent_reg.entities.values()):
                if (
                    _ent.platform == DOMAIN
                    and _ent.config_entry_id == entry.entry_id
                    and (_ent.unique_id or "") in _doomed
                ):
                    _LOGGER.info(
                        "Removing consumer leftover %s (uid=%s)", _ent.entity_id, _ent.unique_id
                    )
                    _ent_reg.async_remove(_ent.entity_id)
    except Exception as err:
        _LOGGER.error("Consumer leftover cleanup failed: %s", err, exc_info=True)

    # === ENTITY REGISTRY MIGRATION (v1.0.6: Option A Standard Canonical Names) ===
    # Smoothly migrate legacy entity IDs from earlier versions to the official
    # device-scoped canonical entity IDs (sensor.energa_{serial}_{slug})
    # preserving recorder history, statistics, and automations.
    try:
        from homeassistant.helpers import entity_registry as er

        _ent_reg = er.async_get(hass)
        _canon_map = {}
        for _m in meters_to_process:
            _mid = str(_m["meter_point_id"])
            _serial = str(_m.get("meter_serial", _mid))

            _canon_map.update({
                f"energa_{_mid}_prosumer_balance": f"sensor.energa_{_serial}_bilans_prosumencki",
                f"energa_{_mid}_bank_kwh": f"sensor.energa_{_serial}_bank_wirtualny_kwh",
                f"energa_{_mid}_bank_pln": f"sensor.energa_{_serial}_bank_wirtualny_pln",
                f"energa_{_mid}_bank_level": f"sensor.energa_{_serial}_magazyn_poziom",
                f"energa_{_mid}_bank_charge": f"sensor.energa_{_serial}_bank_ladowanie",
                f"energa_{_mid}_bank_discharge": f"sensor.energa_{_serial}_bank_rozladowanie",
                f"energa_{_mid}_first_data_date": f"sensor.energa_{_serial}_data_pierwszego_odczytu",
                f"energa_{_mid}_rcem_auto": f"sensor.energa_{_serial}_rcem_auto",
                f"energa_{_mid}_bill_forecast": f"sensor.energa_{_serial}_prognoza_rachunku",
                f"energa_{_mid}_bill_current": f"sensor.energa_{_serial}_dotychczasowy_rachunek",
                f"energa_{_mid}_mtd_brutto": f"sensor.energa_{_serial}_koszt_brutto_mtd",
                f"energa_{_mid}_mtd_sale_total": f"sensor.energa_{_serial}_koszt_energii_czynnej_mtd",
                f"energa_{_mid}_mtd_distr_total": f"sensor.energa_{_serial}_koszt_dystrybucji_mtd",
                f"energa_{_mid}_mtd_deposit": f"sensor.energa_{_serial}_depozyt_wygenerowany_mtd",
                f"energa_{_mid}_mtd_deposit_applied": f"sensor.energa_{_serial}_odzyskano_z_depozytu_mtd",
                f"energa_{_mid}_mtd_cover_day": f"sensor.energa_{_serial}_pokrycie_z_magazynu_dzien_mtd",
                f"energa_{_mid}_mtd_cover_night": f"sensor.energa_{_serial}_pokrycie_z_magazynu_noc_mtd",
                f"energa_{_mid}_mtd_energy_import": f"sensor.energa_{_serial}_pobor_energii_mtd",
                f"energa_{_mid}_mtd_energy_export": f"sensor.energa_{_serial}_oddanie_energii_mtd",
                f"energa_{_mid}_mtd_energy_import_1": f"sensor.energa_{_serial}_pobor_energii_strefa_1_mtd",
                f"energa_{_mid}_mtd_energy_import_2": f"sensor.energa_{_serial}_pobor_energii_strefa_2_mtd",
                f"energa_{_mid}_bank_kwh_l1": f"sensor.energa_{_serial}_bank_wirtualny_l1_dzien_kwh",
                f"energa_{_mid}_bank_kwh_l2": f"sensor.energa_{_serial}_bank_wirtualny_l2_noc_kwh",
                f"energa_{_mid}_autoconsumption_today_kwh": f"sensor.energa_{_serial}_autokonsumpcja_dzis",
                f"energa_{_mid}_autoconsumption_yesterday_kwh": f"sensor.energa_{_serial}_autokonsumpcja_wczoraj",
                f"energa_{_mid}_autoconsumption_mtd_kwh": f"sensor.energa_{_serial}_autokonsumpcja_mtd",
                f"energa_{_mid}_autoconsumption_autoconsumption_ratio_mtd": f"sensor.energa_{_serial}_stopien_autokonsumpcji_mtd",
                f"energa_{_mid}_autoconsumption_self_sufficiency_ratio_mtd": f"sensor.energa_{_serial}_samowystarczalnosc_energetyczna_mtd",
                f"energa_{_mid}_autoconsumption_today_home_consumption_kwh": f"sensor.energa_{_serial}_realne_zuzycie_domu_dzis",
                f"energa_{_mid}_autoconsumption_mtd_home_consumption_kwh": f"sensor.energa_{_serial}_realne_zuzycie_domu_mtd",
                f"energa_{_mid}_autoconsumption_savings_mtd_pln": f"sensor.energa_{_serial}_oszczednosc_autokonsumpcja_mtd",
                f"energa_{_serial}_create_dashboard": f"button.energa_{_serial}_utworz_pulpit_rozliczen",
            })

        for _ent in list(_ent_reg.entities.values()):
            if _ent.platform == DOMAIN and _ent.config_entry_id == entry.entry_id:
                if _ent.unique_id in _canon_map:
                    _target = _canon_map[_ent.unique_id]
                    if _ent.entity_id != _target:
                        _existing_target = _ent_reg.async_get(_target)
                        if _existing_target and _existing_target.unique_id != _ent.unique_id:
                            _LOGGER.warning(
                                "Cannot migrate %s to %s: target entity_id already occupied by %s",
                                _ent.entity_id, _target, _existing_target.unique_id
                            )
                        else:
                            _LOGGER.info(
                                "Migrating entity %s -> %s (uid=%s)",
                                _ent.entity_id, _target, _ent.unique_id
                            )
                            _ent_reg.async_update_entity(_ent.entity_id, new_entity_id=_target)
    except Exception as err:
        _LOGGER.debug("Entity registry canonical migration skipped: %s", err)

    _LOGGER.info("Created %d Energa sensors", len(sensors))
    _LOGGER.debug(
        "Energa: Sensor list: %s",
        [
            s.entity_id if hasattr(s, "entity_id") else s._attr_unique_id
            for s in sensors
        ],
    )
    # ``update_before_add=False`` is deliberate: with ``True`` HA calls
    # ``CoordinatorEntity.async_update()`` for every entity, which awaits
    # ``coordinator.async_request_refresh()``. The debouncer then runs one more
    # full API refresh (~10 s) that the platform setup awaits, tripping HA's
    # "Setup of sensor platform ... is taking over 10 seconds" warning. Initial
    # states are still written by ``Entity.add_to_platform_finish``.
    async_add_entities(sensors, update_before_add=False)

    # Ensure post-startup settlement calibration runs once recorder and entities are ready
    if entry.options.get(
        CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT
    ):
        async def _async_delayed_settlement_calibration():
            for delay in (2, 8, 20):
                await asyncio.sleep(delay)
                _LOGGER.debug(
                    "Energa: running delayed settlement calibration (%ds after startup)",
                    delay,
                )
                await coordinator.async_refresh_settlement(notify=True)

        entry.async_create_background_task(
            hass,
            _async_delayed_settlement_calibration(),
            "energa_delayed_settlement_calibration",
        )

    # === CLEANUP STALE DEVICES ===
    # Remove devices for meters no longer returned by the API
    # (e.g., after user switches Energa account)
    active_serials = {
        str(m.get("meter_serial", m["meter_point_id"]))
        for m in meters_to_process
    }

    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        for identifier in device.identifiers:
            if identifier[0] == DOMAIN and identifier[1] not in active_serials:
                _LOGGER.info(
                    "Removing stale device %s (%s) — meter no longer in API",
                    device.name,
                    identifier[1],
                )
                dev_reg.async_remove_device(device.id)
                break


