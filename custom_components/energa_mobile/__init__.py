"""Energa My Meter integration.

Clean rebuild with simplified architecture:
- Statistics sensors with zone support (G12w: strefa 1 + strefa 2)
- No self-healing (manual fetch_history service)
- Active meter filtering
"""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.components.recorder.models import (
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.util import dt as dt_util

from .api import (
    EnergaAPI,
    EnergaAuthError,
    EnergaConnectionError,
    EnergaTokenExpiredError,
)
from .const import (
    CONF_DEVICE_TOKEN,
    CONF_PASSWORD,
    CONF_PROSUMER_COEFFICIENT,
    CONF_USERNAME,
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
PLATFORMS = ["sensor", "button"]
TIMEZONE = ZoneInfo("Europe/Warsaw")

# Blind auto-backfill window (v0.3.0): the Energa API holds ~2 years.
# No detection probing — history just downloads in the background.
AUTO_HISTORY_DAYS = 730


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Energa My Meter from config entry."""
    # Use dedicated session to avoid clearing cookies on the shared HA session
    session = aiohttp.ClientSession()

    # Get device token from config (may not exist in old installations)
    device_token = entry.data.get(CONF_DEVICE_TOKEN) or secrets.token_hex(32)
    api = EnergaAPI(
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        device_token,
        session,
        create_session_fn=lambda: aiohttp.ClientSession(),
    )

    # Login to API (with timeout to prevent blocking HA startup)
    try:
        await asyncio.wait_for(api.async_login(), timeout=30)
    except asyncio.TimeoutError:
        _LOGGER.warning("Login timed out after 30s — Energa API may be down")
        await session.close()
        raise ConfigEntryNotReady("Login timeout — API nie odpowiada") from None
    except EnergaAuthError as err:
        await session.close()
        raise ConfigEntryAuthFailed(err) from err
    except EnergaTokenExpiredError:
        _LOGGER.debug("Token expired during setup, retrying login")
        try:
            await asyncio.wait_for(api.async_login(), timeout=30)
        except asyncio.TimeoutError:
            await session.close()
            raise ConfigEntryNotReady("Login retry timeout") from None
        except EnergaAuthError as err:
            await session.close()
            raise ConfigEntryAuthFailed(err) from err
        except EnergaConnectionError as err:
            await session.close()
            raise ConfigEntryNotReady(err) from err
    except EnergaConnectionError as err:
        await session.close()
        raise ConfigEntryNotReady(err) from err

    # Initialize Canonical SQLite Storage (v1.0 Architecture)
    from .storage.sqlite.database import CanonicalStorage
    db_path = hass.config.path(".storage", "energa_canonical.db")
    storage = CanonicalStorage(db_path)

    # Store API and storage instances
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "session": session,
        "storage": storage,
    }

    # Close session when HA shuts down
    async def _close_session(_event):
        await session.close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_CLOSE, _close_session)
    )

    # Set hass reference for statistics queries
    api.set_hass(hass)

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register fetch_history service (v0.3.2: fire-and-forget — a 730d
    # import takes an hour; the caller gets an ack + notification).
    async def fetch_history_service(call: ServiceCall) -> None:
        """Service to manually fetch historical data (background)."""
        start_date_str = call.data["start_date"]
        days = call.data.get("days", 30)
        _LOGGER.info(
            "fetch_history called: start=%s days=%s", start_date_str, days
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

        # Get fresh meter data
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

        # Filter active meters
        active_meters = [
            m
            for m in meters
            if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
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

        # Fire-and-forget: each meter imports in the background with its
        # own progress notification (energa_import_*).
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
            }
        ),
    )

    async def generate_dashboard_service(call: ServiceCall) -> None:
        """Service to generate or refresh the Energa Lovelace dashboard."""
        url_path = call.data.get("url_path", DEFAULT_URL_PATH)
        title = call.data.get("title", DEFAULT_TITLE)
        icon = call.data.get("icon", DEFAULT_ICON)
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

        meter_id = call.data.get("meter_id")
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

    # Reload integration when options change (e.g. prices updated)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # v0.3.0: blind 730-day auto-backfill in the background when the
    # entry has no statistics yet (fresh first boot). Never blocks
    # setup; progress lands in notifications (energa_import_*).
    hass.async_create_task(_maybe_auto_backfill(hass, api, entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        # Close dedicated session
        if isinstance(entry_data, dict) and "session" in entry_data:
            await entry_data["session"].close()
        # Unregister service if no more entries remain
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, "fetch_history")
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options are updated."""
    _LOGGER.debug("Options updated, reloading: %s", list(entry.options.keys()))
    await hass.config_entries.async_reload(entry.entry_id)


async def _maybe_auto_backfill(hass: HomeAssistant, api, entry: ConfigEntry) -> None:
    """Schedule the blind 730-day history import for fresh entries.

    Idempotent: runs only when none of the entry meters has Panel
    Energia statistics yet. Any failure is fully defensive — the user
    can always trigger Options → Pobierz Historię manually.
    """
    try:
        try:
            meters = await api.async_get_data(force_refresh=False)
        except Exception as err:
            _LOGGER.debug("Auto-backfill: meter fetch failed: %s", err)
            return
        active = [
            m for m in (meters or [])
            if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
        ]
        if not active:
            return
        if await _has_any_panel_statistics(hass, active):
            _LOGGER.debug("Auto-backfill: statistics already present, skipping")
            return
        try:
            start_str = (entry.data or {}).get("auto_history_start")
            start_date = datetime.strptime(start_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            start_date = datetime.now(TIMEZONE) - timedelta(days=AUTO_HISTORY_DAYS)
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
            days, start_date.date(), len(active),
        )
        for meter in active:
            await _import_meter_history(hass, api, meter, start_date, days, entry)
    except Exception as err:
        _LOGGER.debug("Auto-backfill skipped: %s", err)


async def _stat_sum_before(
    hass: HomeAssistant, statistic_id: str, when
) -> float:
    """Sum imported strictly before `when` (v0.3.4 flow anchor).

    A reimport continues its series from this base instead of restarting
    at 0 (which the recorder reads as a meter reset). Full-range
    backfills find nothing before their start and begin cleanly at 0.0.
    Fully defensive: flow backfill must never break history import.
    """
    try:
        import functools
        from datetime import timedelta

        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        try:
            start_dt = when - timedelta(days=30)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=TIMEZONE)
            end_dt = when
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=TIMEZONE)
        except Exception:
            return 0.0
        stats = await get_instance(hass).async_add_executor_job(
            functools.partial(
                statistics_during_period,
                hass, start_dt, end_dt, [statistic_id], "day", None, {"sum"},
            )
        )
        rows = (stats or {}).get(statistic_id) or []
        sums = [r.get("sum") for r in rows if r.get("sum") is not None]
        if sums:
            # v0.3.5: MAX, not last — a reset row inside the lookback
            # (partial reimport restarted at 0) must not shrink the base.
            return max(0.0, max(float(s) for s in sums))
    except Exception as err:
        _LOGGER.debug("Flow anchor lookup failed for %s: %s", statistic_id, err)
    return 0.0


async def _has_any_panel_statistics(hass: HomeAssistant, meters: list) -> bool:
    """True when any meter already has Panel Energia statistics."""
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            get_last_statistics,
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
            for entity in registry.entities.values():
                if entity.unique_id == uid:
                    wanted.append(entity.entity_id)
                    break
        if not wanted:
            return False
        stats = await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, wanted[0], True, {"sum"}
        )
        rows = (stats or {}).get(wanted[0]) or []
        return bool(rows and rows[0].get("sum") is not None)
    except Exception as err:
        _LOGGER.debug("Auto-backfill statistics check failed: %s", err)
        return True


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
        # Collect all hourly data
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

            # Rate limiting
            await asyncio.sleep(0.5)

            try:
                day_data = await api.async_get_history_hourly(
                    meter_point_id, target_day, include_timestamps=True
                )
            except Exception as err:
                _LOGGER.warning("Failed to fetch day %s: %s", target_day.date(), err)
                continue

            # Process import data (total) — use API timestamps (#26)
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

            # Process export data — use API timestamps (#26)
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

            # Process zone-specific export data
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

        # Extend import to today to prevent sum discontinuity with live stats.
        # Without this, partial imports (e.g., from March 1) create a gap:
        # import ends with sum=45, but coordinator already wrote today's stats
        # starting from sum=0, causing a spike in the Energy Panel.
        last_imported = start_date + timedelta(days=days - 1)
        today = datetime.now(TIMEZONE)
        if last_imported.date() < today.date():
            extra_start = last_imported + timedelta(days=1)
            extra_days = (today.date() - extra_start.date()).days + 1
            _LOGGER.info(
                "Extending import to today (+%d extra days) for sum continuity",
                extra_days,
            )
            for extra_offset in range(extra_days):
                target_day = (extra_start + timedelta(days=extra_offset)).replace(
                    tzinfo=TIMEZONE
                )
                if target_day.date() > today.date():
                    break

                await asyncio.sleep(0.5)

                try:
                    day_data = await api.async_get_history_hourly(
                        meter_point_id, target_day, include_timestamps=True
                    )
                except Exception as err:
                    _LOGGER.warning("Failed to fetch extra day %s: %s", target_day.date(), err)
                    continue

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

        # Map entity suffix to sensor name (shared by the anchored caller below)
        suffix_to_name = {
            "import": "panel_energia_zuzycie",
            "import_1": "panel_energia_strefa_1",
            "import_2": "panel_energia_strefa_2",
            "export": "panel_energia_produkcja",
            "export_1": "panel_energia_produkcja_strefa_1",
            "export_2": "panel_energia_produkcja_strefa_2",
        }

        def build_statistics(
            points: list, entity_suffix: str, entry: ConfigEntry,
            base: float = 0.0, cost_base: float = 0.0,
        ) -> int:
            if not points:
                return 0

            # Get price from config options
            price = get_price_for_key(dict(entry.options), entity_suffix, meter_id=str(serial))

            # Forward calculation from zero - sort oldest first
            points.sort(key=lambda x: x["dt"])

            # Merge duplicate UTC timestamps (DST spring-forward gap:
            # local 02:00 doesn't exist, so hour_idx 2 and 3 both map
            # to the same UTC hour after as_utc() conversion)
            merged: list[dict] = []
            for point in points:
                if merged and merged[-1]["dt"] == point["dt"]:
                    merged[-1]["value"] += point["value"]
                    _LOGGER.debug(
                        "DST dedup: merged %.3f kWh into %s (total %.3f)",
                        point["value"], point["dt"], merged[-1]["value"],
                    )
                else:
                    merged.append(dict(point))
            points = merged

            # v0.3.5: start from the anchor base — a partial reimport must
            # CONTINUE the recorder sums (v0.3.4 did this for flows only;
            # panels restarted at 0 and poisoned whole months).
            try:
                running_sum = max(0.0, float(base))
            except (ValueError, TypeError):
                running_sum = 0.0
            statistics = []

            for point in points:
                hourly_value = point["value"]

                # Spike guard: skip anomalous values (same as data_updater)
                if hourly_value < 0 or hourly_value > MAX_HOURLY_KWH:
                    _LOGGER.warning(
                        "Import spike guard: skipping %.1f kWh for %s at %s",
                        hourly_value, serial, point["dt"],
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

            # Build cost statistics (v0.3.0: import only — export is priced
            # live via the RCEm/Cena Oddania entity, never frozen at 0.95).
            # v0.3.5: incremental from the cost anchor (same reason as above).
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

            # Map entity suffix to sensor name
            energy_sensor_name = suffix_to_name.get(
                entity_suffix, f"panel_{entity_suffix}"
            )
            entity_id = f"sensor.energa_{meter_id}_{energy_sensor_name}"

            # Import energy statistics
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
            _LOGGER.info(
                "Imported %d energy statistics for %s", len(statistics), entity_id
            )

            # Canonically archive chunk to SQLite (v1.0 Architecture)
            storage = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("storage")
            if storage and points:
                try:
                    from decimal import Decimal
                    from .core.readings.models import IntervalReading
                    is_export = entity_suffix.startswith("export")
                    c_readings = [
                        IntervalReading(
                            ppe_id=meter_point_id,
                            meter_id=str(meter_id),
                            register=entity_suffix,
                            interval_start_utc=dt_util.as_utc(p["dt"]),
                            resolution="1h",
                            import_kwh=Decimal("0.0") if is_export else Decimal(str(round(float(p["value"]), 4))),
                            export_kwh=Decimal(str(round(float(p["value"]), 4))) if is_export else Decimal("0.0"),
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

            # Import cost statistics (skipped for export, v0.3.0)
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

        # v0.3.5: anchored panel import — look up the already-imported sum
        # before the range so a partial reimport CONTINUES the series
        # instead of restarting at 0 (a 0-based September reimport once
        # poisoned the previous month bucket and cut the FIFO bank).
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
                    eid = f"sensor.energa_{meter_id}_{energy_name}"
                    base = await _stat_sum_before(hass, eid, first_dt)
                    if not entity_suffix.startswith("export"):
                        cost_base = await _stat_sum_before(
                            hass, f"{eid}_cost", first_dt
                        )
            return build_statistics(points, entity_suffix, entry, base, cost_base)

        # Build and import statistics
        if has_zones:
            count_1 = await _build_anchored(import_1_points, "import_1")
            count_2 = await _build_anchored(import_2_points, "import_2")
            count_exp1 = await _build_anchored(export_1_points, "export_1")
            count_exp2 = await _build_anchored(export_2_points, "export_2")
            total_count = count_1 + count_2 + count_exp1 + count_exp2

            persistent_notification.async_create(
                hass,
                f"Zakończono import dla licznika {serial}\n"
                f"Zaimportowano {total_count} punktów danych\n"
                f"(Import S1: {count_1}, S2: {count_2}, "
                f"Export S1: {count_exp1}, S2: {count_exp2})",
                title="Energa: Sukces",
                notification_id=f"energa_import_{meter_id}",
            )
        else:
            count_import = build_statistics(import_points, "import", entry)
            count_export = build_statistics(export_points, "export", entry)
            total_count = count_import + count_export

            persistent_notification.async_create(
                hass,
                f"Zakończono import dla licznika {serial}\n"
                f"Zaimportowano {total_count} punktów danych\n"
                f"(Import: {count_import}, Export: {count_export})",
                title="Energa: Sukces",
                notification_id=f"energa_import_{meter_id}",
            )

        _LOGGER.info("History import complete for %s: %d points", serial, total_count)

        # === v0.2.23: backfill bank flows (Energy battery history) ===
        # Panel import rebuilds statistics; without this the battery stays
        # 0/0 until live deltas accrue. Replays live accumulator semantics
        # over the same hourly points (baselines cancel out in deltas, so
        # raw sums are used). Live sensors seed from these sums on setup.
        # v0.3.4: series are ANCHORED on already-imported sums — a partial
        # reimport continues the totals instead of restarting at 0 (which
        # the recorder reads as a meter reset, collapsing battery bars).
        try:
            from .settlement import (
                anchor_flow_series,
                bucket_flows,
                flow_history_series,
            )

            # v0.3.9: single-zone export goes to slot 1 (it sat in
            # slot 0, zeroing charge and doubling discharge).
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
                _coeff = float(entry.options.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT))
            except (ValueError, TypeError):
                _coeff = DEFAULT_PROSUMER_COEFFICIENT
            _ch, _dis = flow_history_series(
                [(v[0], v[1]) for _, v in _ordered], _coeff, _coeff >= 0.7
            )
            from homeassistant.helpers import entity_registry as er

            _reg = er.async_get(hass)
            for _direction, _series in (("charge", _ch), ("discharge", _dis)):
                _uid = f"energa_{meter_point_id}_bank_{_direction}"
                _eid = _reg.async_get_entity_id("sensor", DOMAIN, _uid)
                if not _eid:
                    _LOGGER.debug("Flow backfill: entity missing for %s", _uid)
                    continue
                _base = await _stat_sum_before(hass, _eid, start_date)
                _anchored = anchor_flow_series(_series, _base)
                _dts = [d for d, _ in _ordered]
                if len(_anchored) != len(_dts):
                    _LOGGER.debug("Flow backfill: length mismatch for %s", _eid)
                    continue
                _stats = [
                    {"start": _dt, "sum": _cum, "state": _st}
                    for (_dt, (_cum, _st)) in zip(_dts, _anchored)
                ]
                if not _stats:
                    continue
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

    except Exception as err:
        _LOGGER.error("History import failed for %s: %s", serial, err, exc_info=True)
        persistent_notification.async_create(
            hass,
            f"Błąd importu historii dla {serial}: {err}",
            title="Energa: Błąd",
            notification_id=f"energa_import_{meter_id}",
        )
