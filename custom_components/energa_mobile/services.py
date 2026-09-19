"""Service registration, backfill, and history import for Energa My Meter integration."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
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

from .adapters.pse.models import MarketPriceRecord
from .api import EnergaAPI
from .const import (
    CONF_BANK_INITIAL_KWH,
    CONF_BANK_INITIAL_KWH_L1,
    CONF_BANK_INITIAL_KWH_L2,
    CONF_BANK_RCE_PRICE,
    CONF_CREATE_SETTLEMENT_DASHBOARD,
    CONF_ENABLE_SYNTHETIC_STORAGE,
    CONF_PROSUMER_COEFFICIENT,
    CONF_VERIFY_PERIOD_END,
    CONF_VERIFY_PERIOD_START,
    DEFAULT_BANK_RCE_PRICE,
    DEFAULT_CREATE_SETTLEMENT_DASHBOARD,
    DEFAULT_ENABLE_SYNTHETIC_STORAGE,
    DEFAULT_PROSUMER_COEFFICIENT,
    DOMAIN,
    MAX_HOURLY_KWH,
    get_price_for_key,
    get_prosumer_coefficient,
)
from .core.completeness import (
    STATE_COMPLETE,
    STATE_INCOMPLETE,
    STATE_UNKNOWN,
    evaluate_completeness,
    registers_for_meter,
    unknown_result,
)
from .core.identity.models import PPE, SettlementType
from .core.settlement.models import SettlementLot
from .core.verification import (
    CANONICAL_OPENING_RULE,
    OPENING_SOURCE_API,
    OPENING_SOURCE_CANONICAL,
    OPENING_SOURCE_OVERRIDE,
    OPENING_SOURCE_RECORDER,
    PERIOD_KWH_KEYS,
    SOURCE_ENERGA_API,
    SOURCE_RECORDER,
    build_period_invoice,
    canonical_opening_lot_id,
    choose_period_source,
    format_period_date,
    opening_bank_from_monthly_flows,
    opening_deposit_from_monthly_flows,
    openings_from_canonical_lots,
    period_is_historical,
)
from .dashboard_generator import (
    DEFAULT_ICON,
    DEFAULT_TITLE,
    DEFAULT_URL_PATH,
    async_provision_dashboard,
)
from .tariff import fee_source, fees_from_options

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


def _g12w_day_night_split(
    call_data: dict, consumption_kwh: Decimal
) -> tuple[Decimal, Decimal]:
    """Day/night kWh bases for a G12W reconciliation (P0.2).

    The service schema used to inject ``day_kwh``/``night_kwh`` with a
    ``default=0.0`` through voluptuous, so ``call.data`` always contained
    both keys and the ``consumption_kwh / 2`` fallback was dead — the
    invoice was recomputed from 0 kWh. The schema now leaves the keys
    absent when the caller omits them.

    Documented choice: with no zone split available the consumption is
    divided 50/50 (a neutral assumption; callers with real G12W data
    should pass ``day_kwh``/``night_kwh``). When exactly one zone is
    given the other is derived from the total so the two never
    double-count.
    """
    def _as_decimal(key: str) -> Decimal | None:
        raw = (call_data or {}).get(key)
        if raw is None:
            return None
        try:
            return max(Decimal("0"), Decimal(str(raw)))
        except (InvalidOperation, ValueError, TypeError):
            return None

    day = _as_decimal("day_kwh")
    night = _as_decimal("night_kwh")
    total = max(Decimal("0"), Decimal(str(consumption_kwh or "0")))
    if day is None and night is None:
        half = total / Decimal("2")
        return half, total - half
    if day is None:
        return max(Decimal("0"), total - night), night
    if night is None:
        return day, max(Decimal("0"), total - day)
    return day, night


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
            day_kwh, night_kwh = _g12w_day_night_split(call.data, consumption_kwh)
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
                # No default: absent keys must stay absent so the G12W
                # 50/50 fallback over consumption_kwh can run (P0.2).
                vol.Optional("day_kwh"): vol.Coerce(float),
                vol.Optional("night_kwh"): vol.Coerce(float),
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

    async def clear_period_service(call: ServiceCall) -> None:
        """Clear the saved ``verify_period_start`` / ``verify_period_end`` dates.

        The period date entities persist their values in the config entry
        Options, so a test period used once stays selected forever. This action
        removes both keys for the entry, letting the calculator start clean.
        """
        entry_id = (call.data or {}).get("entry_id")
        entries = hass.config_entries.async_entries(DOMAIN)
        entry = None
        if entry_id:
            entry = next((e for e in entries if e.entry_id == entry_id), None)
        if entry is None and entries:
            entry = entries[0]
        if entry is None:
            return
        options = dict(entry.options or {})
        changed = False
        for key in (CONF_VERIFY_PERIOD_START, CONF_VERIFY_PERIOD_END):
            if key in options:
                options.pop(key, None)
                changed = True
        if changed:
            hass.config_entries.async_update_entry(entry, options=options)
            _LOGGER.info("Energa: cleared verify_period dates for entry %s", entry.entry_id)

    hass.services.async_register(
        DOMAIN,
        "clear_period",
        clear_period_service,
        schema=vol.Schema({vol.Optional("entry_id"): str}),
    )


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister domain services when all entries are removed."""
    for service_name in (
        "fetch_history",
        "generate_dashboard",
        "reconcile_invoice",
        "verify_period",
        "clear_period",
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


def _period_month_keys(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """Calendar months touched by ``[start, end)`` (exclusive end)."""
    try:
        last_moment = end - timedelta(microseconds=1)
    except (TypeError, ValueError, OverflowError):
        last_moment = end
    first = (start.year, start.month)
    last = (last_moment.year, last_moment.month)
    keys: list[tuple[int, int]] = []
    year, month = first
    while (year, month) <= last:
        keys.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
        if len(keys) > 600:  # hard safety stop for absurd windows
            break
    return keys


def _option_rcem(entry: ConfigEntry, coordinator) -> float:
    """Fallback RCEm: coordinator cache, then Options, then last-known default."""
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


async def _resolve_period_rcem(
    api: EnergaAPI | None,
    coordinator,
    entry: ConfigEntry,
    data: dict,
    start_dt: datetime,
    end_dt: datetime,
) -> tuple[float | None, dict, str, list[str]]:
    """Resolve the RCEm used to price the verified period.

    The energy is valued with the RCEm of the month it was delivered, so the
    whole official PSE table (``api.async_fetch_official_rcem_map``, cached) is
    consulted for every month the period touches. Returns
    ``(scalar, monthly_prices, source, warnings)``:

    * explicit ``rcem_pln``/``rcem`` from the call -> ``scalar`` set, source
      ``override``;
    * PSE table -> ``monthly_prices`` filled per month, source ``pse_table``;
    * table unreachable (or month not published) -> Options/``_rce_cache``
      price as ``scalar``, source ``option`` and a warning.
    """
    for key in ("rcem_pln", "rcem"):
        val = (data or {}).get(key)
        if val is not None:
            try:
                return float(val), {}, "override", []
            except (ValueError, TypeError):
                pass

    wanted = _period_month_keys(start_dt, end_dt)
    table: dict = {}
    fetch_map = getattr(api, "async_fetch_official_rcem_map", None)
    if callable(fetch_map):
        try:
            fetched = await fetch_map()
            if isinstance(fetched, dict):
                table = fetched
        except Exception as err:  # noqa: BLE001 - PSE fetch must not break the service
            _LOGGER.debug("verify_period PSE RCEm table fetch failed: %s", err)

    cache = getattr(coordinator, "_rcem_monthly", None) if coordinator is not None else None
    if not isinstance(cache, dict):
        cache = {}

    prices: dict = {}
    for key in wanted:
        value = table.get(key)
        if value is None:
            value = cache.get(key)
        if value is None:
            continue
        try:
            value = float(value)
        except (ValueError, TypeError):
            continue
        prices[key] = value
        cache[key] = value

    if coordinator is not None:
        try:
            coordinator._rcem_monthly = cache
        except Exception as err:  # noqa: BLE001 - best-effort cache
            _LOGGER.debug("Could not cache monthly RCEm on coordinator: %s", err)

    warnings: list[str] = []
    if prices:
        missing = [f"{y:04d}-{m:02d}" for (y, m) in wanted if (y, m) not in prices]
        if missing:
            fallback = _option_rcem(entry, coordinator)
            for key in wanted:
                prices.setdefault(key, fallback)
            warnings.append(
                "Brak historycznych cen RCEm (PSE) dla miesięcy: "
                f"{', '.join(missing)} — dla nich użyto ceny z opcji/ostatniej "
                "znanej jako przybliżenia."
            )
        return None, prices, "pse_table", warnings

    fallback = _option_rcem(entry, coordinator)
    warnings.append(
        "Nie udało się pobrać tabeli RCEm PSE dla okresu — użyto ceny z opcji "
        f"bank_rce_price ({fallback:.5f} PLN/kWh)."
    )
    return fallback, {}, "option", warnings


def _monthly_export_kwh(hourly: dict) -> dict:
    """Export kWh per ``(year, month)`` from the hourly epoch series."""
    buckets: dict = {}
    for suffix in ("export", "export_1", "export_2"):
        series = (hourly or {}).get(suffix)
        if not isinstance(series, dict):
            continue
        for ts, value in series.items():
            try:
                kwh = float(value)
            except (ValueError, TypeError):
                continue
            if kwh <= 0.0:
                continue
            moment = _stat_row_moment(ts)
            if moment is None:
                continue
            key = (moment.year, moment.month)
            buckets[key] = buckets.get(key, 0.0) + kwh
    return buckets


def _effective_period_rcem(
    scalar: float | None,
    monthly_prices: dict,
    hourly: dict,
    fallback: float,
) -> float:
    """One RCEm for the invoice: an explicit scalar, or the export-weighted
    average of the period's monthly RCEm (energy valued in its delivery month).
    """
    if scalar is not None:
        return float(scalar)
    if not monthly_prices:
        return float(fallback)
    weights = _monthly_export_kwh(hourly)
    total = sum(weights.get(key, 0.0) for key in monthly_prices)
    if total <= 0.0:
        # No export in the period -> zero deposit; report the latest month.
        return float(monthly_prices[max(monthly_prices)])
    return (
        sum(
            weights.get(key, 0.0) * float(price)
            for key, price in monthly_prices.items()
        )
        / total
    )


def _rcem_cache_signature(scalar: float | None, monthly_prices: dict):
    """Hashable signature of the resolved RCEm for the in-memory memo cache."""
    if scalar is not None:
        return float(scalar)
    return tuple(
        sorted((int(y), int(m), float(v)) for (y, m), v in monthly_prices.items())
    )


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


def _has_explicit_coefficient(options: dict, meter: dict) -> bool:
    """True when the settlement coefficient was explicitly configured.

    Guards the best-effort product inference: an entry that never saved a
    coefficient must not be assumed net-metering just because the default
    (0.8) applies.
    """
    opts = options or {}
    if CONF_PROSUMER_COEFFICIENT in opts:
        return True
    serial = str(meter.get("meter_serial", meter.get("meter_point_id", "")))
    meter_id = str(meter.get("meter_point_id", ""))
    for ident in (serial, meter_id):
        if ident and f"meter_{ident}_{CONF_PROSUMER_COEFFICIENT}" in opts:
            return True
    return False


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
    api: EnergaAPI | None, meter: dict, start: datetime, end: datetime,
    on_progress=None,
) -> dict:
    """Build {zone: {epoch_hour: kWh}} from the Energa API (Faza 2).

    Best effort: any API error returns ``{}`` so the caller falls back to the
    recorder series. The shape matches :func:`_collect_meter_hourly`.

    ``on_progress(done, total)`` — optional, forwarded to the API range walk
    so a caller can display day-by-day progress/ETA.
    """
    if api is None:
        return {}
    meter_point_id = str(meter.get("meter_point_id", ""))
    if not meter_point_id:
        return {}
    try:
        result = await api.async_get_hourly_range(
            meter_point_id, start, end, on_progress=on_progress
        )
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


def period_completeness_store(coordinator) -> dict | None:
    """Return the coordinator's completeness store when the feature is active.

    ``None`` means the integration has no completeness infrastructure (the
    feature is unavailable, e.g. plain unit-test doubles) — callers must then
    not gate, since they cannot know anything better. An empty dict means the
    feature is active but has not produced a verdict yet.
    """
    if coordinator is None:
        return None
    store = getattr(coordinator, "_period_completeness", None)
    return store if isinstance(store, dict) else None


def period_completeness_get(coordinator, meter_id) -> dict | None:
    """Return the cached period-completeness result for a meter (or ``None``)."""
    store = period_completeness_store(coordinator)
    if store is None or meter_id is None:
        return None
    result = store.get(str(meter_id))
    return result if isinstance(result, dict) else None


def _completeness_matches_period(result: dict, expected_start, expected_end) -> bool:
    """True when a completeness verdict was computed for the expected bounds."""
    if not result:
        return False
    got_start = format_period_date(result.get("period_start"))
    got_end = format_period_date(result.get("period_end"))
    want_start = format_period_date(expected_start) if expected_start else None
    want_end = format_period_date(expected_end) if expected_end else None
    if want_start is None or want_end is None:
        return True
    return got_start == want_start and got_end == want_end


def period_completeness_status(
    coordinator, meter_id, *, expected_start=None, expected_end=None
) -> str:
    """Return ``complete`` / ``incomplete`` / ``unknown`` for a meter.

    When ``expected_start``/``expected_end`` are given a verdict computed for a
    different window is treated as stale (``unknown``) so the button/service
    never trusts a cache from before the user edited the period.
    """
    result = period_completeness_get(coordinator, meter_id)
    if not result:
        return STATE_UNKNOWN
    if not _completeness_matches_period(result, expected_start, expected_end):
        return STATE_UNKNOWN
    state = str(result.get("state") or STATE_UNKNOWN)
    return state if state in (STATE_COMPLETE, STATE_INCOMPLETE, STATE_UNKNOWN) else STATE_UNKNOWN


async def async_compute_period_completeness(
    hass: HomeAssistant, entry: ConfigEntry, meter: dict
) -> dict:
    """Compute per-day coverage for the dates the user selected.

    Recorder long-term statistics are checked first; only when they yield no
    readings at all for the window is the Energa API queried (matching the
    ``verify_period`` source preference). Always returns an attribute dict with
    ``state`` (``complete`` / ``incomplete`` / ``unknown``).
    """
    options = getattr(entry, "options", {}) or {}
    start = format_period_date(options.get(CONF_VERIFY_PERIOD_START))
    end = format_period_date(options.get(CONF_VERIFY_PERIOD_END))
    if not start or not end:
        return unknown_result(period_start=start, period_end=end, source_checked=None)

    try:
        start_dt, _ = _parse_period_datetime(start)
        end_dt, end_is_date = _parse_period_datetime(end)
    except (ValueError, TypeError):
        return unknown_result(period_start=start, period_end=end)
    if end_is_date:
        end_dt = end_dt + timedelta(days=1)

    hourly = await _collect_meter_hourly(hass, meter, start_dt, end_dt)
    source = "recorder"
    if sum(len(zone) for zone in (hourly or {}).values()) == 0:
        entry_data = hass.data.get(DOMAIN, {}).get(getattr(entry, "entry_id", ""), {})
        api = entry_data.get("api") if isinstance(entry_data, dict) else None
        hourly = await _collect_meter_hourly_api(api, meter, start_dt, end_dt)
        source = "api"

    return evaluate_completeness(
        period_start=start,
        period_end=end,
        hourly_by_zone=hourly,
        registers=registers_for_meter(meter),
        source_checked=source,
        tz=TIMEZONE,
        now=datetime.now(timezone.utc),
    )


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

    The daily **``change``** column is the only trustworthy daily increment for
    the integration's ``*_stats`` sensors: their raw daily ``state`` is *not*
    the day's consumption, so summing it produced ~0 kWh monthly flows and a
    silently zero warehouse (live Wiśniowa bug). When the recorder has no
    ``change`` values at all we fall back to the reset-aware delta of the
    cumulative ``sum`` column (same helper the coordinator uses).

    Returns ``{(year, month): {suffix: kWh}}``; an empty dict means the
    recorder has no usable history (the caller then reports ``coverage_unknown``
    instead of guessing).
    """
    from .settlement import is_export_prosumer, reset_aware_delta

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
                {"change", "sum"},
            )
        ) or {}
    except Exception as err:  # noqa: BLE001 - missing recorder must not raise
        _LOGGER.debug("verify_period monthly statistics query failed: %s", err)
        return {}

    monthly: dict = {}
    for suffix, statistic_id in statistic_ids.items():
        rows = list(raw.get(statistic_id, []) or [])
        # Daily increments from ``change`` (preferred, resets are already
        # handled by the recorder). ``sum`` is kept as a reset-aware fallback
        # only when not a single row carried ``change``.
        used_change = False
        sum_by_month: dict = {}
        for row in rows:
            moment = _stat_row_moment(row.get("start"))
            if moment is None:
                continue
            change = row.get("change")
            if change is not None:
                try:
                    value = float(change)
                except (ValueError, TypeError):
                    value = None
                if value is not None:
                    bucket = monthly.setdefault((moment.year, moment.month), {})
                    bucket[suffix] = bucket.get(suffix, 0.0) + value
                    used_change = True
            sum_value = row.get("sum")
            if sum_value is not None:
                try:
                    sum_by_month.setdefault((moment.year, moment.month), []).append(
                        float(sum_value)
                    )
                except (ValueError, TypeError):
                    continue
        if not used_change:
            for key, sums in sum_by_month.items():
                delta = reset_aware_delta(sums)
                if delta:
                    bucket = monthly.setdefault(key, {})
                    bucket[suffix] = bucket.get(suffix, 0.0) + delta
    return monthly


async def _resolve_monthly_rcem(
    api: EnergaAPI | None,
    coordinator,
    months,
) -> tuple[dict, list[str], str]:
    """Historical RCEm per month from the PSE table (cached).

    The PSE RCEm page publishes every month at once, so ``api`` exposes
    :meth:`async_fetch_official_rcem_map` (one HTTP call per day, cached on the
    API and mirrored on the coordinator under ``_rcem_monthly``). Only months
    actually found are returned; the caller's ledger then applies its fallback
    RCEm and reports the missing months itself.

    Returns ``(prices, missing, source)``.
    """
    wanted = sorted({(int(y), int(m)) for (y, m) in (months or [])})
    prices: dict = {}
    missing: list[str] = []
    cache = getattr(coordinator, "_rcem_monthly", None) if coordinator is not None else None
    if not isinstance(cache, dict):
        cache = {}

    table: dict = {}
    fetch_map = getattr(api, "async_fetch_official_rcem_map", None)
    if callable(fetch_map):
        try:
            fetched = await fetch_map()
            if isinstance(fetched, dict):
                table = fetched
        except Exception as err:  # noqa: BLE001 - PSE fetch must not break the service
            _LOGGER.debug("verify_period PSE RCEm table fetch failed: %s", err)

    for year, month in wanted:
        key = (year, month)
        value = cache.get(key)
        if value is None:
            value = table.get(key)
        if value is not None:
            try:
                value = float(value)
            except (ValueError, TypeError):
                value = None
        if value is None:
            # Only historical values go into the price map; the pure ledger
            # applies ``fallback_rcem`` and reports the missing months itself.
            missing.append(f"{year:04d}-{month:02d}")
            continue
        cache[key] = value
        prices[key] = float(value)

    if coordinator is not None:
        try:
            coordinator._rcem_monthly = cache
        except Exception as err:  # noqa: BLE001 - best-effort cache
            _LOGGER.debug("Could not cache monthly RCEm on coordinator: %s", err)

    if table:
        source = "pse_table"
    elif cache and len(cache) >= len(wanted):
        source = "cache"
    else:
        source = "fallback"
    return prices, missing, source


async def _prior_monthly_flows(
    hass: HomeAssistant, meter: dict, coordinator, start_dt: datetime
) -> tuple[dict, bool]:
    """Monthly flows before ``start_dt``: recorder daily rows, else coordinator.

    The recorder's daily ``change`` rows are preferred (they are the true daily
    increments); when it has no history at all the coordinator's ``_monthly``
    cache (API YEAR charts / locally computed sums) is used so opening balances
    do not silently degrade to ``coverage_unknown``.

    Returns ``(monthly, day_accurate)``. ``day_accurate`` is ``True`` for the
    recorder source: its period-start month bucket then contains only the days
    elapsed up to ``start_dt`` (a true partial month). The coordinator cache is
    whole-month only (``False``), so a mid-month caller cannot split its start
    month and must drop it (see :func:`_drop_month`).
    """
    monthly = await _collect_monthly_flows(hass, meter, start_dt)
    if monthly:
        return monthly, True
    if coordinator is not None:
        cached = getattr(coordinator, "_monthly", None)
        if isinstance(cached, dict):
            mid = str(meter.get("meter_point_id", ""))
            cached_monthly = cached.get(mid) or {}
            if isinstance(cached_monthly, dict):
                return cached_monthly, False
    return {}, True


def _drop_month(monthly: dict, ym: tuple[int, int]) -> dict:
    """Return ``monthly`` without the ``(year, month)`` bucket (never raises)."""
    out: dict = {}
    for key, value in (monthly or {}).items():
        try:
            if (int(key[0]), int(key[1])) == ym:
                continue
        except (TypeError, ValueError, IndexError):
            pass
        out[key] = value
    return out


def _months_through(monthly: dict, start_dt: datetime) -> list[tuple[int, int]]:
    """``(year, month)`` keys of ``monthly`` at or before the start month."""
    keys: list[tuple[int, int]] = []
    for key in list(monthly or {}):
        try:
            year, month = int(key[0]), int(key[1])
        except (TypeError, ValueError, IndexError):
            continue
        if (year, month) <= (start_dt.year, start_dt.month):
            keys.append((year, month))
    return sorted(set(keys))


def _canonical_storage(hass: HomeAssistant, entry) -> object | None:
    """Return the canonical SQLite storage bound to ``entry`` (or ``None``)."""
    try:
        edata = (hass.data.get(DOMAIN, {}) or {}).get(entry.entry_id, {})
    except Exception:  # noqa: BLE001 - plain test doubles may lack .data
        return None
    if not isinstance(edata, dict):
        return None
    return edata.get("storage")


def _canonical_ppe_id(entry, meter: dict) -> str:
    """PPE id used for canonical settlement lots (mirrors the data updater)."""
    data = getattr(entry, "data", None)
    if isinstance(data, dict):
        value = data.get("ppe_id")
        if isinstance(value, str) and value:
            return value
    mid = str(meter.get("meter_point_id", ""))
    return f"PPE_{mid}" if mid else ""


def _kwh_zones(has_zones: bool) -> list[str]:
    """Zone names used for canonical kWh lots (matches the FIFO engines)."""
    return ["day", "night"] if has_zones else ["total"]


def _read_canonical_kwh_opening(
    storage, ppe_id: str, has_zones: bool, reference_date: date
):
    """``(bank_open_1, bank_open_2)`` from canonical snapshots, else ``None``."""
    if storage is None or not ppe_id:
        return None
    try:
        lots = storage.get_settlement_lots(ppe_id, unit="kWh")
    except Exception as err:  # noqa: BLE001 - canonical read must never break
        _LOGGER.debug("verify_period canonical kWh lots read failed: %s", err)
        return None
    values = openings_from_canonical_lots(
        lots, unit="kWh", zones=_kwh_zones(has_zones), reference_date=reference_date
    )
    if values is None:
        return None
    if has_zones:
        return float(values.get("day", 0.0)), float(values.get("night", 0.0))
    return float(values.get("total", 0.0)), 0.0


def _read_canonical_pln_opening(storage, ppe_id: str, reference_date: date):
    """Opening deposit (PLN) from canonical snapshots, else ``None``."""
    if storage is None or not ppe_id:
        return None
    try:
        lots = storage.get_settlement_lots(ppe_id, unit="PLN")
    except Exception as err:  # noqa: BLE001 - canonical read must never break
        _LOGGER.debug("verify_period canonical PLN lots read failed: %s", err)
        return None
    values = openings_from_canonical_lots(
        lots, unit="PLN", zones=["total"], reference_date=reference_date
    )
    if values is None:
        return None
    return float(values.get("total", 0.0))


def _write_canonical_opening(
    storage,
    ppe_id: str,
    unit: str,
    zones: list[str],
    reference_date: date,
    amounts: list,
    provenance: dict,
) -> None:
    """Persist one opening snapshot lot per zone (idempotent, best effort)."""
    if storage is None or not ppe_id:
        return
    # settlement_lot has a FK to ppe; make sure the identity row exists first
    # (never clobber an existing, richer record).
    try:
        if storage.get_ppe(ppe_id) is None:
            storage.upsert_ppe(
                PPE(
                    ppe_id=ppe_id,
                    settlement_type=(
                        SettlementType.NET_METERING
                        if unit == "kWh"
                        else SettlementType.NET_BILLING_RCEM
                    ),
                )
            )
    except Exception as err:  # noqa: BLE001 - persistence must never break a call
        _LOGGER.debug("verify_period canonical PPE upsert failed: %s", err)
    now = datetime.now(timezone.utc)
    lots: list[SettlementLot] = []
    for zone, amount in zip(zones, amounts):
        if amount is None:
            continue
        try:
            value = Decimal(str(round(float(amount), 4)))
        except (ValueError, TypeError):
            continue
        lots.append(
            SettlementLot(
                lot_id=canonical_opening_lot_id(ppe_id, unit, zone, reference_date),
                ppe_id=ppe_id,
                unit=unit,
                zone=zone,
                original_amount=value,
                remaining_amount=value,
                created_at_utc=now,
                assigned_at=reference_date,
                expires_at=reference_date,
                rule_version=CANONICAL_OPENING_RULE,
                provenance=json.dumps(
                    provenance, ensure_ascii=False, sort_keys=True, default=str
                ),
            )
        )
    if not lots:
        return
    try:
        storage.save_settlement_lots(lots)
    except Exception as err:  # noqa: BLE001 - persistence must never break a call
        _LOGGER.debug("verify_period canonical opening write failed: %s", err)


def _write_canonical_market_prices(storage, monthly_rcem: dict) -> None:
    """Persist the monthly RCEm values used for the opening FIFO (best effort)."""
    if storage is None or not monthly_rcem:
        return
    records: list[MarketPriceRecord] = []
    for (year, month), price in (monthly_rcem or {}).items():
        try:
            value = float(price)
            year_i, month_i = int(year), int(month)
        except (ValueError, TypeError):
            continue
        if not 1 <= month_i <= 12:
            continue
        records.append(
            MarketPriceRecord(
                price_type="RCEM",
                applicable_year=year_i,
                applicable_month=month_i,
                publication_date=date(year_i, month_i, 1),
                revision=1,
                price_mwh=Decimal(str(round(value * 1000.0, 4))),
                price_kwh=Decimal(str(round(value, 6))),
                source_url="verify_period",
                raw_snippet="verify_period history cache",
                resolution="1M",
            )
        )
    if not records:
        return
    try:
        storage.save_market_prices(records)
    except Exception as err:  # noqa: BLE001 - persistence must never break a call
        _LOGGER.debug("verify_period canonical market_price write failed: %s", err)


def _period_has_positive_import(hourly: dict) -> bool:
    """True when the period window has at least one positive import reading."""
    for suffix in ("import", "import_1", "import_2"):
        series = (hourly or {}).get(suffix) or {}
        for value in series.values():
            try:
                if float(value) > 0.0:
                    return True
            except (ValueError, TypeError):
                continue
    return False


def _monthly_has_export(monthly: dict) -> bool:
    """True when the prior monthly flows contain any positive export."""
    for row in (monthly or {}).values():
        if not isinstance(row, dict):
            continue
        for name in ("export", "export_1", "export_2"):
            try:
                if float(row.get(name) or 0.0) > 0.0:
                    return True
            except (ValueError, TypeError):
                continue
    return False


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


def _empty_period_result(
    meter: dict, old_system: bool, is_prosumer: bool = True
) -> dict:
    """Zeroed, JSON-safe breakdown for a meter with no data in the window."""
    from .settlement import settlement_system_name

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
        "old_system": bool(old_system) and is_prosumer,
        "system": settlement_system_name(is_prosumer, old_system),
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
        "fee_source": None,
        "kwh": kwh,
    }


async def _async_verify_period(hass: HomeAssistant, call: ServiceCall) -> dict:
    """Implementation of the ``energa_mobile.verify_period`` service."""
    return await async_verify_period_data(hass, dict(call.data or {}))


async def async_verify_period_data(
    hass: HomeAssistant, data: dict, on_progress=None
) -> dict:
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

    ``on_progress(done, total)`` — optional, forwarded to the day-by-day API
    fetch (only used when a concrete ``meter_id`` triggers the API source) so
    the caller can show progress/ETA.
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

    # Bramka kompletności: dopóki świeży status nie jest ``complete``, nie
    # liczymy rachunku "na dziurze" — zwracamy czytelny błąd zamiast wyniku.
    # Gdy cache nie istnieje wcale (brak infrastruktury), nie blokujemy.
    requested_meter = data.get("meter_id")
    if requested_meter:
        expected_start = format_period_date(data.get("start"))
        expected_end = format_period_date(data.get("end"))
        store = period_completeness_store(coordinator)
        completeness = period_completeness_get(coordinator, requested_meter)
        state = period_completeness_status(
            coordinator,
            requested_meter,
            expected_start=expected_start,
            expected_end=expected_end,
        )
        if store is not None and state != STATE_COMPLETE:
            if state == STATE_INCOMPLETE and completeness:
                warning = (
                    "Dane w wybranym okresie nie są kompletne "
                    f"({completeness.get('available_days')}/"
                    f"{completeness.get('expected_days')} dni). "
                    "Uzupełnij brakujące dni przed przeliczeniem rachunku."
                )
            else:
                warning = (
                    "Status kompletności okresu nie został jeszcze ustalony "
                    "(albo dotyczy innego zakresu dat). Odczekaj, aż encja "
                    "„Okres: kompletność danych” przyjmie wartość „complete”."
                )
            return {
                "empty": True,
                "error": "period_incomplete",
                "warning": warning,
                "period_start": start_dt.isoformat(),
                "period_end": end_dt.isoformat(),
                "completeness": completeness,
                "source": SOURCE_RECORDER,
            }

    (
        rcem_scalar,
        rcem_prices,
        rcem_source,
        rcem_warnings,
    ) = await _resolve_period_rcem(api, coordinator, entry, data, start_dt, end_dt)
    if rcem_scalar is not None:
        base_rcem = float(rcem_scalar)
    elif rcem_prices:
        base_rcem = float(rcem_prices[max(rcem_prices)])
    else:
        base_rcem = float(DEFAULT_BANK_RCE_PRICE)
    rcem_cache_sig = _rcem_cache_signature(rcem_scalar, rcem_prices)
    months = _period_months(start_dt, end_dt)
    meters = await _active_meters_for_period(
        hass, api, coordinator, data.get("meter_id")
    )

    base: dict = {
        "entry_id": entry.entry_id,
        "period_start": start_dt.isoformat(),
        "period_end": end_dt.isoformat(),
        "rcem": base_rcem,
        "rcem_source": rcem_source,
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

    from .settlement import is_export_prosumer

    results: list[dict] = []
    for meter in meters:
        old_system = _meter_old_system(entry, meter)
        coefficient = _meter_coefficient(entry, meter)
        prosumer = is_export_prosumer(meter)
        has_zones = meter.get("zone_count", 1) > 1
        meter_point_id = str(meter.get("meter_point_id", ""))
        serial = str(meter.get("meter_serial", meter_point_id))

        cache_key = (
            meter_point_id,
            start_dt.isoformat(),
            end_dt.isoformat(),
            rcem_cache_sig,
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
            api_hourly = await _collect_meter_hourly_api(
                api, meter, start_dt, end_dt, on_progress
            )
            if sum(len(zone) for zone in api_hourly.values()) > 0:
                hourly = api_hourly
            else:
                source = SOURCE_RECORDER

        meter_rcem = _effective_period_rcem(
            rcem_scalar, rcem_prices, hourly, base_rcem
        )
        total_points = sum(len(zone) for zone in hourly.values())
        if total_points == 0:
            results.append(
                {
                    **_empty_period_result(meter, old_system, prosumer),
                    "rcem": meter_rcem,
                    "rcem_source": rcem_source,
                    "source": source,
                    "cached": False,
                }
            )
            continue

        # Opening balances. Net-metering (kWh warehouse) is always rebuilt from
        # the prior monthly flows (FIFO 12m). Net-billing (PLN deposit) is now
        # rebuilt the same way: a monetary FIFO 12-month ledger over the prior
        # monthly flows, each month valued with its own historical RCEm from the
        # PSE table. Explicit ``bank_open_1/2`` / ``deposit_open_pln`` from the
        # service call remain manual overrides and skip the reconstruction.
        entry_options = dict(entry.options or {})
        # The seller product is only inferable for a real prosumer with an
        # explicitly configured settlement coefficient (the system
        # discriminates "Oferta Podstawowa" vs "taryfa urzędowa"); a plain
        # consumer or an unconfigured entry keeps the tariff defaults.
        product_system = (
            old_system
            if prosumer and _has_explicit_coefficient(entry_options, meter)
            else None
        )
        fees = fees_from_options(
            entry_options, meter.get("tariff"), old_system=product_system
        )
        fee_origin, _missing_fee_keys = fee_source(
            entry_options, meter.get("tariff"), old_system=product_system
        )

        bank_open_1: float | None = None
        bank_open_2: float | None = None
        deposit_open: float | None = None
        deposit_open_source: str | None = None
        deposit_detail: dict | None = None
        deposit_warnings: list[str] = []
        opening_source: str | None = None

        storage_inst = _canonical_storage(hass, entry)
        ppe_id = _canonical_ppe_id(entry, meter)
        reference_date = start_dt.date()
        start_is_month_start = start_dt.day <= 1

        if old_system and prosumer:
            if override_bank_1 is not None or override_bank_2 is not None:
                bank_open_1 = override_bank_1 if override_bank_1 is not None else 0.0
                bank_open_2 = override_bank_2 if override_bank_2 is not None else 0.0
                opening_source = OPENING_SOURCE_OVERRIDE
            else:
                canonical = _read_canonical_kwh_opening(
                    storage_inst, ppe_id, has_zones, reference_date
                )
                if canonical is not None:
                    bank_open_1, bank_open_2 = canonical
                    opening_source = OPENING_SOURCE_CANONICAL
                else:
                    monthly, day_accurate = await _prior_monthly_flows(
                        hass, meter, coordinator, start_dt
                    )
                    if not start_is_month_start and not day_accurate:
                        monthly = _drop_month(
                            monthly, (start_dt.year, start_dt.month)
                        )
                        deposit_warnings.append(
                            "Miesiąc startowy pochodzi z miesięcznego cache (API) — "
                            "bez podziału na dni; saldo magazynu policzono na 1. "
                            "dnia miesiąca startowego, nie na datę startu okresu."
                        )
                    bank_open_1, bank_open_2, _bank_detail = (
                        opening_bank_from_monthly_flows(
                            monthly,
                            coefficient,
                            has_zones=has_zones,
                            period_start=start_dt,
                        )
                    )
                    # Honest reconstruction guard: a 0/0 warehouse on a window
                    # with real positive imports is only trustworthy when the
                    # history actually contained export (otherwise the old
                    # state-column bug produced exactly this). With export
                    # history a 0 warehouse is a legitimate "fully consumed"
                    # result and must not degrade to coverage_unknown.
                    if (
                        not (bank_open_1 or bank_open_2)
                        and _period_has_positive_import(hourly)
                        and not _monthly_has_export(monthly)
                    ):
                        bank_open_1 = None
                        bank_open_2 = None
                    opening_source = (
                        OPENING_SOURCE_API
                        if source == SOURCE_ENERGA_API
                        else OPENING_SOURCE_RECORDER
                    )
                    _write_canonical_opening(
                        storage_inst,
                        ppe_id,
                        "kWh",
                        _kwh_zones(has_zones),
                        reference_date,
                        [bank_open_1, bank_open_2],
                        {
                            "opening_source": opening_source,
                            "has_zones": has_zones,
                            "start": base["period_start"],
                            "day_accurate": day_accurate,
                        },
                    )
        elif prosumer:
            if override_deposit is not None:
                deposit_open = override_deposit
                deposit_open_source = "override"
                opening_source = OPENING_SOURCE_OVERRIDE
            else:
                canonical_dep = _read_canonical_pln_opening(
                    storage_inst, ppe_id, reference_date
                )
                if canonical_dep is not None:
                    deposit_open = canonical_dep
                    deposit_open_source = "canonical"
                    opening_source = OPENING_SOURCE_CANONICAL
                else:
                    monthly, day_accurate = await _prior_monthly_flows(
                        hass, meter, coordinator, start_dt
                    )
                    if not start_is_month_start and not day_accurate:
                        monthly = _drop_month(
                            monthly, (start_dt.year, start_dt.month)
                        )
                        deposit_warnings.append(
                            "Miesiąc startowy pochodzi z miesięcznego cache (API) — "
                            "bez podziału na dni; saldo depozytu policzono na 1. "
                            "dnia miesiąca startowego, nie na datę startu okresu."
                        )
                    wanted = _months_through(monthly, start_dt)
                    if wanted:
                        monthly_rcem, _missing_rcem, monthly_rcem_source = (
                            await _resolve_monthly_rcem(api, coordinator, wanted)
                        )
                        deposit_open, dep_detail = (
                            opening_deposit_from_monthly_flows(
                                monthly,
                                monthly_rcem,
                                fees,
                                period_start=start_dt,
                                fallback_rcem=meter_rcem,
                            )
                        )
                        deposit_detail = dep_detail
                        if deposit_open is not None:
                            deposit_open_source = "history"
                            _LOGGER.debug(
                                "verify_period deposit_open %s = %.2f PLN "
                                "(generated=%.2f applied=%.2f expired=%.2f "
                                "months=%s rcem=%s)",
                                meter_point_id,
                                deposit_open,
                                dep_detail.get("generated_pln", 0.0),
                                dep_detail.get("applied_pln", 0.0),
                                dep_detail.get("expired_pln", 0.0),
                                dep_detail.get("months_used"),
                                monthly_rcem_source,
                            )
                        # Only months with real export matter; ``missing_rcem``
                        # is reported by the pure ledger and respects that.
                        missing_export_rcem = list(
                            dep_detail.get("missing_rcem") or []
                        )
                        if missing_export_rcem:
                            deposit_warnings.append(
                                "Brak historycznych cen RCEm (PSE) dla miesięcy: "
                                f"{', '.join(missing_export_rcem)} — użyto "
                                "bieżącej/ostatniej znanej ceny jako przybliżenia "
                                "salda depozytu."
                            )
                        if deposit_open is not None:
                            opening_source = (
                                OPENING_SOURCE_API
                                if source == SOURCE_ENERGA_API
                                else OPENING_SOURCE_RECORDER
                            )
                            _write_canonical_opening(
                                storage_inst,
                                ppe_id,
                                "PLN",
                                ["total"],
                                reference_date,
                                [deposit_open],
                                {
                                    "opening_source": opening_source,
                                    "start": base["period_start"],
                                    "day_accurate": day_accurate,
                                    "months_used": dep_detail.get("months_used"),
                                },
                            )
                            _write_canonical_market_prices(
                                storage_inst, monthly_rcem
                            )
                    else:
                        deposit_warnings.append(
                            "Brak miesięcznej historii przepływów przed okresem: "
                            "nie odtworzono salda początkowego depozytu "
                            "(net-billing)."
                        )
                    if opening_source is None:
                        opening_source = (
                            OPENING_SOURCE_API
                            if source == SOURCE_ENERGA_API
                            else OPENING_SOURCE_RECORDER
                        )
        else:
            deposit_open = override_deposit
            if override_deposit is not None:
                deposit_open_source = "override"
                opening_source = OPENING_SOURCE_OVERRIDE

        invoice = build_period_invoice(
            hourly,
            fees=fees,
            rcem=meter_rcem,
            months=months,
            old_system=old_system,
            deposit_open_pln=deposit_open,
            cover_day=0.0,
            cover_night=0.0,
            bank_open_1=bank_open_1,
            bank_open_2=bank_open_2,
            prosumer_coefficient=coefficient,
            tariff=meter.get("tariff"),
            is_prosumer=prosumer,
        )
        if fee_origin in ("partial", "defaults"):
            invoice.setdefault("warnings", [])
            invoice["warnings"] = list(invoice["warnings"]) + [
                "Część stawek taryfowych pochodzi z tabeli domyślnej "
                f"(źródło: {fee_origin}) — ustaw stawki w opcjach wpisu, aby "
                "odtworzyć rachunek co do grosza."
            ]
        if deposit_warnings:
            invoice.setdefault("warnings", [])
            invoice["warnings"] = list(invoice["warnings"]) + deposit_warnings
        if rcem_warnings:
            invoice.setdefault("warnings", [])
            invoice["warnings"] = list(invoice["warnings"]) + rcem_warnings
        result = {
            **invoice,
            "fee_source": fee_origin,
            "rcem_source": rcem_source,
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
            "opening_source": opening_source,
            "deposit_open_source": deposit_open_source,
            "deposit_history": deposit_detail,
        }
        # Do not memoise an "unknown opening balance" result: once the recorder
        # backfill completes, the next press must recompute instead of serving
        # a stale coverage_unknown reply for the whole session.
        if not result.get("coverage_unknown"):
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
    _publish_verify_results(coordinator, results)
    return response


def _publish_verify_results(coordinator, results: list[dict]) -> None:
    """Mirror per-meter results onto the coordinator for the result sensor.

    The ``Przelicz`` button stores its own result, but a direct
    ``energa_mobile.verify_period`` service call previously left
    ``sensor.*_weryfikacja_rachunku`` at ``unavailable``. Publishing here makes
    the sensor reflect the service call too, under both the meter point id and
    the serial (the sensor looks up either). Best effort: never raises.
    """
    if coordinator is None or not results:
        return
    try:
        store = getattr(coordinator, "_verify_result", None)
        if not isinstance(store, dict):
            store = {}
            coordinator._verify_result = store
        for result in results:
            if not isinstance(result, dict):
                continue
            for key in (result.get("meter_point_id"), result.get("meter_serial")):
                if key:
                    store[str(key)] = result
        try:
            coordinator.async_update_listeners()
        except Exception as err:  # noqa: BLE001 - refresh is best effort
            _LOGGER.debug("Energa: verify result listener update skipped: %s", err)
    except Exception as err:  # noqa: BLE001 - cache publish must never break
        _LOGGER.debug("Energa: verify result publish skipped: %s", err)


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
