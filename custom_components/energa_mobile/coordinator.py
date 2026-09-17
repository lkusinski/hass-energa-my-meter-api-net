"""Coordinator for Energa My Meter integration."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import EnergaAuthError, EnergaConnectionError, EnergaTokenExpiredError
from .const import (
    CONF_BALANCE_BASELINE_EXPORT,
    CONF_BALANCE_BASELINE_IMPORT,
    CONF_BANK_INITIAL_KWH,
    CONF_BANK_RCE_PRICE,
    CONF_ENABLE_AUTO_SETTLEMENT,
    CONF_ENABLE_SYNTHETIC_STORAGE,
    CONF_INVERTER_ENERGY_ENTITY,
    CONF_PROSUMER_COEFFICIENT,
    CONF_RCE_AUTO_FETCH,
    CONF_USE_ROLLING_365D,
    DEFAULT_BALANCE_BASELINE,
    DEFAULT_BANK_INITIAL_KWH,
    DEFAULT_BANK_RCE_PRICE,
    DEFAULT_ENABLE_AUTO_SETTLEMENT,
    DEFAULT_ENABLE_SYNTHETIC_STORAGE,
    DEFAULT_PROSUMER_COEFFICIENT,
    DEFAULT_USE_ROLLING_365D,
    DOMAIN,
)
from .settlement import reset_aware_delta

_LOGGER = logging.getLogger(__name__)

TIMEZONE = ZoneInfo("Europe/Warsaw")


class EnergaCoordinator(DataUpdateCoordinator):
    """Coordinator for fetching Energa data with smart fetch."""

    def __init__(self, hass: HomeAssistant, api, entry, storage=None) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Energa My Meter",
            update_interval=timedelta(minutes=15),  # 15-minute polling for fresh metering data
        )
        self.api = api
        self.entry = entry
        self.storage = storage
        from .projections.arbitrage import ArbitrageEngine
        self._arbitrage_engine = ArbitrageEngine(
            battery_efficiency=Decimal(str(entry.options.get("bess_efficiency", "0.88"))),
            charge_hours=int(entry.options.get("bess_charge_hours", 3)),
            discharge_hours=int(entry.options.get("bess_discharge_hours", 3)),
        )
        self._rce_interval_records: list = []
        self._arbitrage_plan = None
        self._rce_current_record = None
        self._hourly_stats: dict = {}  # {meter_id: {"import_1": [...], "import_2": [...], ...}}
        self._pre_fetched_stats: dict = {}  # {entity_id: {"sum": x, "start": dt}}
        self._meter_totals: dict = {}  # {meter_id: {"import_1": x, "import_2": y, ...}}
        self._rce_cache: float | None = None
        self._rce_last_fetch = None
        self._rce_fetch_lock = False
        self._rce_source: str | None = None  # v0.2.11: where cached RCE comes from
        self._rolling_365: dict = {}  # v0.2.11: {meter_id: {suffix: kWh, "_coverage_days": n}}
        self._monthly: dict = {}  # v0.2.20: {meter_id: {(y, m): {suffix: kWh}}} for FIFO bank
        self._mtd: dict = {}  # v0.2.11: month-to-date sums, same shape
        self._autoconsumption_summary: dict = {}
        self._profile_forecast_cache: dict = {}  # {meter_id: HourlyProfileResult}
        self._rce_records_last_fetch = None
        self._synth_tasks: dict[str, asyncio.Task] = {}

    def async_request_synthetic_storage(self, meter_id: str) -> None:
        """Debounce and run synthetic storage recalculation after statistics import."""

        if not self.entry.options.get(
            CONF_ENABLE_SYNTHETIC_STORAGE, DEFAULT_ENABLE_SYNTHETIC_STORAGE
        ):
            return

        async def _run_synth():
            await asyncio.sleep(2)
            try:
                from .synthetic_storage import async_synthesize_storage_from_recorder

                meter = next(
                    (m for m in (self.data or []) if m.get("meter_point_id") == meter_id),
                    None,
                )
                if meter:
                    await async_synthesize_storage_from_recorder(self.hass, self.entry, meter)
            except Exception as err:
                _LOGGER.debug("Deferred synthetic storage update failed for %s: %s", meter_id, err)

        if not hasattr(self, "_synth_tasks"):
            self._synth_tasks = {}
        prev_task = self._synth_tasks.get(meter_id)
        if prev_task and not prev_task.done():
            prev_task.cancel()

        self._synth_tasks[meter_id] = self.hass.async_create_task(_run_synth())

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

            # === Dynamic RCE auto-fetch & BESS Arbitrage Plan ===
            try:
                from datetime import date as _date
                from datetime import datetime as _dt

                from .adapters.pse.rce_client import async_fetch_rce_day
                today = _date.today()
                now_local = _dt.now()
                sess = getattr(self.api, "_session", None)
                if sess is None or getattr(sess, "closed", True):
                    sess = self.api._create_session_fn()

                need_rce_fetch = False
                if not self._rce_interval_records or self._rce_records_last_fetch is None:
                    need_rce_fetch = True
                elif (now_local - self._rce_records_last_fetch).total_seconds() > 7200:
                    need_rce_fetch = True
                elif now_local.hour >= 14 and not any(getattr(r, "business_date", None) == today + timedelta(days=1) for r in self._rce_interval_records):
                    need_rce_fetch = True

                if need_rce_fetch:
                    records_today = await async_fetch_rce_day(sess, today)
                    all_rce = list(records_today)
                    if now_local.hour >= 14:
                        tomorrow = today + timedelta(days=1)
                        records_tomorrow = await async_fetch_rce_day(sess, tomorrow)
                        all_rce.extend(records_tomorrow)

                    if all_rce:
                        self._rce_interval_records = all_rce
                        self._rce_records_last_fetch = now_local
                        if self.storage:
                            self.storage.save_market_prices(all_rce)

                if self._rce_interval_records:
                    now_utc = _dt.now(timezone.utc)
                    curr = next(
                        (r for r in self._rce_interval_records if r.interval_start_utc and r.interval_end_utc and r.interval_start_utc <= now_utc < r.interval_end_utc),
                        None
                    )
                    self._rce_current_record = curr or (self._rce_interval_records[-1] if self._rce_interval_records else None)
                    self._arbitrage_plan = self._arbitrage_engine.plan_day(self._rce_interval_records, target_date=today)
            except Exception as rce_dyn_err:
                _LOGGER.debug("Dynamic RCE auto-fetch skipped: %s", rce_dyn_err)

            # === v0.2.11 settlement calibration: rolling 365d + MTD sums ===

            await self.async_refresh_settlement(notify=False)

            # === Autoconsumption calculation (hour-synchronized with PV) ===
            await self.async_update_autoconsumption(active_meters)

            # === Hourly Profile WAL Forecasting in worker thread (v1.6.9) ===
            await self._async_update_profile_forecasts(active_meters)

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
        # v1.0.1: fresh startup fallback must be 1 day (today + yesterday),
        # never 30 days — a 30-day fetch takes >75s and exceeds HA's 60s setup watchdog.
        # Background auto-backfill handles the full 730-day history asynchronously.
        default_start = now - timedelta(days=1)

        # Find entity_id for this meter's import sensor
        registry = er.async_get(self.hass)
        entity_id = None

        # For G12w, check zone 1 sensor; for single zone, check import sensor
        target_unique_id = (
            f"energa_{meter_id}_import_1_stats"
            if has_zones
            else f"energa_{meter_id}_import_stats"
        )

        for entity in list(registry.entities.values()):
            if (
                entity.unique_id == target_unique_id
                and entity.platform == DOMAIN
            ):
                entity_id = entity.entity_id
                break

        if not entity_id:
            # Fall back to checking candidate statistic ID directly in recorder
            candidate_id = f"sensor.energa_{meter_id}_{'panel_energia_strefa_1' if has_zones else 'panel_energia_zuzycie'}"
            try:
                last_stats = await get_instance(self.hass).async_add_executor_job(
                    get_last_statistics, self.hass, 1, candidate_id, True, {"sum", "start"}
                )
                if candidate_id in last_stats and last_stats[candidate_id]:
                    last_ts = last_stats[candidate_id][0].get("start")
                    if last_ts:
                        if isinstance(last_ts, (int, float)):
                            last_dt = dt_util.utc_from_timestamp(last_ts).astimezone(tz)
                        else:
                            last_dt = last_ts.astimezone(tz)
                        start_date = last_dt + timedelta(hours=1)
                        _LOGGER.debug(
                            "Smart fetch for %s (candidate): last_stat=%s, start=%s",
                            candidate_id,
                            last_dt,
                            start_date,
                        )
                        return start_date
            except Exception as err:
                _LOGGER.debug("Candidate stats lookup failed for %s: %s", candidate_id, err)

            _LOGGER.debug(
                "No entity or statistics found for meter %s, using initial fallback (%s)",
                meter_id,
                default_start.date(),
            )
            return default_start

        # Query last statistic
        try:
            last_stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, entity_id, True, {"sum", "start"}
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

        suffix_to_name = {
            "import": "panel_energia_zuzycie",
            "import_1": "panel_energia_strefa_1",
            "import_2": "panel_energia_strefa_2",
            "export": "panel_energia_produkcja",
            "export_1": "panel_energia_produkcja_strefa_1",
            "export_2": "panel_energia_produkcja_strefa_2",
        }

        # Determine which suffixes to check
        if has_zones:
            suffixes = ["import_1", "import_2", "export_1", "export_2"]
        else:
            suffixes = ["import", "export"]

        for suffix in suffixes:
            unique_id = f"energa_{meter_id}_{suffix}_stats"

            candidates = [
                entity.entity_id
                for entity in list(registry.entities.values())
                if entity.unique_id == unique_id and entity.platform == DOMAIN
            ]
            if not candidates:
                energy_name = suffix_to_name.get(suffix, f"panel_{suffix}")
                candidates.append(f"sensor.energa_{meter_id}_{energy_name}")
                for m in getattr(self.api, "_meters_data", []):
                    if m.get("meter_point_id") == meter_id and m.get("meter_serial"):
                        candidates.append(f"sensor.energa_{m['meter_serial']}_{energy_name}")

            for entity_id in set(candidates):
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
                        self._pre_fetched_stats[entity_id] = last_stats[entity_id][0]
                        _LOGGER.debug(
                            "Pre-fetched stats for %s: sum=%.3f",
                            entity_id,
                            last_stats[entity_id][0].get("sum", 0),
                        )
                        break

                except Exception as err:
                    _LOGGER.debug(
                        "Could not pre-fetch stats for %s: %s", entity_id, err
                    )

    async def async_refresh_settlement(self, notify: bool = True) -> None:
        """Recompute settlement calibration (rolling 365d + MTD sums) locally without hitting API."""
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
                try:
                    _coeff_now = float(
                        _opts.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT)
                    )
                except (ValueError, TypeError):
                    _coeff_now = DEFAULT_PROSUMER_COEFFICIENT
                # Fetch from API if empty or first run for all meters (v1.5.0, v1.7.0)
                meter_ids_to_fetch = set()
                if self.api._meters_data:
                    for meter in self.api._meters_data:
                        mpid = str(meter.get("meter_point_id", ""))
                        if mpid:
                            meter_ids_to_fetch.add(mpid)
                for mid_key in list(self._meter_totals.keys()):
                    meter_ids_to_fetch.add(str(mid_key))

                for mid_str in meter_ids_to_fetch:
                    if not mid_str:
                        continue
                    if mid_str not in self._monthly or not self._monthly[mid_str]:
                        try:
                            api_monthly = await self.api.async_get_monthly_history(mid_str)
                            if api_monthly:
                                self._monthly[mid_str] = api_monthly
                                _LOGGER.info(
                                    "Energa: Loaded %d monthly flows directly from API for meter %s",
                                    len(api_monthly), mid_str,
                                )
                        except Exception as api_err:
                            _LOGGER.debug("API monthly history fetch failed for %s: %s", mid_str, api_err)

                _monthly = await self._async_compute_monthly_sums(_now)
                if _monthly:
                    for mid_str, m_data in _monthly.items():
                        if mid_str not in self._monthly:
                            self._monthly[mid_str] = m_data
                        else:
                            for ym_key, vals in m_data.items():
                                existing = self._monthly[mid_str].get(ym_key)
                                if not existing:
                                    self._monthly[mid_str][ym_key] = vals
                                else:
                                    ex_imp = float(existing.get("import", existing.get("import_1", 0) + existing.get("import_2", 0)))
                                    ex_exp = float(existing.get("export", existing.get("export_1", 0) + existing.get("export_2", 0)))
                                    rec_imp = float(vals.get("import", vals.get("import_1", 0) + vals.get("import_2", 0)))
                                    rec_exp = float(vals.get("export", vals.get("export_1", 0) + vals.get("export_2", 0)))
                                    # Only update if recorder has more complete or equal data (avoid regression from truncated DB)
                                    if (rec_imp >= ex_imp - 0.5) and (rec_exp >= ex_exp - 0.5):
                                        self._monthly[mid_str][ym_key].update(vals)

                # Ensure _mtd is complete: if local recorder coverage is less than current day of month,
                # hydrate from official API monthly history so dashboard MTD never displays truncated days
                cur_key = (_now.year, _now.month)
                for mid_str in meter_ids_to_fetch:
                    if not mid_str:
                        continue
                    m_dict = self._monthly.get(mid_str, {})
                    if cur_key in m_dict:
                        cur_m = m_dict[cur_key]
                        api_imp = float(cur_m.get("import", cur_m.get("import_1", 0) + cur_m.get("import_2", 0)))
                        api_exp = float(cur_m.get("export", cur_m.get("export_1", 0) + cur_m.get("export_2", 0)))

                        mtd_rec = self._mtd.get(mid_str, {})
                        rec_imp = float(mtd_rec.get("import", mtd_rec.get("import_1", 0) + mtd_rec.get("import_2", 0)))
                        rec_exp = float(mtd_rec.get("export", mtd_rec.get("export_1", 0) + mtd_rec.get("export_2", 0)))

                        # If API monthly has more complete totals than the recorder (e.g. recorder truncated or fresh start),
                        # or if recorder is empty, adopt the official API values:
                        if (api_imp > rec_imp + 0.5) or (api_exp > rec_exp + 0.5) or not mtd_rec:
                            _LOGGER.info(
                                "Energa: Hydrating MTD for meter %s from official API (API imp=%.2f exp=%.2f > Rec imp=%.2f exp=%.2f)",
                                mid_str, api_imp, api_exp, rec_imp, rec_exp,
                            )
                            hydrated_data = {
                                "import": api_imp,
                                "export": api_exp,
                                "import_1": float(cur_m.get("import_1", 0.0)),
                                "import_2": float(cur_m.get("import_2", 0.0)),
                                "export_1": float(cur_m.get("export_1", 0.0)),
                                "export_2": float(cur_m.get("export_2", 0.0)),
                                "_coverage_days": _now.day,
                                "_source": "energa_operator_api_monthly",
                            }
                            self._mtd[mid_str] = hydrated_data
                            # Also map to meter serial if known
                            if self.api._meters_data:
                                for m in self.api._meters_data:
                                    if str(m.get("meter_point_id")) == mid_str and m.get("meter_serial"):
                                        self._mtd[str(m["meter_serial"])] = hydrated_data

                # v1.9.0: hourly-netted salda (OSD "BP") — the base the seller
                # actually invoices. Attached AFTER hydration so both recorder
                # and API-hydrated MTD entries get them. Falls back silently to
                # gross flows when the recorder has no hourly data.
                _saldos = await self._async_compute_hourly_saldos(_month_start, _now)
                for mid_str, _s in _saldos.items():
                    self._mtd.setdefault(mid_str, {}).update(_s)
                    if self.api._meters_data:
                        for _m in self.api._meters_data:
                            if str(_m.get("meter_point_id")) == mid_str and _m.get("meter_serial"):
                                self._mtd.setdefault(str(_m["meter_serial"]), {}).update(_s)
                if notify:
                    self.async_update_listeners()
        except Exception as cal_err:
            _LOGGER.debug("Settlement refresh skipped: %s", cal_err)

    async def async_update_autoconsumption(self, active_meters: list[dict]) -> None:
        """Compute hour-synchronized autoconsumption metrics for active meters."""
        inverter_entity = self.entry.options.get(CONF_INVERTER_ENERGY_ENTITY)
        if not inverter_entity:
            return

        from datetime import timezone
        from decimal import Decimal

        from .autoconsumption import compute_autoconsumption_summary
        from .core.readings.models import IntervalReading
        from .ha.recorder_adapter import RecorderAdapter

        rec_adapter = RecorderAdapter(self.hass)
        now = datetime.now(timezone.utc)
        start_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # 1. Fetch hourly solar generation from inverter entity
        try:
            pv_hourly = await rec_adapter.async_get_hourly_statistics(
                inverter_entity, start_month, now
            )
        except Exception as err:
            _LOGGER.warning("Failed to fetch inverter hourly statistics for %s: %s", inverter_entity, err)
            pv_hourly = {}

        if not pv_hourly:
            _LOGGER.debug("No inverter hourly statistics found for %s", inverter_entity)
            return

        # 2. For each meter, gather interval readings
        for meter in active_meters:
            meter_id = str(meter["meter_point_id"])
            ppe_id = str(meter.get("ppe", meter_id))
            tariff = meter.get("tariff_name", "G12W")

            readings: list[IntervalReading] = []
            if self.storage:
                try:
                    readings = self.storage.get_readings(
                        ppe_id=ppe_id,
                        start_utc=start_month,
                        end_utc=now,
                        resolution="1h",
                        meter_id=meter_id,
                    )
                except Exception as err:
                    _LOGGER.debug("Error getting readings from storage for %s: %s", meter_id, err)

            # Fallback to coordinator's _hourly_stats if storage yielded no readings
            if not readings and meter_id in self._hourly_stats:
                h_stats = self._hourly_stats[meter_id]

                def _parse_pts(items):
                    d = {}
                    for it in items or []:
                        if isinstance(it, dict):
                            t = it.get("start")
                            v = it.get("state", 0.0)
                        elif isinstance(it, (tuple, list)) and len(it) >= 2:
                            t, v = it[0], it[1]
                        else:
                            continue
                        if hasattr(t, "tzinfo"):
                            d[t] = float(v or 0.0)
                    return d

                imp_z1 = _parse_pts(h_stats.get("import_1"))
                imp_z2 = _parse_pts(h_stats.get("import_2"))
                imp_tot = _parse_pts(h_stats.get("import"))

                exp_z1 = _parse_pts(h_stats.get("export_1"))
                exp_z2 = _parse_pts(h_stats.get("export_2"))
                exp_tot = _parse_pts(h_stats.get("export"))

                if not imp_tot and (imp_z1 or imp_z2):
                    for t in set(imp_z1.keys()).union(imp_z2.keys()):
                        imp_tot[t] = imp_z1.get(t, 0.0) + imp_z2.get(t, 0.0)

                if not exp_tot and (exp_z1 or exp_z2):
                    for t in set(exp_z1.keys()).union(exp_z2.keys()):
                        exp_tot[t] = exp_z1.get(t, 0.0) + exp_z2.get(t, 0.0)

                all_times = sorted(set(imp_tot.keys()).union(exp_tot.keys()))
                for dt_pt in all_times:
                    imp_val = imp_tot.get(dt_pt, 0.0)
                    exp_val = exp_tot.get(dt_pt, 0.0)
                    utc_dt = dt_pt if dt_pt.tzinfo is not None else dt_pt.replace(tzinfo=timezone.utc)
                    readings.append(
                        IntervalReading(
                            ppe_id=ppe_id,
                            meter_id=meter_id,
                            register="combined",
                            interval_start_utc=utc_dt,
                            resolution="1h",
                            import_kwh=Decimal(str(round(imp_val, 4))),
                            export_kwh=Decimal(str(round(exp_val, 4))),
                            quality="ok",
                            source="energa",
                        )
                    )

            if readings:
                summary = compute_autoconsumption_summary(
                    pv_hourly_map=pv_hourly,
                    energa_readings=readings,
                    tariff=tariff,
                    options=self.entry.options,
                    now_dt=now,
                )
                self._autoconsumption_summary[meter_id] = summary
                _LOGGER.info(
                    "Autoconsumption updated for meter %s: today=%.2f kWh, MTD=%.2f kWh (ratio: %.1f%%, saved: %.2f PLN)",
                    meter_id,
                    summary.today_kwh,
                    summary.mtd_kwh,
                    summary.autoconsumption_ratio_mtd,
                    summary.savings_mtd_pln,
                )

    async def _async_update_profile_forecasts(self, active_meters: list[dict]) -> None:
        """Compute HourlyProfileForecaster projections in executor worker thread to keep MainThread unblocked."""
        if not self.storage:
            return

        from datetime import date as _date
        from datetime import timezone
        today = _date.today()
        opts = self.entry.options

        def _calc_profile_in_worker(meter_dict: dict):
            mid_str = str(meter_dict.get("meter_point_id", ""))
            serial_str = str(meter_dict.get("meter_serial", mid_str))
            tariff_code = meter_dict.get("tariff")
            old_system = float(opts.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT)) >= 0.7
            coord_rce = self._rce_cache
            rce = float(coord_rce) if opts.get(CONF_RCE_AUTO_FETCH) and coord_rce is not None else float(opts.get(CONF_BANK_RCE_PRICE, DEFAULT_BANK_RCE_PRICE))

            wh_cover = 0.0
            if old_system:
                totals = self._meter_totals.get(mid_str)
                if totals:
                    try:
                        bi = float(opts.get(CONF_BALANCE_BASELINE_IMPORT, DEFAULT_BALANCE_BASELINE))
                        be = float(opts.get(CONF_BALANCE_BASELINE_EXPORT, DEFAULT_BALANCE_BASELINE))
                        coeff = float(opts.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT))
                        initial = float(opts.get(CONF_BANK_INITIAL_KWH, DEFAULT_BANK_INITIAL_KWH))
                        net_imp = float(totals.get("import", 0)) - bi
                        net_exp = float(totals.get("export", 0)) - be
                        wh_cover = max(0.0, net_exp * coeff - net_imp) + max(0.0, initial)
                    except (ValueError, TypeError):
                        wh_cover = 0.0

            try:
                from .projections.forecast import HourlyProfileForecaster
                canonical_readings = self.storage.get_readings(
                    ppe_id=mid_str,
                    meter_id=serial_str,
                    resolution="1h",
                )
                if not canonical_readings:
                    return mid_str, None

                forecaster = HourlyProfileForecaster(
                    readings=canonical_readings,
                    tariff_code=tariff_code,
                )
                if forecaster.history_days_count < 7:
                    return mid_str, None

                month_start_utc = datetime(today.year, today.month, 1, 0, 0, tzinfo=timezone.utc)
                mtd_readings = [
                    r for r in canonical_readings
                    if r.interval_start_utc >= month_start_utc
                ]
                profile_res = forecaster.forecast_month(
                    current_date=today,
                    mtd_readings=mtd_readings,
                    tariff_options=opts,
                    rce_price=rce,
                    warehouse_kwh=wh_cover if old_system else 0.0,
                    is_old_system=old_system,
                )
                return mid_str, profile_res
            except Exception as ex:
                _LOGGER.debug("Worker profile calculation failed for %s: %s", mid_str, ex)
                return mid_str, None

        for m in active_meters:
            try:
                mid_str, res = await self.hass.async_add_executor_job(_calc_profile_in_worker, m)
                if res is not None:
                    self._profile_forecast_cache[mid_str] = res
            except Exception as err:
                _LOGGER.debug("Async profile forecast failed for meter: %s", err)

    def _compute_period_sums_from_memory(self, start, end) -> dict:
        """Fallback: compute period sums directly from coordinator._hourly_stats in memory."""
        out: dict = {}
        tz = TIMEZONE
        start_dt = start if start.tzinfo is not None else start.replace(tzinfo=tz)
        end_dt = end if end.tzinfo is not None else end.replace(tzinfo=tz)

        for mid, series in getattr(self, "_hourly_stats", {}).items():
            if not isinstance(series, dict):
                continue
            mid_str = str(mid)
            meter_sums = {}
            for suffix, points in series.items():
                if not isinstance(points, list):
                    continue
                kwh_total = 0.0
                has_any = False
                for p in points:
                    if not isinstance(p, dict):
                        continue
                    p_start = p.get("start")
                    if not p_start:
                        continue
                    if isinstance(p_start, str):
                        try:
                            from datetime import datetime as _dt
                            p_start = _dt.fromisoformat(p_start)
                        except Exception:
                            continue
                    if p_start.tzinfo is None:
                        p_start = p_start.replace(tzinfo=tz)
                    if start_dt <= p_start <= end_dt:
                        try:
                            val = float(p.get("state", 0.0) or 0.0)
                            if val >= 0:
                                kwh_total += val
                                has_any = True
                        except (ValueError, TypeError):
                            continue
                if has_any:
                    meter_sums[suffix] = round(kwh_total, 3)

            if "import_1" in meter_sums or "import_2" in meter_sums:
                if "import" not in meter_sums:
                    meter_sums["import"] = round(
                        meter_sums.get("import_1", 0.0) + meter_sums.get("import_2", 0.0), 3
                    )
            if "export_1" in meter_sums or "export_2" in meter_sums:
                if "export" not in meter_sums:
                    meter_sums["export"] = round(
                        meter_sums.get("export_1", 0.0) + meter_sums.get("export_2", 0.0), 3
                    )

            if meter_sums:
                try:
                    span_days = max(1, (end_dt.date() - start_dt.date()).days)
                except Exception:
                    span_days = 1
                meter_sums["_coverage_days"] = span_days
                out[mid_str] = meter_sums
        return out

    async def _async_compute_period_sums(self, start, end) -> dict:
        """Sum Panel Energia statistics per meter over [start, end] (v0.2.11).

        Returns {meter_id: {suffix: delta_kwh, "_coverage_days": n}} where
        delta is last.sum - first.sum of daily statistics in the window.
        Falls back to in-memory hourly stats if recorder data is unavailable.
        """
        out: dict = {}
        try:
            import functools

            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self.hass)
            wanted: dict = {}  # entity_id -> (meter_id, suffix)
            for mid in list(self._meter_totals.keys()):
                for suffix in (
                    "import_1", "import_2", "export_1", "export_2",
                    "import", "export",
                ):
                    uid = f"energa_{mid}_{suffix}_stats"
                    for entity in list(registry.entities.values()):
                        if entity.unique_id == uid and entity.platform == DOMAIN:
                            wanted[entity.entity_id] = (str(mid), suffix)
                            break
            if wanted:
                stats = await get_instance(self.hass).async_add_executor_job(
                    functools.partial(
                        statistics_during_period,
                        self.hass, start, end, list(wanted.keys()), "day", None, {"sum"},
                    )
                )
                for stat_id, points in (stats or {}).items():
                    if not points:
                        continue
                    sums = [p.get("sum") for p in points if p.get("sum") is not None]
                    if not sums:
                        continue
                    mid, suffix = wanted[stat_id]
                    if len(sums) >= 2:
                        delta = reset_aware_delta(sums)
                    else:
                        # Single daily point available (e.g. 1st day of month or fresh start)
                        pt = points[0]
                        delta = pt.get("change") or pt.get("state") or 0.0
                        try:
                            delta = max(0.0, float(delta))
                        except (ValueError, TypeError):
                            delta = 0.0
                    out.setdefault(str(mid), {})[suffix] = delta
                    span = self._stat_span_days(points)
                    prev = out[str(mid)].get("_coverage_days")
                    out[str(mid)]["_coverage_days"] = (
                        span if prev is None else min(prev, span)
                    )
        except Exception as err:
            _LOGGER.debug("Period sums from recorder failed: %s", err)

        # Fallback to in-memory hourly stats if recorder had no data for any meter
        try:
            mem_sums = self._compute_period_sums_from_memory(start, end)
            for mid_str, series in mem_sums.items():
                if mid_str not in out or not out[mid_str]:
                    out[mid_str] = series
                else:
                    for suffix, val in series.items():
                        if suffix not in out[mid_str]:
                            out[mid_str][suffix] = val
        except Exception as mem_err:
            _LOGGER.debug("In-memory period sums fallback failed: %s", mem_err)

        return out

    async def _async_compute_hourly_saldos(self, start, end) -> dict:
        """Hourly-netted invoice bases (net-billing) per meter over [start, end].

        The Energa invoice settles gross import and export **hour by hour**
        (OSD "BP"/bilansowanie prosumentów): the energy + variable-distribution
        base is the sum of hourly POSITIVE balances ("salda dodatnie"), the
        deposit base is the sum of NEGATIVE ones, and excise is charged on the
        "nakładka" (gross import − salda dodatnie). Recorder hourly statistics
        of the four zone series are paired per hour via ``tariff.bill_saldos``.

        Returns {meter_id: {saldo_plus_1/2, saldo_minus_1/2, gross_1/2,
        overlap_1/2, total_plus}}. Fully defensive — empty dict when the
        recorder has no hourly data (caller then falls back to gross flows).
        """
        out: dict = {}
        try:
            from homeassistant.helpers import entity_registry as er

            from .ha.recorder_adapter import RecorderAdapter
            from .tariff import bill_saldos

            registry = er.async_get(self.hass)
            wanted: dict = {}  # meter_id -> {suffix: statistic_id}
            for mid in list(self._meter_totals.keys()):
                for suffix in ("import_1", "export_1", "import_2", "export_2"):
                    uid = f"energa_{mid}_{suffix}_stats"
                    for entity in list(registry.entities.values()):
                        if entity.unique_id == uid and entity.platform == DOMAIN:
                            wanted.setdefault(str(mid), {})[suffix] = entity.entity_id
                            break
            if not wanted:
                return out

            adapter = RecorderAdapter(self.hass)
            for mid, series_ids in wanted.items():
                hourly: dict = {}
                for suffix, stat_id in series_ids.items():
                    hourly[suffix] = await adapter.async_get_hourly_statistics(
                        stat_id, start, end
                    )
                saldos = bill_saldos(hourly)
                if saldos:
                    out[mid] = saldos
        except Exception as err:
            _LOGGER.debug("Hourly salda computation failed: %s", err)
        return out

    async def _async_compute_monthly_sums(self, end, months: int = 14) -> dict:
        """Per-month Panel Energia sums per meter for the FIFO bank (v0.2.20).

        Returns {meter_id: {(year, month): {suffix: delta_kwh}}}.
        Sequential small recorder queries in the executor; fully defensive —
        settlement calibration must never break the coordinator update.
        """
        from datetime import datetime as _dt

        out: dict = {}
        try:
            y, m = end.year, end.month
            bounds = []
            for _ in range(max(1, int(months))):
                bounds.append((y, m))
                m -= 1
                if m < 1:
                    m = 12
                    y -= 1
            bounds.reverse()
            for (by, bm) in bounds:
                # Caching: past closed months never change retroactively (v1.6.9).
                # If all meters already have this past month in memory, reuse it directly without querying recorder.
                is_past_closed_month = (by < end.year) or (by == end.year and bm < end.month)
                all_cached = (
                    is_past_closed_month
                    and bool(self._monthly)
                    and all(
                        mid_str in self._monthly and (by, bm) in self._monthly[mid_str]
                        for mid_str in [str(k) for k in self._meter_totals.keys()]
                    )
                )
                if all_cached:
                    for mid_str in [str(k) for k in self._meter_totals.keys()]:
                        cached_val = self._monthly[mid_str].get((by, bm))
                        if cached_val:
                            out.setdefault(mid_str, {})[(by, bm)] = cached_val
                    continue

                ms = _dt(by, bm, 1, tzinfo=end.tzinfo)
                me = _dt(by + 1, 1, 1, tzinfo=end.tzinfo) if bm == 12 else _dt(by, bm + 1, 1, tzinfo=end.tzinfo)
                if me > end:
                    me = end
                if ms >= me:
                    continue
                sums = await self._async_compute_period_sums(ms, me)
                for mid, vals in (sums or {}).items():
                    per = {k: v for k, v in vals.items() if not str(k).startswith("_")}
                    if per:
                        out.setdefault(str(mid), {})[(by, bm)] = per
            return out
        except Exception as err:
            _LOGGER.debug("Monthly sums failed: %s", err)
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
