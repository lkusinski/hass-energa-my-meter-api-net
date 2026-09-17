"""Energa My Meter integration.

Clean rebuild with simplified architecture:
- Modular services and background history backfill (services.py)
- Domain-driven storage and recorder adapters
- Active meter filtering and multi-platform forwarding
"""

from __future__ import annotations

import asyncio
import logging
import secrets

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

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
    DOMAIN,
)
from .dashboard_generator import async_provision_dashboard
from .services import (
    AUTO_HISTORY_DAYS,
    TIMEZONE,
    _has_any_panel_statistics,
    _has_history_statistics,
    _import_meter_history,
    _maybe_auto_backfill,
    _stat_sum_before,
    async_register_services,
    async_unregister_services,
)

__all__ = [
    "AUTO_HISTORY_DAYS",
    "DOMAIN",
    "PLATFORMS",
    "TIMEZONE",
    "_has_any_panel_statistics",
    "_has_history_statistics",
    "_import_meter_history",
    "_maybe_auto_backfill",
    "_stat_sum_before",
    "async_provision_dashboard",
    "async_setup_entry",
    "async_unload_entry",
]

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "button", "binary_sensor"]


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

    # Initialize Canonical SQLite Storage & HA Production Adapters (v1.0 Architecture)
    from .ha.alerts import ProsumerAlertManager
    from .ha.migration_map import MigrationMap
    from .ha.recorder_adapter import RecorderAdapter
    from .storage.sqlite.database import CanonicalStorage

    db_path = hass.config.path(".storage", "energa_canonical.db")
    storage = CanonicalStorage(db_path)
    recorder_adapter = RecorderAdapter(hass)
    migration_map = MigrationMap()
    alert_manager = ProsumerAlertManager(storage)

    # Initialize coordinator before setting up platforms so all platforms have access
    from .coordinator import EnergaCoordinator

    coordinator = EnergaCoordinator(hass, api, entry, storage=storage)

    # Store API and storage instances
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "session": session,
        "storage": storage,
        "recorder_adapter": recorder_adapter,
        "migration_map": migration_map,
        "alert_manager": alert_manager,
        "coordinator": coordinator,
    }

    # Initial data fetch for coordinator
    try:
        await coordinator.async_config_entry_first_refresh()
        _LOGGER.debug("Energa: Initial coordinator refresh successful")
    except Exception as err:
        _LOGGER.warning("Energa: Initial coordinator fetch failed, will retry: %s", err)

    # v1.9.0 backfill: an entry created before v0.3.8 that never stored
    # `prosumer_coefficient` inherits the 0.8 default, which mislabels a plain
    # consumer as old net-metering. When the meter list is known and no meter
    # exports, pin the coefficient to the wizard's "brak" answer (0.0).
    try:
        from .settlement import consumer_coefficient_needed

        if consumer_coefficient_needed(
            getattr(api, "_meters_data", None), entry.options
        ):
            _new_opts = dict(entry.options)
            _new_opts[CONF_PROSUMER_COEFFICIENT] = 0.0
            hass.config_entries.async_update_entry(entry, options=_new_opts)
            _LOGGER.info(
                "Energa: no export prosumer on entry %s — pinned prosumer_coefficient=0.0",
                entry.entry_id,
            )
    except Exception as err:  # noqa: BLE001 - backfill must never break setup
        _LOGGER.debug("Energa: consumer coefficient backfill skipped: %s", err)

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

    # Register domain services
    await async_register_services(hass)

    # Reload integration when options change (e.g. prices updated)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # v0.3.0 / v1.1.2: blind 730-day auto-backfill in the background when the
    # entry has no statistics yet (fresh first boot). Uses background task
    # so HA startup bootstrap is never blocked.
    if hasattr(entry, "async_create_background_task"):
        entry.async_create_background_task(
            hass, _maybe_auto_backfill(hass, api, entry), name="energa_auto_backfill"
        )
    else:
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
        # Unregister services if no more entries remain
        if not hass.data[DOMAIN]:
            await async_unregister_services(hass)
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options are updated."""
    _LOGGER.debug("Options updated, reloading: %s", list(entry.options.keys()))
    await hass.config_entries.async_reload(entry.entry_id)
