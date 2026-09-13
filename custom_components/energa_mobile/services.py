"""Service registration, backfill, and history import for Energa My Meter integration."""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime, timedelta
from decimal import Decimal
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
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .api import EnergaAPI
from .const import (
    CONF_BANK_INITIAL_KWH,
    CONF_BANK_INITIAL_KWH_L1,
    CONF_BANK_INITIAL_KWH_L2,
    CONF_ENABLE_SYNTHETIC_STORAGE,
    CONF_PROSUMER_COEFFICIENT,
    DEFAULT_ENABLE_SYNTHETIC_STORAGE,
    DEFAULT_PROSUMER_COEFFICIENT,
    DOMAIN,
    MAX_HOURLY_KWH,
    get_price_for_key,
)
from .dashboard_generator import (
    DEFAULT_ICON,
    DEFAULT_TITLE,
    DEFAULT_URL_PATH,
    async_provision_dashboard,
)

_LOGGER = logging.getLogger(__name__)
TIMEZONE = ZoneInfo("Europe/Warsaw")
AUTO_HISTORY_DAYS = 730


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


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister domain services when all entries are removed."""
    for service_name in ("fetch_history", "generate_dashboard", "reconcile_invoice"):
        if hass.services.has_service(DOMAIN, service_name):
            hass.services.async_remove(DOMAIN, service_name)


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


async def _maybe_auto_backfill(
    hass: HomeAssistant, api: EnergaAPI, entry: ConfigEntry
) -> None:
    """Schedule the blind 730-day history import for fresh entries."""
    import sys

    energa_mod = sys.modules.get("custom_components.energa_mobile")
    _prov_fn = getattr(energa_mod, "async_provision_dashboard", async_provision_dashboard)
    _has_stats_fn = getattr(energa_mod, "_has_history_statistics", _has_history_statistics)
    _import_fn = getattr(energa_mod, "_import_meter_history", _import_meter_history)

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

        # 1. Auto-provision Lovelace dashboard
        try:
            coeff = float(
                entry.options.get(
                    CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT
                )
            )
            await _prov_fn(hass, meters, coeff=coeff)
            persistent_notification.async_create(
                hass,
                f"Pulpit Energa został utworzony w menu bocznym: [/{DEFAULT_URL_PATH}](/{DEFAULT_URL_PATH})\n\n"
                "Pobieranie historii z ostatnich 2 lat wystartowało w tle. Dane wypełnią się automatycznie.",
                title="Energa: Gotowe!",
                notification_id="energa_dashboard_ready",
            )
        except Exception as d_err:
            _LOGGER.debug("Auto-provision dashboard skipped: %s", d_err)

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
            "Pobieranie historii z ostatnich 2 lat wystartowało w tle "
            f"({len(active)} liczników). Panel Energia wypełni się sam — "
            "to potrwa kilkanaście minut.",
            title="Energa: Pobieranie danych",
            notification_id="energa_auto_backfill",
        )
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
        _LOGGER.info("Auto-backfill: successfully completed for all active meters")
    except Exception as err:
        _LOGGER.debug("Auto-backfill skipped: %s", err)


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

    persistent_notification.async_create(
        hass,
        f"Rozpoczęto pobieranie historii dla licznika {serial}\n"
        f"Zakres: {days} dni od {start_date.date()}"
        + (f"\nTaryfa wielostrefowa: {meter.get('tariff')}" if has_zones else ""),
        title="Energa: Import Historii",
        notification_id=f"energa_import_{meter_id}",
    )

    try:
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

            persistent_notification.async_create(
                hass,
                f"Zakończono import historii dla licznika {serial}.\n"
                f"Zaimportowano {total_count} punktów danych (Import S1: {count_1}, S2: {count_2}, "
                f"Export S1: {count_exp1}, S2: {count_exp2}).\n\n"
                f"📊 Pulpit dostępny pod adresem: [/{DEFAULT_URL_PATH}](/{DEFAULT_URL_PATH})"
                + panel_hint,
                title="Energa: Sukces importu historii",
                notification_id=f"energa_import_{meter_id}",
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

            persistent_notification.async_create(
                hass,
                f"Zakończono import historii dla licznika {serial}.\n"
                f"Zaimportowano {total_count} punktów danych (Import: {count_import}, Export: {count_export}).\n\n"
                f"📊 Pulpit dostępny pod adresem: [/{DEFAULT_URL_PATH}](/{DEFAULT_URL_PATH})"
                + panel_hint,
                title="Energa: Sukces importu historii",
                notification_id=f"energa_import_{meter_id}",
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
        persistent_notification.async_create(
            hass,
            f"Błąd importu historii dla {serial}: {err}",
            title="Energa: Błąd",
            notification_id=f"energa_import_{meter_id}",
        )
