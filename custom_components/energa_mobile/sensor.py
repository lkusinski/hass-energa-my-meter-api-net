"""Sensor platform for Energa My Meter.

Clean rebuild based on thedeemling/hass-energa-my-meter architecture.
Implements invisible statistics sensors for Energy Dashboard integration.
Supports multi-zone tariffs (G12w: strefa 1 + strefa 2).
"""

import logging
from datetime import timedelta
from typing import override
from zoneinfo import ZoneInfo

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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.loader import async_get_integration

from .api import EnergaAuthError, EnergaConnectionError, EnergaTokenExpiredError
from .const import (
    CONF_BALANCE_BASELINE_EXPORT,
    CONF_BALANCE_BASELINE_IMPORT,
    CONF_BANK_INITIAL_KWH,
    CONF_BANK_INITIAL_PLN,
    CONF_BANK_RCE_PRICE,
    CONF_ENABLE_AUTO_SETTLEMENT,
    CONF_PROSUMER_COEFFICIENT,
    CONF_RCE_AUTO_FETCH,
    CONF_SETTLEMENT_DATE,
    CONF_USE_ROLLING_365D,
    DEFAULT_BALANCE_BASELINE,
    DEFAULT_BANK_INITIAL_KWH,
    DEFAULT_BANK_INITIAL_PLN,
    DEFAULT_BANK_RCE_PRICE,
    DEFAULT_ENABLE_AUTO_SETTLEMENT,
    DEFAULT_PROSUMER_COEFFICIENT,
    DEFAULT_RCE_AUTO_FETCH,
    DEFAULT_SETTLEMENT_DATE,
    DEFAULT_USE_ROLLING_365D,
    DOMAIN,
    ROLLING_MIN_COVERAGE_DAYS,
    get_price_for_key,
)
from .settlement import (
    FlowAccumulator,
    days_to_settlement,
    deposit_valid_until,
    month_to_date_forecast,
    next_settlement_date,
    parse_settlement_date,
    rolling_kwh_bank,
)

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

    # Create coordinator
    coordinator = EnergaCoordinator(hass, api, entry)

    # Initial data fetch
    try:
        await coordinator.async_config_entry_first_refresh()
        _LOGGER.debug("Energa: Initial refresh successful")
    except Exception as err:
        _LOGGER.warning("Energa: Initial fetch failed, will retry: %s", err)

    # CRITICAL: Fetch meters directly from API to create sensors
    # Don't rely on coordinator.data which may be empty at startup
    try:
        meters_list = await api.async_get_data(force_refresh=False)
        _LOGGER.info(
            "Energa: Fetched %d meters from API for sensor setup",
            len(meters_list) if meters_list else 0,
        )
    except Exception as err:
        _LOGGER.error("Energa: Failed to fetch meters for setup: %s", err)
        meters_list = []

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
        ppe = meter.get("ppe", meter_id)
        has_zones = meter.get("zone_count", 1) > 1

        device_info = DeviceInfo(
            identifiers={(DOMAIN, str(serial))},
            name=f"Energa {serial}",
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
        if meter.get("total_minus"):
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
        if has_zones and meter.get("total_minus"):
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
                state_class_override=SensorStateClass.TOTAL,
            )
        )

        # 4. Daily Export (Today's production - resets at midnight)
        if meter.get("obis_minus"):
            sensors.append(
                EnergaLiveSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="daily_produkcja",
                    name="Produkcja Dziś",
                    icon="mdi:solar-power",
                    device_info=device_info,
                    state_class_override=SensorStateClass.TOTAL,
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
        if meter.get("obis_minus") and has_zones:
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
                EnergaCostStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="export_1",
                    name="Panel Energia Produkcja Strefa 1 Rekompensata",
                    device_info=device_info,
                    entry=entry,
                    serial=serial,
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
            sensors.append(
                EnergaCostStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="export_2",
                    name="Panel Energia Produkcja Strefa 2 Rekompensata",
                    device_info=device_info,
                    entry=entry,
                    serial=serial,
                )
            )
        elif meter.get("obis_minus"):
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
            sensors.append(
                EnergaCostStatisticsSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    data_key="export",
                    name="Panel Energia Produkcja Rekompensata",
                    device_info=device_info,
                    entry=entry,
                    serial=serial,
                )
            )

        # === PROSUMER BALANCE SENSOR ===
        # (only for prosumers — meters with export capability)
        if meter.get("obis_minus"):
            sensors.append(
                EnergaProsumerBalanceSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    device_info=device_info,
                    entry=entry,
                    has_zones=has_zones,
                )
            )

        # === BANK SENSORS (native, per strefa, old kWh + new PLN, issue #37) ===
        # Auto-detect old (net-metering, coeff 0.8/0.7) vs new (net-billing, coeff < 0.7)
        # coefficient 0.8 = old system (roczny bilans kWh)
        # coefficient 0.7 = old system with 0.7 opłata (przejściowy)
        # coefficient 0.0 = new system (net-billing, RCE×1.23, miesięczny PLN)
        if meter.get("obis_minus"):
            coeff = float(entry.options.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT))
            is_old_system = coeff >= 0.7  # 0.8 or 0.7 = old net-metering

            if is_old_system:
                sensors.append(
                    EnergaBankKwhSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        has_zones=has_zones,
                    )
                )
            else:
                sensors.append(
                    EnergaBankPlnSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        has_zones=has_zones,
                    )
                )
                # RCEm auto sensor only for new system (net-billing)
                sensors.append(
                    EnergaRceSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        api=api,
                    )
                )
                # v0.2.11 bill forecast (monthly net-billing view, needs history)
                if entry.options.get(
                    CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT
                ):
                    sensors.append(
                        EnergaBillForecastSensor(
                            coordinator=coordinator,
                            meter_id=meter_id,
                            device_info=device_info,
                            entry=entry,
                            has_zones=has_zones,
                        )
                    )
            # v0.2.12 native bank flows (Energy battery, live stock):
            # charge/discharge totals from Bilans deltas (old) or
            # export/import deltas (new). Replaces bank_energii.yaml.
            for direction in ("charge", "discharge"):
                sensors.append(
                    EnergaBankFlowSensor(
                        coordinator=coordinator,
                        meter_id=meter_id,
                        device_info=device_info,
                        entry=entry,
                        has_zones=has_zones,
                        direction=direction,
                    )
                )
            sensors.append(
                EnergaFirstDataDateSensor(
                    coordinator=coordinator,
                    meter_id=meter_id,
                    device_info=device_info,
                    entry=entry,
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

        if meter.get("obis_minus"):
            price_keys.append(("export", "Cena Oddania", "mdi:cash-refund"))
            price_keys.append(
                ("coefficient", "Współczynnik Prosumencki", "mdi:percent")
            )

        for p_key, p_name, p_icon in price_keys:
            sensors.append(
                EnergaPriceSensor(
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

    _LOGGER.info("Created %d Energa sensors", len(sensors))
    _LOGGER.debug(
        "Energa: Sensor list: %s",
        [
            s.entity_id if hasattr(s, "entity_id") else s._attr_unique_id
            for s in sensors
        ],
    )
    async_add_entities(sensors, update_before_add=True)

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


class EnergaCoordinator(DataUpdateCoordinator):
    """Coordinator for fetching Energa data with smart fetch."""

    def __init__(self, hass: HomeAssistant, api, entry) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Energa My Meter",
            update_interval=timedelta(hours=1),  # Hourly updates
        )
        self.api = api
        self.entry = entry
        self._hourly_stats: dict = {}  # {meter_id: {"import_1": [...], "import_2": [...], ...}}
        self._pre_fetched_stats: dict = {}  # {entity_id: {"sum": x, "start": dt}}
        self._meter_totals: dict = {}  # {meter_id: {"import_1": x, "import_2": y, ...}}
        self._rce_cache: float | None = None
        self._rce_last_fetch = None
        self._rce_fetch_lock = False
        self._rce_source: str | None = None  # v0.2.11: where cached RCE comes from
        self._rolling_365: dict = {}  # v0.2.11: {meter_id: {suffix: kWh, "_coverage_days": n}}
        self._mtd: dict = {}  # v0.2.11: month-to-date sums, same shape

    async def _async_update_data(self):
        """Fetch data from API using smart fetch pattern."""
        try:
            # Fetch meter data (force_refresh=True to update total readings
            # from lastMeasurements on every cycle — fixes #20, #22)
            meters = await self.api.async_get_data(force_refresh=True)

            # Filter active meters
            active_meters = [
                m
                for m in meters
                if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
            ]

            for meter in active_meters:
                meter_id = meter["meter_point_id"]
                has_zones = meter.get("zone_count", 1) > 1

                # Store meter totals for reference
                totals = {
                    "import": float(meter.get("total_plus", 0) or 0),
                    "export": float(meter.get("total_minus", 0) or 0),
                }
                if has_zones:
                    totals["import_1"] = float(meter.get("total_plus_1", 0) or 0)
                    totals["import_2"] = float(meter.get("total_plus_2", 0) or 0)
                    totals["export_1"] = float(meter.get("total_minus_1", 0) or 0)
                    totals["export_2"] = float(meter.get("total_minus_2", 0) or 0)
                self._meter_totals[meter_id] = totals

                # Pre-fetch last statistics for this meter (async-safe)
                await self._fetch_last_stats_for_meter(meter_id, has_zones)

                # Query last_stat_date for this meter (smart fetch)
                start_date = await self._get_smart_start_date(meter_id, has_zones)

                try:
                    stats = await self.api.async_get_hourly_statistics(
                        meter_id, start_date=start_date
                    )
                    self._hourly_stats[meter_id] = stats
                except EnergaTokenExpiredError:
                    raise  # Propagate to outer handler for re-login
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to fetch hourly stats for %s: %s", meter_id, err
                    )
                    self._hourly_stats[meter_id] = {"import": [], "export": []}

            # === RCE auto-fetch (net-billing, 24h cache) ===
            # v0.2.11: prefer official volume-weighted RCEm (as billed),
            # fall back to plain RCE average. Month rule: latest PUBLISHED
            # (PSE publishes ~11th of next month).
            try:
                from datetime import datetime as _dt
                from .settlement import target_rcem_month
                opts = self.entry.options
                if opts.get("rce_auto_fetch"):
                    need_fetch = False
                    if self._rce_cache is None:
                        need_fetch = True
                    elif self._rce_last_fetch and (_dt.now() - self._rce_last_fetch).total_seconds() > 22 * 3600:
                        need_fetch = True
                    if need_fetch and not self._rce_fetch_lock:
                        self._rce_fetch_lock = True
                        try:
                            _ty, _tm = target_rcem_month(_dt.now().date())
                            rcem = await self.api.async_fetch_official_rcem(_tm, _ty)
                            source = "PSE RCEm official"
                            if rcem is None:
                                rcem = await self.api.async_fetch_rce_average(_tm, _ty)
                                source = "PSE RCE avg fallback"
                            if rcem is not None:
                                self._rce_cache = rcem
                                self._rce_last_fetch = _dt.now()
                                self._rce_source = source
                                _LOGGER.info("Coordinator RCE auto-fetched: %.5f PLN/kWh (%s)", rcem, source)
                        finally:
                            self._rce_fetch_lock = False
            except Exception as rce_err:
                _LOGGER.debug("RCE auto-fetch skipped: %s", rce_err)

            # === v0.2.11 settlement calibration: rolling 365d + MTD sums ===
            try:
                from datetime import datetime as _dt2
                _opts = self.entry.options
                if _opts.get(CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT):
                    _now = _dt2.now(TIMEZONE)
                    if _opts.get(CONF_USE_ROLLING_365D, DEFAULT_USE_ROLLING_365D):
                        _rolling = await self._async_compute_period_sums(
                            _now - timedelta(days=365), _now
                        )
                        if _rolling:
                            self._rolling_365 = _rolling
                    _month_start = _now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    _mtd = await self._async_compute_period_sums(_month_start, _now)
                    if _mtd:
                        self._mtd = _mtd
            except Exception as cal_err:
                _LOGGER.debug("Settlement calibration skipped: %s", cal_err)

            return active_meters

        except EnergaTokenExpiredError:
            if getattr(self, "_retrying", False):
                raise UpdateFailed("Token expired again after re-login")
            _LOGGER.debug("Token expired, attempting re-login")
            try:
                await self.api.async_login()
                self._retrying = True
                try:
                    return await self._async_update_data()
                finally:
                    self._retrying = False
            except EnergaAuthError as err:
                raise UpdateFailed(f"Auth error after token refresh: {err}") from err

        except EnergaConnectionError as err:
            raise UpdateFailed(f"Connection error: {err}") from err

        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def _get_smart_start_date(self, meter_id: str, has_zones: bool = False):
        """Get start_date based on last imported statistic."""
        from datetime import datetime as dt_datetime

        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import get_last_statistics
        from homeassistant.helpers import entity_registry as er
        from homeassistant.util import dt as dt_util

        tz = ZoneInfo("Europe/Warsaw")
        now = dt_datetime.now(tz)
        default_start = now - timedelta(days=30)

        # Find entity_id for this meter's import sensor
        registry = er.async_get(self.hass)
        entity_id = None

        # For G12w, check zone 1 sensor; for single zone, check import sensor
        target_unique_id = (
            f"energa_{meter_id}_import_1_stats"
            if has_zones
            else f"energa_{meter_id}_import_stats"
        )

        for entity in registry.entities.values():
            if (
                entity.unique_id == target_unique_id
                and entity.platform == DOMAIN
            ):
                entity_id = entity.entity_id
                break

        if not entity_id:
            _LOGGER.debug(
                "No entity found for meter %s, using default 30 days", meter_id
            )
            return default_start

        # Query last statistic
        try:
            last_stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, entity_id, True, {"sum"}
            )

            if entity_id in last_stats and last_stats[entity_id]:
                last_ts = last_stats[entity_id][0].get("start")
                if last_ts:
                    # Convert to datetime
                    if isinstance(last_ts, (int, float)):
                        last_dt = dt_util.utc_from_timestamp(last_ts).astimezone(tz)
                    else:
                        last_dt = last_ts.astimezone(tz)

                    # Start from next hour
                    start_date = last_dt + timedelta(hours=1)

                    _LOGGER.debug(
                        "Smart fetch for %s: last_stat=%s, start=%s",
                        entity_id,
                        last_dt,
                        start_date,
                    )
                    return start_date

        except Exception as err:
            _LOGGER.warning("Failed to query last stats for %s: %s", entity_id, err)

        return default_start

    def get_hourly_stats(self, meter_id: str, data_key: str) -> list:
        """Get hourly statistics for a meter."""
        meter_stats = self._hourly_stats.get(meter_id, {})
        return meter_stats.get(data_key, [])

    def get_pre_fetched_stats(self) -> dict:
        """Get pre-fetched last statistics for all entities."""
        return self._pre_fetched_stats

    def get_meter_total(self, meter_id: str, data_key: str) -> float:
        """Get meter total reading from API data."""
        totals = self._meter_totals.get(meter_id, {})
        return totals.get(data_key, 0.0)

    async def _fetch_last_stats_for_meter(self, meter_id: str, has_zones: bool = False):
        """Pre-fetch last statistics for meter entities (async-safe)."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import get_last_statistics
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)

        # Determine which suffixes to check
        if has_zones:
            suffixes = ["import_1", "import_2", "export_1", "export_2"]
        else:
            suffixes = ["import", "export"]

        for suffix in suffixes:
            unique_id = f"energa_{meter_id}_{suffix}_stats"

            for entity in registry.entities.values():
                if entity.unique_id == unique_id and entity.platform == DOMAIN:
                    entity_id = entity.entity_id

                    try:
                        last_stats = await get_instance(self.hass).async_add_executor_job(
                            get_last_statistics,
                            self.hass,
                            1,
                            entity_id,
                            True,
                            {"sum", "start"},
                        )

                        if entity_id in last_stats and last_stats[entity_id]:
                            self._pre_fetched_stats[entity_id] = last_stats[entity_id][
                                0
                            ]
                            _LOGGER.debug(
                                "Pre-fetched stats for %s: sum=%.3f",
                                entity_id,
                                last_stats[entity_id][0].get("sum", 0),
                            )

                    except Exception as err:
                        _LOGGER.debug(
                            "Could not pre-fetch stats for %s: %s", entity_id, err
                        )

    async def _async_compute_period_sums(self, start, end) -> dict:
        """Sum Panel Energia statistics per meter over [start, end] (v0.2.11).

        Returns {meter_id: {suffix: delta_kwh, "_coverage_days": n}} where
        delta is last.sum - first.sum of daily statistics in the window.
        Returns {} when recorder data is unavailable (fully defensive —
        settlement calibration must never break the coordinator update).
        """
        try:
            import functools

            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
            from homeassistant.helpers import entity_registry as er
        except Exception as err:
            _LOGGER.debug("Period sums unavailable (imports): %s", err)
            return {}
        try:
            registry = er.async_get(self.hass)
            wanted: dict = {}  # entity_id -> (meter_id, suffix)
            for mid in list(self._meter_totals.keys()):
                for suffix in (
                    "import_1", "import_2", "export_1", "export_2",
                    "import", "export",
                ):
                    uid = f"energa_{mid}_{suffix}_stats"
                    for entity in registry.entities.values():
                        if entity.unique_id == uid and entity.platform == DOMAIN:
                            wanted[entity.entity_id] = (str(mid), suffix)
                            break
            if not wanted:
                return {}
            stats = await get_instance(self.hass).async_add_executor_job(
                functools.partial(
                    statistics_during_period,
                    self.hass, start, end, list(wanted.keys()), "day", None, {"sum"},
                )
            )
            out: dict = {}
            for stat_id, points in (stats or {}).items():
                if not points:
                    continue
                sums = [p.get("sum") for p in points if p.get("sum") is not None]
                if len(sums) < 2:
                    continue
                mid, suffix = wanted[stat_id]
                out.setdefault(str(mid), {})[suffix] = round(
                    max(0.0, sums[-1] - sums[0]), 3
                )
                span = self._stat_span_days(points)
                prev = out[str(mid)].get("_coverage_days")
                out[str(mid)]["_coverage_days"] = (
                    span if prev is None else min(prev, span)
                )
            return out
        except Exception as err:
            _LOGGER.debug("Period sums failed: %s", err)
            return {}

    @staticmethod
    def _stat_span_days(points) -> int:
        """Actual day span covered by statistics points (defensive)."""
        try:
            from datetime import datetime as _dt

            def _ts(p, key):
                v = p.get(key)
                if v is None:
                    return None
                if isinstance(v, (int, float)):
                    return float(v)
                if isinstance(v, _dt):
                    return v.timestamp()
                return None

            first = _ts(points[0], "start")
            last = _ts(points[-1], "end") or _ts(points[-1], "start")
            if first is None or last is None or last <= first:
                return 0
            return int((last - first) // 86400)
        except Exception:
            return 0


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

        _LOGGER.debug(
            "LiveSensor %s: Meter %s not found in data", self._attr_name, self._meter_id
        )
        return None


class EnergaProsumerBalanceSensor(CoordinatorEntity, SensorEntity):
    """Prosumer balance sensor: (export − baseline_export) × coeff − (import − baseline_import).

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
        **kwargs,
    ) -> None:
        """Initialize prosumer balance sensor."""
        super().__init__(coordinator)

        self._meter_id = meter_id
        self._entry = entry

        # Entity attributes
        self._attr_name = "Bilans Prosumencki"
        self._attr_unique_id = f"energa_{meter_id}_prosumer_balance"
        self._attr_has_entity_name = True

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

        coefficient = float(
            self._entry.options.get(
                CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT
            )
        )
        baseline_import = float(
            self._entry.options.get(
                CONF_BALANCE_BASELINE_IMPORT, DEFAULT_BALANCE_BASELINE
            )
        )
        baseline_export = float(
            self._entry.options.get(
                CONF_BALANCE_BASELINE_EXPORT, DEFAULT_BALANCE_BASELINE
            )
        )

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

        coefficient = float(
            self._entry.options.get(
                CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT
            )
        )
        baseline_import = float(
            self._entry.options.get(
                CONF_BALANCE_BASELINE_IMPORT, DEFAULT_BALANCE_BASELINE
            )
        )
        baseline_export = float(
            self._entry.options.get(
                CONF_BALANCE_BASELINE_EXPORT, DEFAULT_BALANCE_BASELINE
            )
        )

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
            "note": "Ustaw baseline_import/export w Opcjach integracji na stan licznika z początku okresu rozliczeniowego",
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


class EnergaBankKwhSensor(CoordinatorEntity, SensorEntity):
    """Virtual storage in kWh for old prosumer (net-metering, per strefa G12W).

    For old system (coefficient 0.8/0.7): bank = max(0, Bilans) + initial_kwh.
    Bilans = (export - baseline_export) * coefficient - (import - baseline_import).
    Uses per-zone meter totals if G12W. 1.23 NOT applied (old system).
    """

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry, has_zones: bool = False) -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._entry = entry
        self._has_zones = has_zones
        self._attr_name = "Bank Wirtualny kWh"
        self._attr_unique_id = f"energa_{meter_id}_bank_kwh"
        self._attr_has_entity_name = True
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:battery-charging"

    @property
    def native_value(self):
        totals = self.coordinator._meter_totals.get(str(self._meter_id))
        if not totals:
            return None

        opts = self._entry.options
        mid = self._meter_id

        # Get baselines
        bi = float(opts.get(CONF_BALANCE_BASELINE_IMPORT, DEFAULT_BALANCE_BASELINE))
        be = float(opts.get(CONF_BALANCE_BASELINE_EXPORT, DEFAULT_BALANCE_BASELINE))

        if self._has_zones:
            # Per-zone baselines if available, else global
            bi1 = float(opts.get(f"meter_{mid}_balance_baseline_import_1", bi))
            bi2 = float(opts.get(f"meter_{mid}_balance_baseline_import_2", bi))
            be1 = float(opts.get(f"meter_{mid}_balance_baseline_export_1", be))
            be2 = float(opts.get(f"meter_{mid}_balance_baseline_export_2", be))

            imp1 = float(totals.get("import_1", totals.get("import", 0)))
            imp2 = float(totals.get("import_2", 0))
            exp1 = float(totals.get("export_1", totals.get("export", 0)))
            exp2 = float(totals.get("export_2", 0))

            # If per-zone baselines not set, use total baselines with total import/export
            if bi1 == bi and bi2 == bi:
                # No per-zone baseline — use total import/export minus global baseline
                net_imp = float(totals.get("import", 0)) - bi
                net_exp = float(totals.get("export", 0)) - be
            else:
                net_imp = (imp1 - bi1) + (imp2 - bi2)
                net_exp = (exp1 - be1) + (exp2 - be2)
        else:
            net_imp = float(totals.get("import", 0)) - bi
            net_exp = float(totals.get("export", 0)) - be

        coeff = float(opts.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT))
        initial = float(opts.get(CONF_BANK_INITIAL_KWH, DEFAULT_BANK_INITIAL_KWH))
        bilans = (net_exp * coeff) - net_imp
        bank = max(0, bilans) + initial
        mode = "baseline"

        _LOGGER.debug(
            "BankKwh %s: imp=%.2f exp=%.2f coeff=%.2f bilans=%.2f initial=%.2f bank=%.2f",
            mid, net_imp, net_exp, coeff, bilans, initial, bank,
        )

        # v0.2.11 rolling FIFO mode: energy older than 12 months expires, so
        # only trailing-365-day flows count (needs Download History).
        # A plain Jan-1 reset would NOT comply (rolling window, not calendar).
        coverage = 0
        if opts.get(CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT) and opts.get(
            CONF_USE_ROLLING_365D, DEFAULT_USE_ROLLING_365D
        ):
            rolling = getattr(self.coordinator, "_rolling_365", {}).get(str(mid), {})
            coverage = int(rolling.get("_coverage_days", 0))
            if coverage >= ROLLING_MIN_COVERAGE_DAYS:
                exp365 = rolling.get(
                    "export",
                    rolling.get("export_1", 0) + rolling.get("export_2", 0),
                )
                imp365 = rolling.get(
                    "import",
                    rolling.get("import_1", 0) + rolling.get("import_2", 0),
                )
                bank = rolling_kwh_bank(exp365, imp365, coeff)
                mode = "rolling_365d"
                _LOGGER.debug(
                    "BankKwh %s rolling: exp365=%.2f imp365=%.2f bank=%.2f",
                    mid, exp365, imp365, bank,
                )

        # Build rich attributes for Lovelace visibility
        attrs = {
            "net_import_kwh": round(net_imp, 2),
            "net_export_kwh": round(net_exp, 2),
            "coefficient": coeff,
            "bilans_kwh": round(bilans, 2),
            "initial_kwh": initial,
            "source": "net-metering 0.8 roczny (old) — faktury FES",
            "formula": "max(0, (export-baseline)*coeff - (import-baseline)) + initial",
            "unit": "kWh — ile energii możesz jeszcze odebrać za darmo",
            "settlement_mode": mode,
        }
        if mode == "rolling_365d":
            attrs["coverage_days"] = coverage
        if opts.get(CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT):
            settle_str = opts.get(CONF_SETTLEMENT_DATE, DEFAULT_SETTLEMENT_DATE)
            base = parse_settlement_date(settle_str)
            if base is not None:
                from datetime import date as _date

                attrs["settlement_next"] = next_settlement_date(base, _date.today()).isoformat()
                attrs["days_to_settlement"] = days_to_settlement(settle_str)
            attrs["validity_note"] = (
                "FIFO 12 m-cy od końca miesiąca wprowadzenia, najstarsza energia "
                "najpierw (energa.pl net-metering). Reset 1.01 NIE obowiązuje."
            )
        if self._has_zones:
            attrs.update({
                "import_1": round(float(totals.get("import_1", 0)), 2),
                "import_2": round(float(totals.get("import_2", 0)), 2),
                "export_1": round(float(totals.get("export_1", 0)), 2),
                "export_2": round(float(totals.get("export_2", 0)), 2),
                "per_strefa_note": "L1 droga / L2 tania — bank łączny, per-strefa w atrybutach",
            })
        self._attr_extra_state_attributes = attrs

        return round(bank, 2)


class EnergaBankPlnSensor(CoordinatorEntity, SensorEntity):
    """Virtual storage in PLN for new prosumer (net-billing, RCE×1.23).

    For new system (coefficient 0.0): bank in PLN.
    bank = initial_pln + export×RCE×1.23 - import×cena_per_strefa.
    Per strefa G12W: import_1 × cena_1 + import_2 × cena_2.
    RCE fetched from PSE or manual input, ×1.23 (VAT on energy sold).
    """

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry, has_zones: bool = False) -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._entry = entry
        self._has_zones = has_zones
        self._attr_name = "Bank Wirtualny PLN"
        self._attr_unique_id = f"energa_{meter_id}_bank_pln"
        self._attr_has_entity_name = True
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = "PLN"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_icon = "mdi:cash-check"

    @property
    def native_value(self):
        totals = self.coordinator._meter_totals.get(str(self._meter_id))
        if not totals:
            return None

        opts = self._entry.options
        mid = self._meter_id

        # Prefer coordinator RCE cache if auto-fetch enabled
        coord_rce = getattr(self.coordinator, "_rce_cache", None)
        if opts.get(CONF_RCE_AUTO_FETCH) and coord_rce is not None:
            rce = float(coord_rce)
        else:
            rce = float(opts.get(CONF_BANK_RCE_PRICE, DEFAULT_BANK_RCE_PRICE))
        initial = float(opts.get(CONF_BANK_INITIAL_PLN, DEFAULT_BANK_INITIAL_PLN))

        bi = float(opts.get(CONF_BALANCE_BASELINE_IMPORT, DEFAULT_BALANCE_BASELINE))
        be = float(opts.get(CONF_BALANCE_BASELINE_EXPORT, DEFAULT_BALANCE_BASELINE))

        if self._has_zones:
            # Per-zone baselines if available
            bi1 = float(opts.get(f"meter_{mid}_balance_baseline_import_1", bi))
            bi2 = float(opts.get(f"meter_{mid}_balance_baseline_import_2", bi))
            be1 = float(opts.get(f"meter_{mid}_balance_baseline_export_1", be))
            be2 = float(opts.get(f"meter_{mid}_balance_baseline_export_2", be))

            imp1 = float(totals.get("import_1", totals.get("import", 0)))
            imp2 = float(totals.get("import_2", 0))
            exp1 = float(totals.get("export_1", totals.get("export", 0)))
            exp2 = float(totals.get("export_2", 0))

            # If per-zone baselines not set, use total
            if bi1 == bi and bi2 == bi:
                net_imp = float(totals.get("import", 0)) - bi
                net_exp = float(totals.get("export", 0)) - be
                price1 = get_price_for_key(opts, "import", mid)
                net_imp_cost = net_imp * price1
            else:
                net_imp1 = imp1 - bi1
                net_imp2 = imp2 - bi2
                price1 = get_price_for_key(opts, "import_1", mid)
                price2 = get_price_for_key(opts, "import_2", mid)
                net_imp_cost = net_imp1 * price1 + net_imp2 * price2
                net_exp = (exp1 - be1) + (exp2 - be2)
        else:
            net_imp = float(totals.get("import", 0)) - bi
            net_exp = float(totals.get("export", 0)) - be
            price = get_price_for_key(opts, "import", mid)
            net_imp_cost = net_imp * price

        comp_export = net_exp * rce * 1.23
        bank = initial + comp_export - net_imp_cost

        _LOGGER.debug(
            "BankPln %s: net_imp_cost=%.2f net_exp=%.2f rce=%.5f comp_export=%.2f initial=%.2f bank=%.2f",
            mid, net_imp_cost, net_exp, rce, comp_export, initial, bank,
        )

        coord_cache = getattr(self.coordinator, "_rce_cache", None)
        if opts.get(CONF_RCE_AUTO_FETCH) and coord_cache is not None:
            rce_source = getattr(self.coordinator, "_rce_source", None) or "PSE auto"
        else:
            rce_source = "manual"
        attrs = {
            "net_import_kwh": round(net_imp, 2) if not self._has_zones else round(imp1 - bi1 + imp2 - bi2, 2),
            "net_export_kwh": round(net_exp, 2),
            "rce_price": rce,
            "rce_source": rce_source,
            "vat_multiplier": 1.23,
            "compensation_export_pln": round(comp_export, 2),
            "import_cost_pln": round(net_imp_cost, 2),
            "initial_pln": initial,
            "source": "net-billing RCE×1.23 miesięczny (nowy system)",
            "formula": "initial + export×RCE×1.23 - import×cena_strefa",
            "unit": "PLN — pozycja netto (depozyt minus koszt importu); ujemny = do zapłaty",
        }
        if opts.get(CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT):
            from datetime import date as _date

            _today = _date.today()
            attrs["deposit_valid_until"] = deposit_valid_until(_today.year, _today.month).isoformat()
            attrs["refund_cap_note"] = (
                "Niewykorzystany depozyt zwracany po 12 m-cach max 20% (RCEm) "
                "/ 30% (RCE od 01.02.2025), do końca 13. miesiąca (Dz.U. 1847)."
            )
            attrs["validity_note"] = (
                "Depozyt ważny 12 m-cy od przypisania (M+1, ×1.23), najstarsze "
                "środki najpierw. Zerowanie co miesiąc NIE obowiązuje."
            )
            attrs["hourly_netting_note"] = (
                "Sprzedawca bilansuje godzinowo (faktura 07: 456 kWh z delty "
                "licznika 523 kWh); sensor liczy z delt licznika — przybliżenie."
            )
            settle_str = opts.get(CONF_SETTLEMENT_DATE, DEFAULT_SETTLEMENT_DATE)
            if parse_settlement_date(settle_str) is not None:
                attrs["days_to_settlement"] = days_to_settlement(settle_str)
        if self._has_zones:
            price1 = get_price_for_key(opts, "import_1", mid)
            price2 = get_price_for_key(opts, "import_2", mid)
            attrs.update({
                "price_1": price1,
                "price_2": price2,
                "import_1": round(float(totals.get("import_1", 0)), 2),
                "import_2": round(float(totals.get("import_2", 0)), 2),
                "per_strefa_note": "L1 droga ×1.30 / L2 tania ×0.65 — koszt liczony per strefa",
            })
        self._attr_extra_state_attributes = attrs

        return round(bank, 2)


class EnergaBankFlowSensor(CoordinatorEntity, SensorEntity):
    """Native bank charge/discharge totals for the Energy battery (v0.2.12).

    Energy Dashboard batteries need total_increasing FLOW sensors, while
    Bank kWh/PLN sensors expose STATE. These two sensors (charge/discharge)
    accumulate Bilans movement between coordinator updates via
    FlowAccumulator (first reading only anchors, restart-safe through
    HA state restore). Replaces the bank_energii.yaml template pair.

    - Old net-metering: base is Bilans (net_exp*coeff - net_imp).
    - New net-billing: charge follows net_export, discharge net_import
      (raw kWh; money value lives in Bank PLN).
    """

    def __init__(
        self, coordinator, meter_id: str, device_info: DeviceInfo,
        entry: ConfigEntry, has_zones: bool = False,
        direction: str = "charge",
    ) -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._entry = entry
        self._has_zones = has_zones
        self._direction = direction
        self._flows = FlowAccumulator()
        is_charge = direction == "charge"
        self._attr_name = (
            "Bank Ładowanie" if is_charge else "Bank Rozładowanie"
        )
        self._attr_unique_id = f"energa_{meter_id}_bank_{direction}"
        self._attr_has_entity_name = True
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = (
            "mdi:battery-charging" if is_charge else "mdi:battery-discharging"
        )
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Seed totals from the previous HA state (restart-safe)."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state in (None, "unknown", "unavailable", "none"):
            return
        try:
            value = float(last.state)
        except (ValueError, TypeError):
            return
        # Seed both sides; each mode reads only its own side.
        self._flows.restore(value, value)

    def _nets(self):
        """(net_import, net_export) from meter totals minus baselines."""
        totals = self.coordinator._meter_totals.get(str(self._meter_id))
        if not totals:
            return None
        opts = self._entry.options
        mid = self._meter_id
        bi = float(opts.get(CONF_BALANCE_BASELINE_IMPORT, DEFAULT_BALANCE_BASELINE))
        be = float(opts.get(CONF_BALANCE_BASELINE_EXPORT, DEFAULT_BALANCE_BASELINE))
        if self._has_zones:
            bi1 = float(opts.get(f"meter_{mid}_balance_baseline_import_1", bi))
            bi2 = float(opts.get(f"meter_{mid}_balance_baseline_import_2", bi))
            be1 = float(opts.get(f"meter_{mid}_balance_baseline_export_1", be))
            be2 = float(opts.get(f"meter_{mid}_balance_baseline_export_2", be))
            if bi1 == bi and bi2 == bi:
                net_imp = float(totals.get("import", 0)) - bi
                net_exp = float(totals.get("export", 0)) - be
            else:
                imp1 = float(totals.get("import_1", totals.get("import", 0)))
                imp2 = float(totals.get("import_2", 0))
                exp1 = float(totals.get("export_1", totals.get("export", 0)))
                exp2 = float(totals.get("export_2", 0))
                net_imp = (imp1 - bi1) + (imp2 - bi2)
                net_exp = (exp1 - be1) + (exp2 - be2)
        else:
            net_imp = float(totals.get("import", 0)) - bi
            net_exp = float(totals.get("export", 0)) - be
        return (net_imp, net_exp)

    @property
    def native_value(self):
        nets = self._nets()
        if nets is None:
            return None
        net_imp, net_exp = nets
        opts = self._entry.options
        coeff = float(opts.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT))
        value: float | None = None
        if coeff >= 0.7:  # old net-metering: follow Bilans movement
            base = net_exp * coeff - net_imp
            flows = self._flows.update(base)
            value = flows[0] if self._direction == "charge" else flows[1]
            mode = "bilans"
        else:  # new net-billing: raw export/import growth
            # Meter totals only grow, so growth lands on side [0].
            base = net_exp if self._direction == "charge" else net_imp
            value = self._flows.update(base)[0]
            mode = "flows"
        value = charge if self._direction == "charge" else discharge
        self._attr_extra_state_attributes = {
            "settlement_mode": mode,
            "coefficient": coeff,
            "net_import_kwh": round(net_imp, 2),
            "net_export_kwh": round(net_exp, 2),
            "source": "natywna para do Baterii w Panelu Energia (zastępuje bank_energii.yaml)",
        }
        return round(value, 2) if value is not None else None


class EnergaFirstDataDateSensor(CoordinatorEntity, SensorEntity):
    """Date of first reading (auto-detected hierarchically, per strefa G12W).

    Created as diagnostic entity, disabled by default if user prefers.
    Value is set after async_find_first_data_date (year→half→month→day, ~14 req).
    Can be manually triggered via button.energa_xxx_wykryj_pierwszy_odczyt.
    """

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._entry = entry
        self._attr_name = "Data pierwszego odczytu"
        self._attr_unique_id = f"energa_{meter_id}_first_data_date"
        self._attr_has_entity_name = True
        self._attr_device_class = SensorDeviceClass.DATE
        self._attr_icon = "mdi:calendar-start"
        self._attr_entity_category = None

    @property
    def native_value(self):
        # Stored in entry.options["first_data_date"] after detection, or from API contract_date
        val = self._entry.options.get(f"meter_{self._meter_id}_first_data_date") or self._entry.options.get("first_data_date")
        if val:
            try:
                from datetime import datetime
                return datetime.strptime(val, "%Y-%m-%d").date()
            except:
                return None
        # Fallback to contract_date from meter data
        totals = self.coordinator._meter_totals.get(str(self._meter_id))
        # Try to get from meter data
        for m in self.coordinator.data or []:
            if str(m.get("meter_point_id")) == str(self._meter_id) or str(m.get("meter_serial")) == str(self._meter_id):
                if m.get("contract_date"):
                    try:
                        return datetime.strptime(str(m["contract_date"]), "%Y-%m-%d").date()
                    except:
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

        # Entity attributes
        self._attr_name = name
        self._attr_unique_id = f"energa_{meter_id}_{data_key}_stats"
        self._attr_has_entity_name = True

        # Sensor class attributes — state_class is required for Energy
        # Dashboard to list this entity in its configuration dropdown.
        # native_value intentionally returns None (data flows via
        # async_import_statistics in _handle_coordinator_update).
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

    @property
    def native_value(self):
        """Return None - statistics are imported via async_import_statistics.

        Returning a value here would cause HA Recorder to auto-create
        statistics with state=value and sum=0, causing spikes
        on the Energy Dashboard.
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
        from .data_updater import EnergaDataUpdater

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
        )

        if not energy_stats:
            _LOGGER.debug("DataUpdater returned no stats for %s", self.entity_id)
            super()._handle_coordinator_update()
            return

        # === IMPORT ENERGY STATISTICS ===
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

        _LOGGER.info(
            "Importing %d energy statistics for %s",
            len(energy_stats),
            self.entity_id,
        )
        async_import_statistics(self.hass, energy_metadata, energy_stats)

        # === IMPORT COST STATISTICS ===
        if cost_stats:
            cost_entity_id = f"{self.entity_id}_cost"
            if self._data_key == "import_1":
                cost_name = "Panel Energia Strefa 1 Koszt"
            elif self._data_key == "import_2":
                cost_name = "Panel Energia Strefa 2 Koszt"
            elif self._data_key == "import":
                cost_name = f"{self._attr_name} Koszt"
            else:
                cost_name = f"{self._attr_name} Rekompensata"

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

            price = self._get_price()

            _LOGGER.info(
                "Importing %d cost statistics for %s (price: %.4f PLN/kWh)",
                len(cost_stats),
                cost_entity_id,
                price,
            )
            async_import_statistics(self.hass, cost_metadata, cost_stats)

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
        self.entity_id = f"sensor.energa_{serial}_{energy_slug}_cost"

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


class EnergaPriceSensor(SensorEntity):
    """Diagnostic sensor exposing configured energy price as HA entity.

    Enables Energy Dashboard "Use entity with current price" mode.
    Not tied to coordinator — value comes from config options.
    """

    def __init__(
        self,
        data_key: str,
        name: str,
        icon: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
        serial: str,
        meter_id: str,
    ) -> None:
        """Initialize price sensor."""
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

    @property
    def native_value(self):
        """Return price from config options."""
        opts = dict(self._entry.options)

        if self._data_key == "coefficient":
            return float(
                opts.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT)
            )

        return get_price_for_key(
            opts, self._data_key, meter_id=self._meter_id
        )


class EnergaRceSensor(CoordinatorEntity, SensorEntity):
    """RCEm sensor — auto-fetch monthly RCE from PSE or use manual value.

    Displays the current RCEm (PLN/kWh) used for net-billing calculations.
    When auto-fetch is enabled, reads from Coordinator cache (24h) populated
    in _async_update_data. Falls back to manual value if fetch fails.
    """

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry, api) -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._entry = entry
        self._api = api
        self._attr_name = "RCEm (auto)"
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

    async def async_update_rcem(self):
        """Manual trigger — delegate to coordinator cache refresh."""
        try:
            rcem = await self._api.async_fetch_rcem()
            if rcem is not None:
                self.coordinator._rce_cache = rcem
                self.coordinator._rce_source = "PSE (manual refresh)"
                from datetime import datetime as _dt
                self.coordinator._rce_last_fetch = _dt.now()
                self.async_write_ha_state()
                _LOGGER.info("RCEm manual fetch: %.5f PLN/kWh", rcem)
        except Exception as err:
            _LOGGER.warning("RCEm manual fetch error: %s", err)


class EnergaBillForecastSensor(CoordinatorEntity, SensorEntity):
    """Month-end bill forecast for new prosumer (net-billing, v0.2.11).

    mtd_net = export_mtd×RCE×1.23 - import_mtd×cena_per_strefa from recorder
    statistics; forecast = mtd_net/days_elapsed×days_in_month (linear).
    Negative = month closes with payment due; positive = deposit surplus.
    Deposit covers energy charges only (not distribution/fixed fees).
    Created only when enable_auto_settlement is on (needs history).
    """

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry, has_zones: bool = False) -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._entry = entry
        self._has_zones = has_zones
        self._attr_name = "Prognoza Rachunku"
        self._attr_unique_id = f"energa_{meter_id}_bill_forecast"
        self._attr_has_entity_name = True
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "PLN"
        # NOTE: no monetary device_class (monetary+measurement rejected
        # by HA, v0.2.12 fix); forecast is a projection, not a meter total.
        self._attr_icon = "mdi:calendar-clock"

    def _mtd_parts(self):
        """(import_kwh, export_kwh) month-to-date from coordinator cache."""
        mtd = getattr(self.coordinator, "_mtd", {}).get(str(self._meter_id), {})
        imp = mtd.get("import", mtd.get("import_1", 0) + mtd.get("import_2", 0))
        exp = mtd.get("export", mtd.get("export_1", 0) + mtd.get("export_2", 0))
        return float(imp), float(exp)

    def _rce(self) -> float:
        opts = self._entry.options
        coord_rce = getattr(self.coordinator, "_rce_cache", None)
        if opts.get(CONF_RCE_AUTO_FETCH) and coord_rce is not None:
            return float(coord_rce)
        return float(opts.get(CONF_BANK_RCE_PRICE, DEFAULT_BANK_RCE_PRICE))

    @property
    def native_value(self):
        from datetime import date as _date

        mtd = getattr(self.coordinator, "_mtd", {}).get(str(self._meter_id))
        if not mtd:
            return None
        imp_mtd, exp_mtd = self._mtd_parts()
        opts = self._entry.options
        mid = self._meter_id
        if self._has_zones:
            m1 = mtd.get("import_1", 0)
            m2 = mtd.get("import_2", 0)
            p1 = get_price_for_key(opts, "import_1", mid)
            p2 = get_price_for_key(opts, "import_2", mid)
            imp_cost = m1 * p1 + m2 * p2
        else:
            imp_cost = imp_mtd * get_price_for_key(opts, "import", mid)
        rce = self._rce()
        mtd_net = exp_mtd * rce * 1.23 - imp_cost
        today = _date.today()
        import calendar as _cal

        forecast = month_to_date_forecast(
            mtd_net, today.day, _cal.monthrange(today.year, today.month)[1]
        )
        self._attr_extra_state_attributes = {
            "mtd_import_kwh": round(imp_mtd, 2),
            "mtd_export_kwh": round(exp_mtd, 2),
            "mtd_net_pln": round(mtd_net, 2),
            "forecast_pln": forecast,
            "day_of_month": today.day,
            "rce_price": rce,
            "rce_source": getattr(self.coordinator, "_rce_source", None) or "manual",
            "formula": "mtd_net/days_elapsed*days_in_month; mtd_net=export×RCE×1.23-import×cena",
            "note": "Depozyt pokrywa tylko energię czynną (bez dystrybucji i opłat stałych)",
        }
        return forecast

    @property
    def available(self) -> bool:
        return (
            self.coordinator.data is not None
            and str(self._meter_id) in getattr(self.coordinator, "_mtd", {})
        )
