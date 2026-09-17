"""Service registration, backfill, and history import for Energa My Meter integration."""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from time import monotonic
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import EnergaAPI
from .const import (
    CONF_BANK_INITIAL_KWH,
    CONF_BANK_INITIAL_KWH_L1,
    CONF_BANK_INITIAL_KWH_L2,
    CONF_BANK_RCE_PRICE,
    CONF_CREATE_SETTLEMENT_DASHBOARD,
    CONF_ENABLE_SYNTHETIC_STORAGE,
    CONF_PROSUMER_COEFFICIENT,
    DEFAULT_BANK_RCE_PRICE,
    DEFAULT_CREATE_SETTLEMENT_DASHBOARD,
    DEFAULT_ENABLE_SYNTHETIC_STORAGE,
    DEFAULT_PROSUMER_COEFFICIENT,
    DOMAIN,
    MAX_HOURLY_KWH,
    get_price_for_key,
    get_prosumer_coefficient,
)
from .core.verification import (
    PERIOD_KWH_KEYS,
    SOURCE_ENERGA_API,
    SOURCE_RECORDER,
    build_period_invoice,
    choose_period_source,
    opening_bank_from_monthly_flows,
    period_is_historical,
)
from .dashboard_generator import (
    DEFAULT_ICON,
    DEFAULT_TITLE,
    DEFAULT_URL_PATH,
    async_provision_dashboard,
)
from .tariff import fees_from_options

_LOGGER = logging.getLogger(__name__)
TIMEZONE = ZoneInfo("Europe/Warsaw")
AUTO_HISTORY_DAYS = 730

# Backfill progress notifications (restored v1.9.0): one notification per meter,
# refreshed no more often than every ``BACKFILL_PROGRESS_INTERVAL_S`` seconds,
# dismissed automatically a while after the import ends.
BACKFILL_NOTIFICATION_TITLE = "Energa: Import Historii"
BACKFILL_OVERVIEW_TITLE = "Energa: Pobieranie historii"
BACKFILL_OVERVIEW_NOTIFICATION_ID = "energa_auto_backfill"
BACKFILL_PROGRESS_INTERVAL_S = 45.0
BACKFILL_DISMISS_DELAY_S = 120.0

# Recorder statistic entity names for the per-zone energy series.
_PERIOD_STAT_NAME = {
    "import": "panel_energia_zuzycie",
    "import_1": "panel_energia_strefa_1",
    "import_2": "panel_energia_strefa_2",
    "export": "panel_energia_produkcja",
    "export_1": "panel_energia_produkcja_strefa_1",
    "export_2": "panel_energia_produkcja_strefa_2",
}


def _get_entry_and_api(
    hass: HomeAssistant, meter_id: str | None = None
) -> tuple[ConfigEntry | None, EnergaAPI | None]:
    """Retrieve the most appropriate config entry and API instance for service calls."""
    domain_data = hass.data.get(DOMAIN, {})
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return None, None

    if len(entries) == 1 or not meter_id:
        entry = entries[0]
        api = domain_data.get(entry.entry_id, {}).get("api")
        return entry, api

    # If meter_id specified and multiple entries, find matching entry
    for entry in entries:
        edata = domain_data.get(entry.entry_id, {})
        coord = edata.get("coordinator")
        if coord and coord.data:
            for m in coord.data:
                if (
                    str(m.get("meter_point_id")) == str(meter_id)
                    or str(m.get("meter_serial")) == str(meter_id)
                ):
                    return entry, edata.get("api")

    entry = entries[0]
    return entry, domain_data.get(entry.entry_id, {}).get("api")


async def async_register_services(hass: HomeAssistant) -> None:
    """Register all domain services for Energa My Meter."""
    if hass.services.has_service(DOMAIN, "fetch_history"):
        return

    async def fetch_history_service(call: ServiceCall) -> None:
        """Service to manually fetch historical data (background)."""
        start_date_str = call.data["start_date"]
        days = call.data.get("days", 30)
        meter_id = call.data.get("meter_id")
        _LOGGER.info(
            "fetch_history called: start=%s days=%s meter_id=%s",
            start_date_str,
            days,
            meter_id,
        )

        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
        except ValueError:
            _LOGGER.error(
                "Invalid date format: %s (expected YYYY-MM-DD)", start_date_str
            )
            persistent_notification.async_create(
                hass,
                f"Błędny format daty: {start_date_str}",
                title="Energa: Błąd",
                notification_id="energa_fetch_error",
            )
            return

        entry, api = _get_entry_and_api(hass, meter_id)
        if not api or not entry:
            _LOGGER.error("No active Energa API session found for fetch_history")
            persistent_notification.async_create(
                hass,
                "Brak aktywnej sesji Energa API. Upewnij się, że integracja jest załadowana.",
                title="Energa: Błąd",
                notification_id="energa_fetch_error",
            )
            return

        try:
            meters = await api.async_get_data(force_refresh=True)
        except Exception as err:
            _LOGGER.error("Failed to fetch meter data: %s", err)
            persistent_notification.async_create(
                hass,
                f"Nie można pobrać danych licznika: {err}",
                title="Energa: Błąd",
                notification_id="energa_fetch_error",
            )
            return

        active_meters = [
            m
            for m in meters
            if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
        ]

        if meter_id:
            active_meters = [
                m
                for m in active_meters
                if str(m.get("meter_serial")) == str(meter_id)
                or str(m.get("meter_point_id")) == str(meter_id)
            ]

        if not active_meters:
            _LOGGER.warning("No active meters found")
            persistent_notification.async_create(
                hass,
                "Nie znaleziono aktywnych liczników",
                title="Energa: Ostrzeżenie",
                notification_id="energa_fetch_warning",
            )
            return

        _LOGGER.info(
            "fetch_history: importing %d meter(s) from %s (%d days)",
            len(active_meters),
            start_date.date(),
            days,
        )

        for meter in active_meters:
            hass.async_create_task(
                _import_meter_history(hass, api, meter, start_date, days, entry)
            )

    hass.services.async_register(
        DOMAIN,
        "fetch_history",
        fetch_history_service,
        schema=vol.Schema(
            {
                vol.Required("start_date"): str,
                vol.Optional("days", default=30): int,
                vol.Optional("meter_id"): str,
            }
        ),
    )

    async def generate_dashboard_service(call: ServiceCall) -> None:
        """Service to generate or refresh the Energa Lovelace dashboard."""
        url_path = call.data.get("url_path", DEFAULT_URL_PATH)
        title = call.data.get("title", DEFAULT_TITLE)
        icon = call.data.get("icon", DEFAULT_ICON)
        meter_id = call.data.get("meter_id")

        entry, api = _get_entry_and_api(hass, meter_id)
        if not entry or not api:
            _LOGGER.error("No active Energa entry found for generate_dashboard")
            return

        coeff = float(
            entry.options.get(
                CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT
            )
        )

        try:
            meters_list = await api.async_get_data(force_refresh=False)
        except Exception as err:
            _LOGGER.error("Failed to fetch meters for dashboard generation: %s", err)
            meters_list = []

        active_meters = [
            m
            for m in meters_list
            if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
        ]

        if meter_id:
            active_meters = [
                m
                for m in active_meters
                if str(m.get("meter_serial")) == str(meter_id)
                or str(m.get("meter_point_id")) == str(meter_id)
            ]

        await async_provision_dashboard(
            hass,
            active_meters,
            url_path=url_path,
            title=title,
            icon=icon,
            coeff=coeff,
        )

    hass.services.async_register(
        DOMAIN,
        "generate_dashboard",
        generate_dashboard_service,
        schema=vol.Schema(
            {
                vol.Optional("url_path", default=DEFAULT_URL_PATH): str,
                vol.Optional("title", default=DEFAULT_TITLE): str,
                vol.Optional("icon", default=DEFAULT_ICON): str,
                vol.Optional("meter_id"): str,
            }
        ),
    )

    async def reconcile_invoice_service(call: ServiceCall) -> None:
        """Service to reconcile seller invoice lines and audit variances."""
        inv_number = str(call.data["invoice_number"])
        period_start_s = str(call.data["period_start"])
        period_end_s = str(call.data["period_end"])
        tariff_code = str(call.data.get("tariff", "G11")).upper()
        consumption_kwh = Decimal(str(call.data.get("consumption_kwh", "0.0")))
        months = Decimal(str(call.data.get("months", "1.0")))
        invoiced_gross = (
            Decimal(str(call.data["invoiced_gross"]))
            if "invoiced_gross" in call.data
            else None
        )
        invoiced_lines = call.data.get("invoiced_lines") or []
        ppe_id = str(call.data.get("ppe_id", ""))

        d_start = datetime.strptime(period_start_s, "%Y-%m-%d").date()
        d_end = datetime.strptime(period_end_s, "%Y-%m-%d").date()

        from .core.tariffs.effective_tariffs import (
            calculate_g11_invoice_lines,
            calculate_g12w_invoice_lines,
            reconcile_invoice,
        )

        if "12" in tariff_code:
            day_kwh = Decimal(str(call.data.get("day_kwh", consumption_kwh / 2)))
            night_kwh = Decimal(str(call.data.get("night_kwh", consumption_kwh / 2)))
            comp_lines = calculate_g12w_invoice_lines(
                day_kwh, night_kwh, months=months, effective_date=d_start
            )
        else:
            comp_lines = calculate_g11_invoice_lines(
                consumption_kwh, months=months, effective_date=d_start
            )

        report = reconcile_invoice(
            invoice_number=inv_number,
            period_start=d_start,
            period_end=d_end,
            computed_lines=comp_lines,
            invoiced_lines=invoiced_lines,
            header_invoiced_gross=invoiced_gross,
        )

        # Save to canonical storage
        entry, _ = _get_entry_and_api(hass)
        if entry:
            storage_inst = (
                hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("storage")
            )
            if storage_inst:
                await hass.async_add_executor_job(
                    storage_inst.save_invoice_reconciliation, report, ppe_id
                )

        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "notification_id": f"energa_recon_{inv_number.replace('/', '_')}",
                "title": f"Rekonsyliacja faktury {inv_number}: {report.status}",
                "message": (
                    f"**Faktura:** {inv_number}\n"
                    f"**Status:** {report.status}\n"
                    f"**Wyliczona kwota:** {report.computed_gross} PLN\n"
                    f"**Kwota z faktury:** {report.invoiced_gross} PLN\n"
                    f"**Różnica:** {report.variance_gross} PLN ({report.variance_percent}%)\n\n"
                    f"Szczegóły zapisane w kanonicznym storage audytowym."
                ),
            },
        )

    hass.services.async_register(
        DOMAIN,
        "reconcile_invoice",
        reconcile_invoice_service,
        schema=vol.Schema(
            {
                vol.Required("invoice_number"): str,
                vol.Required("period_start"): str,
                vol.Required("period_end"): str,
                vol.Optional("tariff", default="G11"): str,
                vol.Optional("consumption_kwh", default=0.0): vol.Coerce(float),
                vol.Optional("day_kwh", default=0.0): vol.Coerce(float),
                vol.Optional("night_kwh", default=0.0): vol.Coerce(float),
                vol.Optional("months", default=1.0): vol.Coerce(float),
                vol.Optional("invoiced_gross"): vol.Coerce(float),
                vol.Optional("invoiced_lines", default=[]): list,
                vol.Optional("ppe_id", default=""): str,
            }
        ),
    )

    async def verify_period_service(call: ServiceCall) -> dict:
        """Recompute a full invoice for [start, end] from API or recorder.

        Faza 2: when ``entry_id`` + ``meter_id`` are supplied and the period
        is historical (or the recorder has no data), the Energa API is the
        preferred source; otherwise the hourly recorder statistics are used.
        The response reports ``source`` and ``cached``. Faza 3 opening
        balances: the net-metering warehouse is reconstructed from prior
        monthly flows; ``bank_open_1/2`` and ``deposit_open_pln`` are optional
        manual overrides. RCEm from the call, coordinator cache, Options or
        the last-known default. Always returns a JSON-serialisable dict; an
        empty window yields ``empty: true`` with an ``error`` and zeros.
        """
        return await _async_verify_period(hass, call)

    hass.services.async_register(
        DOMAIN,
        "verify_period",
        verify_period_service,
        schema=vol.Schema(
            {
                vol.Required("start"): vol.Coerce(str),
                vol.Required("end"): vol.Coerce(str),
                vol.Optional("entry_id"): str,
                vol.Optional("meter_id"): str,
                vol.Optional("rcem_pln"): vol.Coerce(float),
                vol.Optional("rcem"): vol.Coerce(float),
                vol.Optional("bank_open_1"): vol.Coerce(float),
                vol.Optional("bank_open_2"): vol.Coerce(float),
                vol.Optional("deposit_open_pln"): vol.Coerce(float),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister domain services when all entries are removed."""
    for service_name in (
        "fetch_history",
        "generate_dashboard",
        "reconcile_invoice",
        "verify_period",
    ):
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)


def _parse_period_datetime(value) -> tuple[datetime, bool]:
    """Parse a service ``start``/``end`` value into a tz-aware datetime.

    Returns ``(datetime, is_date_only)``. Date-only values are treated as
    local midnight; the caller decides whether to extend an end date to the
    exclusive next midnight.
    """
    if isinstance(value, datetime):
        dt = value
        is_date_only = False
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
        is_date_only = True
    else:
        text = str(value or "").strip()
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            dt = datetime.strptime(text, "%Y-%m-%d")
            is_date_only = True
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            is_date_only = False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TIMEZONE)
    return dt, is_date_only


def _period_months(start: datetime, end: datetime) -> int:
    """Number of monthly fixed fees for a period (at least 1)."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day > start.day:
        months += 1
    return max(1, months)


def _resolve_verify_entry(
    hass: HomeAssistant, entry_id: str | None
) -> tuple[ConfigEntry | None, object | None, EnergaAPI | None]:
    """Resolve the config entry plus coordinator/API for verify_period."""
    domain_data = hass.data.get(DOMAIN, {})
    entries = hass.config_entries.async_entries(DOMAIN)
    entry: ConfigEntry | None = None
    if entry_id:
        entry = next((e for e in entries if e.entry_id == entry_id), None)
    if entry is None and entries:
        entry = entries[0]
    if entry is None:
        return None, None, None
    edata = domain_data.get(entry.entry_id, {})
    return entry, edata.get("coordinator"), edata.get("api")


def _resolve_period_rcem(entry: ConfigEntry, coordinator, data: dict) -> float:
    """RCEm from call data, then coordinator cache, then Options/default."""
    for key in ("rcem_pln", "rcem"):
        val = (data or {}).get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    cached = getattr(coordinator, "_rce_cache", None) if coordinator is not None else None
    if cached is not None:
        try:
            return float(cached)
        except (ValueError, TypeError):
            pass
    try:
        return float(
            (entry.options or {}).get(CONF_BANK_RCE_PRICE, DEFAULT_BANK_RCE_PRICE)
        )
    except (ValueError, TypeError):
        return float(DEFAULT_BANK_RCE_PRICE)


def _meter_coefficient(entry: ConfigEntry, meter: dict) -> float:
    """Prosumer opust coefficient for a meter (never raises)."""
    meter_id = str(meter.get("meter_point_id", ""))
    serial = str(meter.get("meter_serial", meter_id))
    try:
        return float(
            get_prosumer_coefficient(
                dict(entry.options or {}), meter_id, serial=serial
            )
        )
    except Exception:  # noqa: BLE001 - never break a service call on bad options
        return float(DEFAULT_PROSUMER_COEFFICIENT)


def _meter_old_system(entry: ConfigEntry, meter: dict) -> bool:
    """True for old net-metering (coefficient >= 0.7)."""
    return _meter_coefficient(entry, meter) >= 0.7


async def _active_meters_for_period(
    hass: HomeAssistant,
    api: EnergaAPI | None,
    coordinator,
    meter_id: str | None,
) -> list[dict]:
    """Return active meters from the coordinator cache (or the API)."""
    meters = getattr(coordinator, "data", None) if coordinator is not None else None
    if not meters and api is not None:
        try:
            meters = await api.async_get_data(force_refresh=False)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("verify_period meter fetch failed: %s", err)
            meters = []
    active = [
        m
        for m in (meters or [])
        if m.get("total_plus") and float(m.get("total_plus", 0) or 0) > 0
    ]
    if meter_id:
        active = [
            m
            for m in active
            if str(m.get("meter_serial")) == str(meter_id)
            or str(m.get("meter_point_id")) == str(meter_id)
        ]
    return active


def _statistic_id_for(
    hass: HomeAssistant, meter_point_id: str, serial: str, suffix: str
) -> str:
    """Resolve the recorder statistic_id for a meter zone.

    Prefers the entity registry (handles renamed entities) via the known
    ``energa_<mid>_<suffix>_stats`` unique_id, then falls back to the
    conventional ``sensor.energa_<serial>_panel_energia_*`` id.
    """
    uid = f"energa_{meter_point_id}_{suffix}_stats"
    try:
        eid = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, uid)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("verify_period registry lookup failed for %s: %s", uid, err)
        eid = None
    if eid:
        return eid
    name = _PERIOD_STAT_NAME.get(suffix, f"panel_{suffix}")
    return f"sensor.energa_{serial}_{name}".lower()


async def _collect_meter_hourly(
    hass: HomeAssistant, meter: dict, start: datetime, end: datetime
) -> dict:
    """Build {zone: {epoch_hour: kWh}} from recorder hourly statistics."""
    from .settlement import is_export_prosumer

    meter_point_id = str(meter.get("meter_point_id", ""))
    serial = str(meter.get("meter_serial", meter_point_id)).lower()
    has_zones = meter.get("zone_count", 1) > 1

    if has_zones:
        suffixes = ["import_1", "import_2"]
        if is_export_prosumer(meter):
            suffixes += ["export_1", "export_2"]
    else:
        suffixes = ["import"]
        if is_export_prosumer(meter):
            suffixes.append("export")

    statistic_ids = {
        suffix: _statistic_id_for(hass, meter_point_id, serial, suffix)
        for suffix in suffixes
    }

    raw: dict = {}
    try:
        raw = await get_instance(hass).async_add_executor_job(
            functools.partial(
                statistics_during_period,
                hass,
                start,
                end,
                list(statistic_ids.values()),
                "hour",
                None,
                {"state"},
            )
        ) or {}
    except Exception as err:  # noqa: BLE001 - missing recorder must not raise
        _LOGGER.debug("verify_period statistics query failed: %s", err)

    collected: dict[str, dict[int, float]] = {}
    for suffix, statistic_id in statistic_ids.items():
        hour_map: dict[int, float] = {}
        for row in raw.get(statistic_id, []) or []:
            ts = row.get("start")
            state = row.get("state")
            if ts is None or state is None:
                continue
            try:
                hour_map[int(float(ts))] = float(state)
            except (ValueError, TypeError):
                continue
        collected[suffix] = hour_map

    hourly: dict[str, dict[int, float]] = {}
    if has_zones:
        hourly["import_1"] = collected.get("import_1", {})
        hourly["import_2"] = collected.get("import_2", {})
        if "export_1" in collected:
            hourly["export_1"] = collected["export_1"]
        if "export_2" in collected:
            hourly["export_2"] = collected["export_2"]
    else:
        hourly["import_1"] = collected.get("import", {})
        if "export" in collected:
            hourly["export_1"] = collected["export"]
    return hourly


async def _collect_meter_hourly_api(
    api: EnergaAPI | None, meter: dict, start: datetime, end: datetime
) -> dict:
    """Build {zone: {epoch_hour: kWh}} from the Energa API (Faza 2).

    Best effort: any API error returns ``{}`` so the caller falls back to the
    recorder series. The shape matches :func:`_collect_meter_hourly`.
    """
    if api is None:
        return {}
    meter_point_id = str(meter.get("meter_point_id", ""))
    if not meter_point_id:
        return {}
    try:
        result = await api.async_get_hourly_range(meter_point_id, start, end)
    except Exception as err:  # noqa: BLE001 - API fallback must not break the service
        _LOGGER.debug("verify_period API fetch failed for %s: %s", meter_point_id, err)
        return {}
    if not isinstance(result, dict):
        return {}
    cleaned: dict[str, dict[int, float]] = {}
    for zone, series in result.items():
        if not isinstance(series, dict):
            continue
        hour_map: dict[int, float] = {}
        for ts, val in series.items():
            try:
                hour_map[int(float(ts))] = float(val)
            except (ValueError, TypeError):
                continue
        if hour_map:
            cleaned[zone] = hour_map
    return cleaned


def _stat_row_moment(value):
    """Normalise a statistics ``start`` field (datetime or epoch) to local."""
    try:
        if isinstance(value, datetime):
            moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return moment.astimezone(TIMEZONE)
        moment = dt_util.utc_from_timestamp(float(value))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(TIMEZONE)
    except (ValueError, TypeError, AttributeError):
        return None


async def _collect_monthly_flows(
    hass: HomeAssistant, meter: dict, start: datetime, *, months: int = 14
) -> dict:
    """Monthly import/export flows preceding ``start`` from recorder stats.

    Used for Faza 3 opening balances: the net-metering warehouse at the start
    of the verified period is the FIFO result over these monthly flows.

    Returns ``{(year, month): {suffix: kWh}}``; an empty dict means the
    recorder has no usable history (the caller then reports ``coverage_unknown``
    instead of guessing).
    """
    from .settlement import is_export_prosumer

    meter_point_id = str(meter.get("meter_point_id", ""))
    serial = str(meter.get("meter_serial", meter_point_id)).lower()
    has_zones = meter.get("zone_count", 1) > 1

    if has_zones:
        suffixes = ["import_1", "import_2"]
    else:
        suffixes = ["import"]
    if is_export_prosumer(meter):
        suffixes += [s.replace("import", "export") for s in list(suffixes)]

    statistic_ids = {
        suffix: _statistic_id_for(hass, meter_point_id, serial, suffix)
        for suffix in suffixes
    }
    lookback_start = start - timedelta(days=31 * max(1, int(months)))
    try:
        raw = await get_instance(hass).async_add_executor_job(
            functools.partial(
                statistics_during_period,
                hass,
                lookback_start,
                start,
                list(statistic_ids.values()),
                "day",
                None,
                {"state"},
            )
        ) or {}
    except Exception as err:  # noqa: BLE001 - missing recorder must not raise
        _LOGGER.debug("verify_period monthly statistics query failed: %s", err)
        return {}

    monthly: dict = {}
    for suffix, statistic_id in statistic_ids.items():
        for row in raw.get(statistic_id, []) or []:
            moment = _stat_row_moment(row.get("start"))
            state = row.get("state")
            if moment is None or state is None:
                continue
            try:
                value = float(state)
            except (ValueError, TypeError):
                continue
            bucket = monthly.setdefault((moment.year, moment.month), {})
            bucket[suffix] = bucket.get(suffix, 0.0) + value
    return monthly


def _verify_cache_get(coordinator, key: tuple) -> dict | None:
    """Return a cached invoice for ``key`` when a real dict cache exists."""
    cache = getattr(coordinator, "_verify_cache", None)
    if not isinstance(cache, dict):
        return None
    cached = cache.get(key)
    return cached if isinstance(cached, dict) else None


def _verify_cache_put(coordinator, key: tuple, result: dict) -> None:
    """Store one invoice result in the coordinator's in-memory cache.

    Deliberately in-memory (no canonical SQLite persistence yet): repeated
    presses/API calls within a session are instant, and ``cached: true`` is
    reported honestly. Cross-restart persistence is a Faza 3 follow-up.
    """
    if coordinator is None:
        return
    cache = getattr(coordinator, "_verify_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            coordinator._verify_cache = cache
        except Exception:  # noqa: BLE001 - never break the service on cache set
            return
    cache[key] = result


def _empty_period_result(meter: dict, old_system: bool) -> dict:
    """Zeroed, JSON-safe breakdown for a meter with no data in the window."""
    meter_point_id = str(meter.get("meter_point_id", ""))
    serial = str(meter.get("meter_serial", meter_point_id))
    kwh = {key: 0.0 for key in PERIOD_KWH_KEYS}
    kwh.update({"bank_open_1": None, "bank_open_2": None,
                "bank_close_1": None, "bank_close_2": None})
    return {
        "empty": True,
        "error": "no_data",
        "meter_point_id": meter_point_id,
        "meter_serial": serial,
        "old_system": bool(old_system),
        "system": "net_metering" if old_system else "net_billing",
        "sale_energy_day": 0.0,
        "sale_energy_night": 0.0,
        "excise_day": 0.0,
        "excise_night": 0.0,
        "excise": 0.0,
        "trade_fee": 0.0,
        "distr_var_day": 0.0,
        "distr_var_night": 0.0,
        "distr_quality": 0.0,
        "distr_oze": 0.0,
        "distr_cogen": 0.0,
        "distr_abonament": 0.0,
        "distr_grid_fixed": 0.0,
        "distr_capacity": 0.0,
        "distr_fixed": 0.0,
        "distr_total": 0.0,
        "netto": 0.0,
        "vat": 0.0,
        "brutto": 0.0,
        "deposit": 0.0,
        "deposit_generated": 0.0,
        "deposit_open": None,
        "deposit_applied": 0.0,
        "deposit_close": 0.0,
        "do_zaplaty": 0.0,
        "coverage_unknown": True,
        "warnings": ["Brak danych w wybranym okresie."],
        "kwh": kwh,
    }


async def _async_verify_period(hass: HomeAssistant, call: ServiceCall) -> dict:
    """Implementation of the ``energa_mobile.verify_period`` service."""
    return await async_verify_period_data(hass, dict(call.data or {}))


async def async_verify_period_data(hass: HomeAssistant, data: dict) -> dict:
    """Recompute a full invoice for [start, end] for one or all meters.

    Faza 1 used hourly recorder statistics only. Faza 2 adds the Energa API
    as the preferred source when a concrete meter is requested
    (``entry_id`` + ``meter_id``) and the period is historical — or when the
    recorder has no data for the window. If the API yields nothing, the
    recorder series is used as fallback. RCEm resolution, fee tables and the
    invoice math (``core.verification.build_period_invoice``) are unchanged.

    Faza 3 resolves the opening balances: for net-metering the service
    reconstructs the kWh warehouse at the start of the period from the prior
    monthly recorder flows (FIFO 12-month); for net-billing the opening
    deposit is taken from ``deposit_open_pln`` when provided (per-month
    historical RCEm is Faza 4). Manual overrides ``bank_open_1/2`` and
    ``deposit_open_pln`` are honoured. Missing balances yield
    ``coverage_unknown=true`` plus a warning, never a fake invoice parity.

    The response carries ``source`` (``energa_api`` | ``recorder_hourly`` |
    ``mixed``) and ``cached``. Results are memoised per (meter, period, RCEm,
    opening balances) in an in-memory coordinator cache for the session;
    cross-restart persistence in the canonical SQLite store is a follow-up
    (never reported as cached when it is not).

    Always returns a JSON-serialisable dict; an empty window yields
    ``empty: true`` with an ``error`` and zeros instead of raising.
    """
    data = dict(data or {})
    try:
        start_dt, _ = _parse_period_datetime(data.get("start"))
        end_dt, end_is_date = _parse_period_datetime(data.get("end"))
    except (ValueError, TypeError):
        return {"empty": True, "error": "invalid_period", "source": SOURCE_RECORDER}
    if end_is_date:
        end_dt = end_dt + timedelta(days=1)
    if end_dt <= start_dt:
        return {"empty": True, "error": "invalid_period", "source": SOURCE_RECORDER}

    entry, coordinator, api = _resolve_verify_entry(hass, data.get("entry_id"))
    if entry is None:
        return {"empty": True, "error": "no_entry", "source": SOURCE_RECORDER}

    rcem = _resolve_period_rcem(entry, coordinator, data)
    months = _period_months(start_dt, end_dt)
    meters = await _active_meters_for_period(
        hass, api, coordinator, data.get("meter_id")
    )

    base: dict = {
        "entry_id": entry.entry_id,
        "period_start": start_dt.isoformat(),
        "period_end": end_dt.isoformat(),
        "rcem": float(rcem),
        "months": months,
        "cached": False,
    }

    if not meters:
        return {
            **base,
            "empty": True,
            "error": "no_meter",
            "source": SOURCE_RECORDER,
            "meters": [],
        }

    api_available = api is not None and bool(data.get("meter_id"))
    historical = period_is_historical(start_dt, end_dt)

    def _optional_float(key):
        value = data.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    override_bank_1 = _optional_float("bank_open_1")
    override_bank_2 = _optional_float("bank_open_2")
    override_deposit = _optional_float("deposit_open_pln")

    results: list[dict] = []
    for meter in meters:
        old_system = _meter_old_system(entry, meter)
        coefficient = _meter_coefficient(entry, meter)
        has_zones = meter.get("zone_count", 1) > 1
        meter_point_id = str(meter.get("meter_point_id", ""))
        serial = str(meter.get("meter_serial", meter_point_id))

        cache_key = (
            meter_point_id,
            start_dt.isoformat(),
            end_dt.isoformat(),
            float(rcem),
            override_bank_1,
            override_bank_2,
            override_deposit,
        )
        cached = _verify_cache_get(coordinator, cache_key)
        if cached is not None:
            results.append({**cached, "cached": True})
            continue

        hourly = await _collect_meter_hourly(hass, meter, start_dt, end_dt)
        recorder_points = sum(len(zone) for zone in hourly.values())
        source = choose_period_source(
            api_available=api_available,
            historical=historical,
            recorder_empty=recorder_points == 0,
        )
        if source == SOURCE_ENERGA_API:
            api_hourly = await _collect_meter_hourly_api(api, meter, start_dt, end_dt)
            if sum(len(zone) for zone in api_hourly.values()) > 0:
                hourly = api_hourly
            else:
                source = SOURCE_RECORDER

        total_points = sum(len(zone) for zone in hourly.values())
        if total_points == 0:
            results.append(
                {
                    **_empty_period_result(meter, old_system),
                    "source": source,
                    "cached": False,
                }
            )
            continue

        # Faza 3 opening balances: net-metering warehouse (kWh) from the
        # recorder's prior monthly flows; net-billing deposit only when the
        # caller provides it (per-month historical RCEm is Faza 4).
        bank_open_1: float | None = None
        bank_open_2: float | None = None
        deposit_open: float | None = None
        if old_system:
            if override_bank_1 is not None or override_bank_2 is not None:
                bank_open_1 = override_bank_1 if override_bank_1 is not None else 0.0
                bank_open_2 = override_bank_2 if override_bank_2 is not None else 0.0
            else:
                monthly = await _collect_monthly_flows(hass, meter, start_dt)
                bank_open_1, bank_open_2, _bank_detail = (
                    opening_bank_from_monthly_flows(
                        monthly,
                        coefficient,
                        has_zones=has_zones,
                        period_start=start_dt,
                    )
                )
        else:
            deposit_open = override_deposit

        fees = fees_from_options(dict(entry.options or {}), meter.get("tariff"))
        invoice = build_period_invoice(
            hourly,
            fees=fees,
            rcem=rcem,
            months=months,
            old_system=old_system,
            deposit_open_pln=deposit_open,
            cover_day=0.0,
            cover_night=0.0,
            bank_open_1=bank_open_1,
            bank_open_2=bank_open_2,
            prosumer_coefficient=coefficient,
            tariff=meter.get("tariff"),
        )
        result = {
            **invoice,
            "empty": False,
            "meter_point_id": meter_point_id,
            "meter_serial": serial,
            "tariff": meter.get("tariff"),
            "has_zones": has_zones,
            "prosumer_coefficient": coefficient,
            "period_start": base["period_start"],
            "period_end": base["period_end"],
            "source": source,
            "cached": False,
        }
        _verify_cache_put(coordinator, cache_key, result)
        results.append(result)

    sources = {result.get("source", SOURCE_RECORDER) for result in results}
    if len(sources) == 1:
        source = sources.pop()
    elif sources:
        source = "mixed"
    else:
        source = SOURCE_RECORDER

    response = {
        **base,
        "source": source,
        "empty": all(result.get("empty", False) for result in results),
        "cached": bool(results) and all(
            result.get("cached", False) for result in results
        ),
        "meters": results,
    }
    if len(results) == 1:
        # Single-meter entries get the full breakdown flat as well.
        response.update(results[0])
    return response


async def _stat_sum_before(
    hass: HomeAssistant, statistic_id: str, when
) -> float:
    """Sum imported strictly before `when` (v0.3.4 flow anchor)."""
    try:
        start_dt = when - timedelta(days=30)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=TIMEZONE)
        end_dt = when
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=TIMEZONE)
    except Exception:
        return 0.0

    try:
        stats = await get_instance(hass).async_add_executor_job(
            functools.partial(
                statistics_during_period,
                hass,
                start_dt,
                end_dt,
                [statistic_id],
                "day",
                None,
                {"sum"},
            )
        )
        rows = (stats or {}).get(statistic_id) or []
        sums = [r.get("sum") for r in rows if r.get("sum") is not None]
        if sums:
            return max(0.0, max(float(s) for s in sums))
    except Exception as err:
        _LOGGER.debug("Flow anchor lookup failed for %s: %s", statistic_id, err)
    return 0.0


async def _has_history_statistics(
    hass: HomeAssistant, meters: list, target_start: datetime
) -> bool:
    """True when statistics already extend back to near target_start."""
    try:
        import functools

        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
        from homeassistant.helpers import entity_registry as er
    except Exception:
        return True  # recorder unavailable → don't duplicate imports
    try:
        registry = er.async_get(hass)
        wanted: list = []
        for meter in meters:
            mid = meter.get("meter_point_id")
            zones = meter.get("zone_count", 1) > 1
            suffix = "import_1" if zones else "import"
            uid = f"energa_{mid}_{suffix}_stats"
            for entity in list(registry.entities.values()):
                if entity.unique_id == uid:
                    wanted.append(entity.entity_id)
                    break
        if not wanted:
            return False

        check_start = target_start
        if check_start.tzinfo is None:
            check_start = check_start.replace(tzinfo=TIMEZONE)
        check_end = check_start + timedelta(days=60)
        stats = await get_instance(hass).async_add_executor_job(
            functools.partial(
                statistics_during_period,
                hass,
                check_start,
                check_end,
                wanted[:1],
                "day",
                None,
                {"sum"},
            )
        )
        rows = (stats or {}).get(wanted[0]) or []
        return len(rows) > 0
    except Exception as err:
        _LOGGER.debug("History statistics check failed: %s", err)
        return False


async def _has_any_panel_statistics(hass: HomeAssistant, meters: list) -> bool:
    """Deprecated alias: True when statistics already extend back to near target_start."""
    start_date = datetime.now(TIMEZONE) - timedelta(days=AUTO_HISTORY_DAYS)
    return await _has_history_statistics(hass, meters, start_date)


def _polish_plural(count: int, one: str, few: str, many: str) -> str:
    """Return the Polish plural form for ``count``.

    Polish has three plural categories: singular (1), few (2-4, but not the
    12-14 teens) and many (everything else, including 0 and 5+). ``one``,
    ``few`` and ``many`` are the already-inflected word forms.
    """
    try:
        number = abs(int(count))
    except (TypeError, ValueError):
        number = 0
    if number == 1:
        return one
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return few
    return many


def _meter_count_phrase(count: int) -> str:
    """E.g. ``1 licznik`` / ``2 liczniki`` / ``5 liczników``."""
    return f"{count} {_polish_plural(count, 'licznik', 'liczniki', 'liczników')}"


def _direction_word(count: int, prosumer: bool) -> str:
    """Inflected direction adjective agreeing with the plural category."""
    if prosumer:
        return _polish_plural(
            count, "dwukierunkowy", "dwukierunkowe", "dwukierunkowych"
        )
    return _polish_plural(
        count, "jednokierunkowy", "jednokierunkowe", "jednokierunkowych"
    )


def describe_active_meters(meters: list) -> str:
    """Human-readable Polish summary of the meters taking part in a backfill.

    Distinguishes one-way consumers from two-way prosumers (export) so the
    notification tells the user what kind of meters are being imported.
    """
    from .settlement import is_export_prosumer

    total = len(meters or [])
    if total <= 0:
        return "0 liczników"
    prosumers = sum(1 for meter in meters if is_export_prosumer(meter))
    consumers = total - prosumers
    if prosumers == total:
        return (
            f"{_meter_count_phrase(total)} {_direction_word(total, True)} (prosument)"
        )
    if prosumers == 0:
        return f"{_meter_count_phrase(total)} {_direction_word(total, False)}"
    return (
        f"{_meter_count_phrase(total)}: "
        f"{consumers} {_direction_word(consumers, False)}, "
        f"{prosumers} {_direction_word(prosumers, True)} (prosument)"
    )


def _settlement_dashboard_clause(entry: ConfigEntry) -> str:
    """Past-tense clause matching the user's settlement-dashboard choice."""
    if entry.options.get(
        CONF_CREATE_SETTLEMENT_DASHBOARD, DEFAULT_CREATE_SETTLEMENT_DASHBOARD
    ):
        return (
            "Panel «Energa — Rozliczenia» utworzono; zostanie zaktualizowany "
            "po zakończeniu importu."
        )
    return (
        "Panel «Energa — Rozliczenia» nie został utworzony — zgodnie z Twoim wyborem."
    )


def _backfill_overview_message(active: list, entry: ConfigEntry, days: int) -> str:
    """Start message for the whole backfill (grammar + truthful dashboards)."""
    years = max(1, round(days / 365))
    return (
        f"Pobieranie historii zużycia z ostatnich {years} lat wystartowało w tle "
        f"— {describe_active_meters(active)} — do bazy statystyk długoterminowych. "
        "Statystyki będą dostępne do wyboru w konfiguracji energii oraz na pulpicie "
        "Energa. "
        f"{_settlement_dashboard_clause(entry)} "
        "Wbudowany Panel Energia nie jest zmieniany — źródła dobierasz sam."
    )


def _schedule_notification_dismiss(
    hass: HomeAssistant,
    notification_id: str,
    delay: float = BACKFILL_DISMISS_DELAY_S,
) -> None:
    """Dismiss a persistent notification after ``delay`` seconds. Never raises."""

    def _dismiss(_now=None) -> None:
        try:
            persistent_notification.async_dismiss(hass, notification_id)
        except Exception as err:  # noqa: BLE001 - cleanup must never break the import
            _LOGGER.debug("Energa: notification dismiss skipped: %s", err)

    try:
        async_call_later(hass, delay, _dismiss)
    except Exception as err:  # noqa: BLE001 - cleanup must never break the import
        _LOGGER.debug("Energa: could not schedule notification dismiss: %s", err)


class _BackfillProgressNotifier:
    """Single throttled persistent notification showing one meter's progress.

    Idempotent by construction: the notification id is derived from the meter,
    so repeated imports overwrite the same entry instead of leaking new ones.
    After :meth:`finish` the notification is dismissed after a short delay.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        notification_id: str,
        serial: str,
        days: int,
        start_date: datetime,
        *,
        now_fn=monotonic,
        interval: float = BACKFILL_PROGRESS_INTERVAL_S,
    ) -> None:
        self._hass = hass
        self._notification_id = notification_id
        self._serial = serial
        self._days = max(1, int(days or 1))
        self._start_date = start_date
        self._now = now_fn
        self._interval = interval
        self._last_update = 0.0
        self.finished = False

    def start(self) -> None:
        """Post the initial 0/N progress notification."""
        self._last_update = self._now()
        self._post(0, self._start_date)

    def update(self, done: int, current_day=None) -> None:
        """Post progress if the throttle window elapsed (or the import ended)."""
        if self.finished:
            return
        if done < self._days and (self._now() - self._last_update) < self._interval:
            return
        self._last_update = self._now()
        self._post(done, current_day)

    def _post(self, done: int, current_day) -> None:
        done = max(0, min(int(done), self._days))
        remaining = max(0, self._days - done)
        pct = int((done / self._days) * 100)
        # Progress is visible in the log as well as in the notification, so a
        # user (or support) can follow a long import from the HA log.
        _LOGGER.info(
            "Energa: backfill %s — %d/%d dni (%d%%)",
            self._serial,
            done,
            self._days,
            pct,
        )
        lines = [
            f"Pobieranie historii dla licznika {self._serial} w toku...",
            "",
            f"- Postęp: **{done} / {self._days} dni ({pct}%)**",
            f"- Pozostało: **{remaining} dni**",
        ]
        if current_day is not None:
            lines.append(f"- Przetwarzany dzień: {current_day}")
        eta_min = round(remaining * 0.8 / 60)
        if eta_min > 0:
            lines.append(f"- Szacowany pozostały czas: ~{eta_min} min")
        persistent_notification.async_create(
            self._hass,
            "\n".join(lines),
            title=BACKFILL_NOTIFICATION_TITLE,
            notification_id=self._notification_id,
        )

    def finish(self, summary: str) -> None:
        """Replace progress with the final summary and schedule dismissal."""
        if self.finished:
            return
        self.finished = True
        _LOGGER.info("Energa: backfill %s finished", self._serial)
        persistent_notification.async_create(
            self._hass,
            summary,
            title=BACKFILL_NOTIFICATION_TITLE,
            notification_id=self._notification_id,
        )
        _schedule_notification_dismiss(self._hass, self._notification_id)

    def dismiss_now(self) -> None:
        """Drop the notification immediately (used when the task is cancelled)."""
        if self.finished:
            return
        self.finished = True
        try:
            persistent_notification.async_dismiss(self._hass, self._notification_id)
        except Exception as err:  # noqa: BLE001 - cleanup must never raise
            _LOGGER.debug("Energa: progress dismiss skipped: %s", err)


async def _maybe_auto_backfill(
    hass: HomeAssistant, api: EnergaAPI, entry: ConfigEntry
) -> None:
    """Schedule the blind 730-day history import for fresh entries."""
    import sys

    energa_mod = sys.modules.get("custom_components.energa_mobile")
    _has_stats_fn = getattr(energa_mod, "_has_history_statistics", _has_history_statistics)
    _import_fn = getattr(energa_mod, "_import_meter_history", _import_meter_history)

    overview_posted = False
    overview_closed = False

    try:
        try:
            meters = await api.async_get_data(force_refresh=False)
        except Exception as err:
            _LOGGER.debug("Auto-backfill: meter fetch failed: %s", err)
            return

        active = [
            m
            for m in (meters or [])
            if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
        ]
        if not active:
            return

        # Dashboard provisioning is handled by
        # ``_async_ensure_settlement_dashboard`` during ``async_setup_entry``
        # (honours the ``create_settlement_dashboard`` option).
        #
        # TODO(v1.9.x): opcjonalna auto-konfiguracja wbudowanego Panelu Energia
        # po zakończeniu backfillu. Świadomie NIE robimy tego domyślnie:
        # przycisk „Skonfiguruj Panel Energia" podmienia globalne źródła
        # grid/battery w `energy_sources`, co na współdzielonej instancji może
        # nadpisać źródła innych integracji. Bezpieczna wersja wymaga osobnej
        # opcji opt-in (+ opis, że dotyczy całego Panelu Energia).

        # 1b. Synthetic storage statistics
        enable_synth = entry.options.get(
            CONF_ENABLE_SYNTHETIC_STORAGE, DEFAULT_ENABLE_SYNTHETIC_STORAGE
        )
        if enable_synth:
            try:
                from .synthetic_storage import (
                    async_synthesize_storage_from_recorder,
                )

                for meter in active:
                    await async_synthesize_storage_from_recorder(
                        hass, entry, meter
                    )
            except Exception as synth_err:
                _LOGGER.debug(
                    "Auto-synthesize storage from recorder skipped: %s",
                    synth_err,
                )

        # 2. Check completed flag
        if (entry.data or {}).get("auto_backfill_completed"):
            _LOGGER.debug(
                "Auto-backfill: already completed according to entry data, skipping"
            )
            return

        try:
            start_str = (entry.data or {}).get("auto_history_start")
            start_date = datetime.strptime(start_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            start_date = datetime.now(TIMEZONE) - timedelta(days=AUTO_HISTORY_DAYS)

        # 3. Check existing statistics
        if await _has_stats_fn(hass, active, start_date):
            _LOGGER.debug(
                "Auto-backfill: historical statistics already present near start_date, skipping"
            )
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "auto_backfill_completed": True}
            )
            return

        days = (datetime.now(TIMEZONE).date() - start_date.date()).days + 1
        days = max(1, min(days, AUTO_HISTORY_DAYS + 1))
        persistent_notification.async_create(
            hass,
            _backfill_overview_message(active, entry, days),
            title=BACKFILL_OVERVIEW_TITLE,
            notification_id=BACKFILL_OVERVIEW_NOTIFICATION_ID,
        )
        overview_posted = True
        _LOGGER.info(
            "Auto-backfill: importing %d days from %s for %d meter(s)",
            days,
            start_date.date(),
            len(active),
        )
        for meter in active:
            await _import_fn(
                hass, api, meter, start_date, days, entry
            )

        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "auto_backfill_completed": True}
        )
        persistent_notification.async_create(
            hass,
            f"Pobieranie historii zakończone — {describe_active_meters(active)}. "
            "Statystyki są już dostępne w bazie długoterminowej.",
            title=BACKFILL_OVERVIEW_TITLE,
            notification_id=BACKFILL_OVERVIEW_NOTIFICATION_ID,
        )
        _schedule_notification_dismiss(hass, BACKFILL_OVERVIEW_NOTIFICATION_ID)
        overview_closed = True
        _LOGGER.info("Auto-backfill: successfully completed for all active meters")
    except Exception as err:
        _LOGGER.debug("Auto-backfill skipped: %s", err)
        persistent_notification.async_create(
            hass,
            f"Pobieranie historii nie powiodło się: {err}",
            title=BACKFILL_OVERVIEW_TITLE,
            notification_id=BACKFILL_OVERVIEW_NOTIFICATION_ID,
        )
        _schedule_notification_dismiss(hass, BACKFILL_OVERVIEW_NOTIFICATION_ID)
        overview_closed = True
    finally:
        if overview_posted and not overview_closed:
            try:
                persistent_notification.async_dismiss(
                    hass, BACKFILL_OVERVIEW_NOTIFICATION_ID
                )
            except Exception as err:  # noqa: BLE001 - cleanup must never raise
                _LOGGER.debug("Energa: overview dismiss skipped: %s", err)


async def _import_meter_history(
    hass: HomeAssistant,
    api: EnergaAPI,
    meter: dict,
    start_date: datetime,
    days: int,
    entry: ConfigEntry,
) -> None:
    """Import historical data for a single meter.

    Supports multi-zone tariffs (G12w): imports zone-specific statistics.
    """
    meter_point_id = meter["meter_point_id"]
    meter_id = meter.get("meter_serial", meter_point_id)
    serial = meter_id
    has_zones = meter.get("zone_count", 1) > 1

    _LOGGER.info(
        "Starting history import for meter %s (%d days from %s, zones=%s)",
        serial,
        days,
        start_date.date(),
        has_zones,
    )

    notifier = _BackfillProgressNotifier(
        hass,
        f"energa_import_{meter_id}",
        serial,
        days,
        start_date,
    )

    try:
        notifier.start()
        import_points = []
        import_1_points = []
        import_2_points = []
        export_points = []
        export_1_points = []
        export_2_points = []

        for day_offset in range(days):
            target_day = (start_date + timedelta(days=day_offset)).replace(
                tzinfo=TIMEZONE
            )
            if target_day.date() > datetime.now(TIMEZONE).date():
                break

            if day_offset > 0:
                await asyncio.sleep(0.3)

            day_data = await api.async_get_history_hourly(
                meter_point_id, target_day, include_timestamps=True
            )

            notifier.update(day_offset + 1, target_day.date())

            if day_data:
                for item in day_data.get("import", []):
                    if isinstance(item, (list, tuple)):
                        hourly_value, tm_ms = item
                    else:
                        continue
                    if hourly_value is not None and hourly_value >= 0:
                        hour_dt = dt_util.as_utc(
                            datetime.fromtimestamp(tm_ms / 1000, tz=TIMEZONE)
                        )
                        import_points.append({"dt": hour_dt, "value": hourly_value})

                if has_zones:
                    for item in day_data.get("import_1", []):
                        if isinstance(item, (list, tuple)):
                            hourly_value, tm_ms = item
                        else:
                            continue
                        if hourly_value is not None and hourly_value >= 0:
                            hour_dt = dt_util.as_utc(
                                datetime.fromtimestamp(tm_ms / 1000, tz=TIMEZONE)
                            )
                            import_1_points.append({"dt": hour_dt, "value": hourly_value})

                    for item in day_data.get("import_2", []):
                        if isinstance(item, (list, tuple)):
                            hourly_value, tm_ms = item
                        else:
                            continue
                        if hourly_value is not None and hourly_value >= 0:
                            hour_dt = dt_util.as_utc(
                                datetime.fromtimestamp(tm_ms / 1000, tz=TIMEZONE)
                            )
                            import_2_points.append({"dt": hour_dt, "value": hourly_value})

                for item in day_data.get("export", []):
                    if isinstance(item, (list, tuple)):
                        hourly_value, tm_ms = item
                    else:
                        continue
                    if hourly_value is not None and hourly_value >= 0:
                        hour_dt = dt_util.as_utc(
                            datetime.fromtimestamp(tm_ms / 1000, tz=TIMEZONE)
                        )
                        export_points.append({"dt": hour_dt, "value": hourly_value})

                if has_zones:
                    for item in day_data.get("export_1", []):
                        if isinstance(item, (list, tuple)):
                            hourly_value, tm_ms = item
                        else:
                            continue
                        if hourly_value is not None and hourly_value >= 0:
                            hour_dt = dt_util.as_utc(
                                datetime.fromtimestamp(tm_ms / 1000, tz=TIMEZONE)
                            )
                            export_1_points.append({"dt": hour_dt, "value": hourly_value})

                    for item in day_data.get("export_2", []):
                        if isinstance(item, (list, tuple)):
                            hourly_value, tm_ms = item
                        else:
                            continue
                        if hourly_value is not None and hourly_value >= 0:
                            hour_dt = dt_util.as_utc(
                                datetime.fromtimestamp(tm_ms / 1000, tz=TIMEZONE)
                            )
                            export_2_points.append({"dt": hour_dt, "value": hourly_value})

        _LOGGER.info(
            "Collected data for meter %s: %d import, %d export%s",
            serial,
            len(import_points),
            len(export_points),
            (
                f", imp_z1={len(import_1_points)}, imp_z2={len(import_2_points)}"
                f", exp_z1={len(export_1_points)}, exp_z2={len(export_2_points)}"
            )
            if has_zones
            else "",
        )

        recorder_adapter = (
            hass.data.get(DOMAIN, {})
            .get(entry.entry_id, {})
            .get("recorder_adapter")
        )

        suffix_to_name = {
            "import": "panel_energia_zuzycie",
            "import_1": "panel_energia_strefa_1",
            "import_2": "panel_energia_strefa_2",
            "export": "panel_energia_produkcja",
            "export_1": "panel_energia_produkcja_strefa_1",
            "export_2": "panel_energia_produkcja_strefa_2",
        }

        def build_statistics(
            points: list,
            entity_suffix: str,
            entry: ConfigEntry,
            base: float = 0.0,
            cost_base: float = 0.0,
        ) -> int:
            if not points:
                return 0

            price = get_price_for_key(
                dict(entry.options), entity_suffix, meter_id=str(serial)
            )
            points.sort(key=lambda x: x["dt"])

            merged: list[dict] = []
            for point in points:
                if merged and merged[-1]["dt"] == point["dt"]:
                    merged[-1]["value"] += point["value"]
                    _LOGGER.debug(
                        "DST dedup: merged %.3f kWh into %s (total %.3f)",
                        point["value"],
                        point["dt"],
                        merged[-1]["value"],
                    )
                else:
                    merged.append(dict(point))
            points = merged

            try:
                running_sum = max(0.0, float(base))
            except (ValueError, TypeError):
                running_sum = 0.0
            statistics = []

            for point in points:
                hourly_value = point["value"]
                if hourly_value < 0 or hourly_value > MAX_HOURLY_KWH:
                    _LOGGER.warning(
                        "Import spike guard: skipping %.1f kWh for %s at %s",
                        hourly_value,
                        serial,
                        point["dt"],
                    )
                    continue

                running_sum += hourly_value
                statistics.append(
                    {
                        "start": point["dt"],
                        "sum": running_sum,
                        "state": hourly_value,
                    }
                )

            cost_statistics = []
            try:
                cost_running = max(0.0, float(cost_base))
            except (ValueError, TypeError):
                cost_running = 0.0
            if not entity_suffix.startswith("export"):
                for stat in statistics:
                    hourly_energy = stat["state"] or 0
                    hourly_cost = hourly_energy * price
                    cost_running += hourly_cost
                    cost_statistics.append(
                        {
                            "start": stat["start"],
                            "sum": cost_running,
                            "state": hourly_cost,
                        }
                    )

            energy_sensor_name = suffix_to_name.get(
                entity_suffix, f"panel_{entity_suffix}"
            )
            entity_id = f"sensor.energa_{meter_id}_{energy_sensor_name}".lower()

            recorder_adapter = (
                hass.data.get(DOMAIN, {})
                .get(entry.entry_id, {})
                .get("recorder_adapter")
            )
            if recorder_adapter:
                recorder_adapter.import_energy_statistics(
                    statistic_id=entity_id,
                    statistics=statistics,
                    name=None,
                    unit="kWh",
                )
            else:
                metadata = StatisticMetaData(
                    source="recorder",
                    statistic_id=entity_id,
                    name=None,
                    unit_of_measurement="kWh",
                    has_mean=False,
                    has_sum=True,
                    mean_type=StatisticMeanType.NONE,
                    unit_class="energy",
                )
                async_import_statistics(hass, metadata, statistics)

            # Canonically archive chunk to SQLite
            storage = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("storage")
            if storage and points:
                try:
                    from .core.readings.models import IntervalReading

                    is_export = entity_suffix.startswith("export")
                    c_readings = [
                        IntervalReading(
                            ppe_id=meter_point_id,
                            meter_id=str(meter_id),
                            register=entity_suffix,
                            interval_start_utc=dt_util.as_utc(p["dt"]),
                            resolution="1h",
                            import_kwh=Decimal("0.0")
                            if is_export
                            else Decimal(str(round(float(p["value"]), 4))),
                            export_kwh=Decimal(str(round(float(p["value"]), 4)))
                            if is_export
                            else Decimal("0.0"),
                            quality="ok",
                            source="energa",
                        )
                        for p in points
                        if p.get("value") is not None and p["value"] >= 0
                    ]
                    if c_readings:
                        storage.insert_readings_idempotent(c_readings)
                except Exception as c_err:
                    _LOGGER.debug("Canonical archive for chunk failed: %s", c_err)

            cost_entity_id = f"{entity_id}_cost"
            if cost_statistics:
                cost_name_map = {
                    "import": "Panel Energia Zużycie Koszt",
                    "import_1": "Panel Energia Strefa 1 Koszt",
                    "import_2": "Panel Energia Strefa 2 Koszt",
                    "export": "Panel Energia Produkcja Rekompensata",
                    "export_1": "Panel Energia Produkcja Strefa 1 Rekompensata",
                    "export_2": "Panel Energia Produkcja Strefa 2 Rekompensata",
                }
                cost_name = cost_name_map.get(entity_suffix, f"Koszt {entity_suffix}")

                if recorder_adapter:
                    recorder_adapter.import_cost_statistics(
                        statistic_id=cost_entity_id,
                        statistics=cost_statistics,
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
                    async_import_statistics(hass, cost_metadata, cost_statistics)
                _LOGGER.info(
                    "Imported %d cost statistics for %s (price: %.4f PLN/kWh)",
                    len(cost_statistics),
                    cost_entity_id,
                    price,
                )
            elif entity_suffix.startswith("export"):
                _LOGGER.debug(
                    "Skipping cost statistics for %s (export priced live via RCEm)",
                    entity_id,
                )

            return len(statistics)

        async def _build_anchored(points: list, entity_suffix: str) -> int:
            base = 0.0
            cost_base = 0.0
            if points:
                try:
                    first_dt = min(
                        p["dt"] for p in points if p.get("dt") is not None
                    )
                except (ValueError, TypeError):
                    first_dt = None
                if first_dt is not None:
                    energy_name = suffix_to_name.get(
                        entity_suffix, f"panel_{entity_suffix}"
                    )
                    candidates = [f"sensor.energa_{meter_id}_{energy_name}"]
                    if serial and str(serial) != str(meter_id):
                        candidates.append(f"sensor.energa_{serial}_{energy_name}")
                    for eid in candidates:
                        b = await _stat_sum_before(hass, eid, first_dt)
                        if b > 0:
                            base = b
                            if not entity_suffix.startswith("export"):
                                cost_base = await _stat_sum_before(
                                    hass, f"{eid}_cost", first_dt
                                )
                            break
                    if base == 0.0 and not entity_suffix.startswith("export"):
                        for eid in candidates:
                            cb = await _stat_sum_before(
                                hass, f"{eid}_cost", first_dt
                            )
                            if cb > 0:
                                cost_base = cb
                                break
            return build_statistics(points, entity_suffix, entry, base, cost_base)

        if has_zones:
            count_1 = await _build_anchored(import_1_points, "import_1")
            count_2 = await _build_anchored(import_2_points, "import_2")
            count_exp1 = await _build_anchored(export_1_points, "export_1")
            count_exp2 = await _build_anchored(export_2_points, "export_2")
            total_count = count_1 + count_2 + count_exp1 + count_exp2

            from .settlement import is_export_prosumer

            panel_hint = (
                "\n\n⚙️ **Konfiguracja Panelu Energia w Home Assistant:**\n"
                f"- Zużycie z sieci (dzień): `sensor.energa_{meter_id}_panel_energia_strefa_1`\n"
                f"- Zużycie z sieci (noc): `sensor.energa_{meter_id}_panel_energia_strefa_2`\n"
                f"- Oddanie do sieci: `sensor.energa_{meter_id}_panel_energia_produkcja_strefa_1` i `...strefa_2`"
            )
            is_prosumer_meter = (
                is_export_prosumer(meter)
                or bool(export_1_points or export_2_points or export_points)
                or bool(meter.get("obis_minus"))
            )
            if is_prosumer_meter:
                panel_hint += (
                    f"\n\n🔋 **Wirtualny Magazyn Energii (Net-metering):**\n"
                    f"Wygenerowano bilansowanie i syntetyczny magazyn energii.\n"
                    f"Możesz skonfigurować Panel Energia jednym kliknięciem za pomocą przycisku:\n"
                    f"`button.energa_{serial}_skonfiguruj_panel_energia` na karcie urządzenia licznika."
                )

            notifier.finish(
                f"Zakończono import historii dla licznika {serial}.\n"
                f"Zaimportowano {total_count} punktów danych (Import S1: {count_1}, S2: {count_2}, "
                f"Export S1: {count_exp1}, S2: {count_exp2}).\n\n"
                f"📊 Pulpit dostępny pod adresem: [/{DEFAULT_URL_PATH}](/{DEFAULT_URL_PATH})"
                + panel_hint
            )
        else:
            count_import = await _build_anchored(import_points, "import")
            count_export = await _build_anchored(export_points, "export")
            total_count = count_import + count_export

            from .settlement import is_export_prosumer

            panel_hint = (
                "\n\n⚙️ **Konfiguracja Panelu Energia w Home Assistant:**\n"
                f"- Zużycie z sieci: `sensor.energa_{meter_id}_panel_energia_zuzycie`\n"
                f"- Oddanie do sieci: `sensor.energa_{meter_id}_panel_energia_produkcja`"
            )
            is_prosumer_meter = (
                is_export_prosumer(meter)
                or bool(export_points)
                or bool(meter.get("obis_minus"))
            )
            if is_prosumer_meter:
                panel_hint += (
                    f"\n\n🔋 **Wirtualny Magazyn Energii (Net-metering):**\n"
                    f"Wygenerowano bilansowanie i syntetyczny magazyn energii.\n"
                    f"Możesz skonfigurować Panel Energia jednym kliknięciem za pomocą przycisku:\n"
                    f"`button.energa_{serial}_skonfiguruj_panel_energia` na karcie urządzenia licznika."
                )

            notifier.finish(
                f"Zakończono import historii dla licznika {serial}.\n"
                f"Zaimportowano {total_count} punktów danych (Import: {count_import}, Export: {count_export}).\n\n"
                f"📊 Pulpit dostępny pod adresem: [/{DEFAULT_URL_PATH}](/{DEFAULT_URL_PATH})"
                + panel_hint
            )

        _LOGGER.info("History import complete for %s: %d points", serial, total_count)

        # Flow series backfill
        try:
            from .settlement import (
                anchor_flow_series,
                bucket_flows,
                flow_history_series,
            )

            if has_zones:
                _pairs = [
                    (import_1_points, 0),
                    (import_2_points, 0),
                    (export_1_points, 1),
                    (export_2_points, 1),
                ]
            else:
                _pairs = [(import_points, 0), (export_points, 1)]
            _ordered = [
                (dt, list(vals))
                for dt, vals in bucket_flows(_pairs, max_hourly=MAX_HOURLY_KWH)
            ]
            try:
                _coeff = float(
                    entry.options.get(
                        CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT
                    )
                )
            except (ValueError, TypeError):
                _coeff = DEFAULT_PROSUMER_COEFFICIENT
            _ch, _dis = flow_history_series(
                [(v[0], v[1]) for _, v in _ordered], _coeff, _coeff >= 0.7
            )

            _reg = er.async_get(hass)

            for _direction, _series in (("charge", _ch), ("discharge", _dis)):
                _uid = f"energa_{meter_point_id}_bank_{_direction}"
                _eid = _reg.async_get_entity_id("sensor", DOMAIN, _uid)
                if not _eid:
                    _LOGGER.debug("Flow backfill: entity missing for %s", _uid)
                    continue

                last_stat = await get_instance(hass).async_add_executor_job(
                    get_last_statistics, hass, 1, _eid, True, {"sum", "start"}
                )
                last_stat_row = (
                    last_stat.get(_eid, [{}])[0]
                    if last_stat and last_stat.get(_eid)
                    else None
                )
                last_start = last_stat_row.get("start") if last_stat_row else None
                last_sum = last_stat_row.get("sum") if last_stat_row else None

                _dts = [d for d, _ in _ordered]
                if last_start is not None and last_sum is not None:
                    last_dt = (
                        dt_util.utc_from_timestamp(last_start)
                        if isinstance(last_start, (int, float))
                        else last_start
                    )
                    filtered_indices = [
                        idx for idx, d in enumerate(_dts) if d > last_dt
                    ]
                    if not filtered_indices:
                        _LOGGER.debug(
                            "Flow backfill: %s up to date at %s (sum=%.3f), skipping",
                            _eid,
                            last_dt,
                            last_sum,
                        )
                        continue
                    _dts = [_dts[idx] for idx in filtered_indices]
                    _sub_ordered = [_ordered[idx] for idx in filtered_indices]
                    _sub_ch, _sub_dis = flow_history_series(
                        [(v[0], v[1]) for _, v in _sub_ordered],
                        _coeff,
                        _coeff >= 0.7,
                    )
                    _series_to_anchor = (
                        _sub_ch if _direction == "charge" else _sub_dis
                    )
                    _anchored = anchor_flow_series(_series_to_anchor, float(last_sum))
                else:
                    _base = await _stat_sum_before(hass, _eid, start_date)
                    _anchored = anchor_flow_series(_series, _base)

                if len(_anchored) != len(_dts):
                    _LOGGER.debug("Flow backfill: length mismatch for %s", _eid)
                    continue
                _stats = [
                    {"start": _dt, "sum": _cum, "state": _st}
                    for (_dt, (_cum, _st)) in zip(_dts, _anchored)
                ]
                if not _stats:
                    continue
                if recorder_adapter:
                    recorder_adapter.import_energy_statistics(
                        statistic_id=_eid,
                        statistics=_stats,
                        name=None,
                        unit="kWh",
                    )
                else:
                    _meta = StatisticMetaData(
                        source="recorder",
                        statistic_id=_eid,
                        name=None,
                        unit_of_measurement="kWh",
                        has_mean=False,
                        has_sum=True,
                        mean_type=StatisticMeanType.NONE,
                        unit_class="energy",
                    )
                    async_import_statistics(hass, _meta, _stats)
                _LOGGER.info("Backfilled %d flow statistics for %s", len(_stats), _eid)
        except Exception as err:
            _LOGGER.debug("Flow history backfill skipped for %s: %s", serial, err)

        # Synthetic virtual storage backfill
        try:
            from .settlement import is_export_prosumer
            from .synthetic_storage import calculate_synthetic_storage

            enable_synth = entry.options.get(
                CONF_ENABLE_SYNTHETIC_STORAGE, DEFAULT_ENABLE_SYNTHETIC_STORAGE
            )
            has_any_export = bool(
                export_points
                or (has_zones and (export_1_points or export_2_points))
            ) or bool(meter.get("obis_minus"))
            if enable_synth and (is_export_prosumer(meter) or has_any_export):
                try:
                    _coeff = float(
                        entry.options.get(
                            CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT
                        )
                    )
                except (ValueError, TypeError):
                    _coeff = DEFAULT_PROSUMER_COEFFICIENT

                by_hour: dict = {}
                if has_zones:
                    for p in import_1_points:
                        by_hour.setdefault(p["dt"], {})["import_1"] = p["value"]
                    for p in import_2_points:
                        by_hour.setdefault(p["dt"], {})["import_2"] = p["value"]
                    for p in export_1_points:
                        by_hour.setdefault(p["dt"], {})["export_1"] = p["value"]
                    for p in export_2_points:
                        by_hour.setdefault(p["dt"], {})["export_2"] = p["value"]

                    synth_records = [
                        {
                            "dt": dt,
                            "import_1": vals.get("import_1", 0.0),
                            "import_2": vals.get("import_2", 0.0),
                            "export_1": vals.get("export_1", 0.0),
                            "export_2": vals.get("export_2", 0.0),
                        }
                        for dt, vals in sorted(by_hour.items())
                    ]
                else:
                    for p in import_points:
                        by_hour.setdefault(p["dt"], {})["import"] = p["value"]
                    for p in export_points:
                        by_hour.setdefault(p["dt"], {})["export"] = p["value"]

                    synth_records = [
                        {
                            "dt": dt,
                            "import": vals.get("import", 0.0),
                            "export": vals.get("export", 0.0),
                        }
                        for dt, vals in sorted(by_hour.items())
                    ]

                base_sums = {}
                first_dt = synth_records[0]["dt"] if synth_records else None
                if first_dt:
                    keys_to_check = (
                        [
                            "magazyn_l1_ladowanie",
                            "magazyn_l1_rozladowanie",
                            "magazyn_l2_ladowanie",
                            "magazyn_l2_rozladowanie",
                            "siec_oddanie_strefa_1",
                            "siec_oddanie_strefa_2",
                            "siec_pobor_strefa_1",
                            "siec_pobor_strefa_2",
                        ]
                        if has_zones
                        else [
                            "magazyn_ladowanie",
                            "magazyn_rozladowanie",
                            "siec_oddanie",
                            "siec_pobor",
                        ]
                    )
                    for k in keys_to_check:
                        prefix = (
                            "syntetyczny_"
                            if k.startswith("magazyn")
                            else "syntetyczna_"
                        )
                        s_eid = f"sensor.energa_{serial}_{prefix}{k}"
                        b = await _stat_sum_before(hass, s_eid, first_dt)
                        if b > 0:
                            base_sums[k] = b

                init_b1 = float(
                    entry.options.get(CONF_BANK_INITIAL_KWH_L1, 0.0) or 0.0
                )
                init_b2 = float(
                    entry.options.get(CONF_BANK_INITIAL_KWH_L2, 0.0) or 0.0
                )
                if not has_zones and init_b1 == 0.0 and init_b2 == 0.0:
                    init_b1 = float(
                        entry.options.get(CONF_BANK_INITIAL_KWH, 0.0) or 0.0
                    )

                synth_res = calculate_synthetic_storage(
                    hourly_records=synth_records,
                    coeff=_coeff,
                    has_zones=has_zones,
                    initial_bank_1=init_b1,
                    initial_bank_2=init_b2,
                    base_sums=base_sums,
                )

                for k, pts in synth_res.get("series", {}).items():
                    prefix = (
                        "syntetyczny_"
                        if k.startswith("magazyn")
                        else "syntetyczna_"
                    )
                    s_eid = f"sensor.energa_{serial}_{prefix}{k}"
                    stats = [
                        {
                            "start": p["dt"],
                            "state": p["state"],
                            "sum": p["sum"],
                        }
                        for p in pts
                    ]
                    if not stats:
                        continue
                    if recorder_adapter:
                        recorder_adapter.import_energy_statistics(
                            statistic_id=s_eid,
                            statistics=stats,
                            name=None,
                            unit="kWh",
                        )
                    else:
                        meta = StatisticMetaData(
                            source="recorder",
                            statistic_id=s_eid,
                            name=None,
                            unit_of_measurement="kWh",
                            has_mean=False,
                            has_sum=True,
                            mean_type=StatisticMeanType.NONE,
                            unit_class="energy",
                        )
                        async_import_statistics(hass, meta, stats)
                _LOGGER.info(
                    "Backfilled %d synthetic storage series for %s",
                    len(synth_res.get("series", {})),
                    serial,
                )
        except Exception as err:
            _LOGGER.warning(
                "Synthetic storage history backfill failed for %s: %s",
                serial,
                err,
                exc_info=True,
            )

    except Exception as err:
        _LOGGER.error("History import failed for %s: %s", serial, err, exc_info=True)
        notifier.finish(f"Błąd importu historii dla {serial}: {err}")
    finally:
        notifier.dismiss_now()
