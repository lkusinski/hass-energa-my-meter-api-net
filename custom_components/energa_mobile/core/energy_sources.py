"""Read-only helpers for the native Home Assistant Energy panel sources.

Detection is purely advisory and must never raise: the built-in Energy
dashboard may be unconfigured, the energy component may be unavailable or
its manager may not be initialised yet. All such cases degrade to an empty
result so callers (onboarding wizard, diagnostics) can continue safely.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_energy_solar_sources(hass: HomeAssistant) -> list[dict]:
    """Return the Energy panel ``energy_sources`` entries of type ``solar``.

    The Home Assistant Energy manager keeps the user's preferences under
    ``manager.data['energy_sources']``. We only read it; nothing is written
    back. A missing manager, missing/unexpected data or any error yields an
    empty list (logged at debug level only).
    """
    try:
        from homeassistant.components.energy.data import async_get_manager

        manager = await async_get_manager(hass)
        if manager is None:
            return []
        data = getattr(manager, "data", None)
        if not isinstance(data, dict):
            return []
        sources = data.get("energy_sources") or []
    except Exception:  # noqa: BLE001 - detection must never break the caller
        _LOGGER.debug("Energy panel solar source detection failed", exc_info=True)
        return []

    solar_sources: list[dict] = []
    try:
        for source in sources:
            if isinstance(source, dict) and source.get("type") == "solar":
                solar_sources.append(source)
    except TypeError:
        return []
    return solar_sources


async def async_has_energy_solar(hass: HomeAssistant) -> bool:
    """Return True when the Energy panel has at least one ``solar`` source."""
    return bool(await async_energy_solar_sources(hass))
