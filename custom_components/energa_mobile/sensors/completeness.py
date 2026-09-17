"""Period completeness sensor for Energa My Meter.

Companion to the period date/button/result trio: it tells the user whether the
readings inside the selected ``Okres Start``/``Okres Koniec`` window are fully
covered. The result sensor and the ``Przelicz okres rozliczeniowy`` button must
not produce an invoice over a window with gaps, so the button is gated on this
sensor reporting ``complete``.

The heavy check (recorder LTS first, Energa API as fallback) is delegated to
``services.async_compute_period_completeness`` and cached on the coordinator so
the button can read it without re-querying. Recompute is triggered on entity
add and whenever the smart period listener fires (date change without reload).
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import (
    CONF_VERIFY_PERIOD_END,
    CONF_VERIFY_PERIOD_START,
)
from ..core.completeness import (
    STATE_COMPLETE,
    STATE_INCOMPLETE,
    STATE_UNKNOWN,
)
from ..core.verification import format_period_date

_LOGGER = logging.getLogger(__name__)


class EnergaPeriodCompletenessSensor(CoordinatorEntity, SensorEntity):
    """Data coverage status ("complete"/"incomplete"/"unknown") for the period."""

    _attr_has_entity_name = True
    _attr_name = "Okres: kompletność danych"
    _attr_translation_key = "period_completeness"
    _attr_icon = "mdi:calendar-check-outline"
    # Diagnostic entity: the completeness status is not a control and must not
    # be offered as an Energy Dashboard source.
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATE_COMPLETE, STATE_INCOMPLETE, STATE_UNKNOWN]
    _attr_state_class = None

    def __init__(
        self,
        coordinator,
        meter: dict,
        meter_id: str,
        serial: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._meter = dict(meter or {})
        self._meter_id = str(meter_id)
        self._serial = str(serial or meter_id)
        self._state = STATE_UNKNOWN
        self._attrs = self._base_unknown_attributes()
        self._refresh_generation = 0
        self._refresh_task = None
        self._attr_unique_id = f"energa_{self._meter_id}_period_completeness"
        self.entity_id = f"sensor.energa_{self._serial}_okres_kompletnosc".lower()
        self._attr_device_info = device_info

    def _base_unknown_attributes(self) -> dict:
        options = getattr(self._entry, "options", {}) or {}
        return {
            "state": STATE_UNKNOWN,
            "period_start": format_period_date(
                options.get(CONF_VERIFY_PERIOD_START)
            ),
            "period_end": format_period_date(options.get(CONF_VERIFY_PERIOD_END)),
            "expected_days": 0,
            "available_days": 0,
            "missing_days": [],
            "completeness_pct": 0.0,
            "source_checked": None,
            "checked_at": None,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to period changes and run the first check."""
        try:
            await super().async_added_to_hass()
        except AttributeError:  # pragma: no cover - test double without base method
            pass
        try:
            from homeassistant.helpers.dispatcher import async_dispatcher_connect

            from ..const import SIGNAL_PERIOD_OPTIONS_UPDATED

            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    SIGNAL_PERIOD_OPTIONS_UPDATED,
                    self._handle_period_options_updated,
                )
            )
        except Exception as err:  # noqa: BLE001 - subscription is best effort
            _LOGGER.debug("Energa: completeness subscription skipped: %s", err)
        self._schedule_refresh()

    def _in_event_loop(self) -> bool:
        """True when called from a running asyncio event loop (thread-safe)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    def _call_in_loop(self, func, *args) -> None:
        """Run ``func`` in the HA event loop, even when called from a worker.

        Writing entity state and creating tasks from a non-loop thread is a
        hard error in HA 2026.9 (``async_write_ha_state`` / ``async_create_task``
        outside the loop). Scheduling through ``hass.loop.call_soon_threadsafe``
        keeps every state write and task creation on the loop.
        """
        loop = getattr(self.hass, "loop", None)
        if loop is None:
            func(*args)
            return
        if self._in_event_loop():
            func(*args)
            return
        try:
            loop.call_soon_threadsafe(func, *args)
        except Exception as err:  # noqa: BLE001 - scheduling must never raise
            _LOGGER.debug("Energa: completeness loop scheduling skipped: %s", err)

    def _safe_write_state(self) -> None:
        def _write() -> None:
            try:
                self.async_write_ha_state()
            except Exception as err:  # noqa: BLE001 - state write is best effort
                _LOGGER.debug("Energa: completeness state write skipped: %s", err)

        self._call_in_loop(_write)

    def _handle_period_options_updated(self, entry_id: str) -> None:
        if entry_id != getattr(self._entry, "entry_id", None):
            return
        # Newest wins: drop any in-flight check for the previous window.
        self._cancel_refresh_task()
        self._attrs = self._base_unknown_attributes()
        self._state = STATE_UNKNOWN
        # Publish the fresh "unknown" immediately so the button gate never
        # keeps reading a stale "complete" from before the date change.
        self._publish()
        self._safe_write_state()
        self._schedule_refresh()

    def _cancel_refresh_task(self) -> None:
        """Cancel a previously scheduled completeness task (newest wins)."""
        task = self._refresh_task
        self._refresh_task = None
        if task is None:
            return
        try:
            cancel = getattr(task, "cancel", None)
            if callable(cancel):
                cancel()
        except Exception as err:  # noqa: BLE001 - cancellation is best effort
            _LOGGER.debug("Energa: completeness task cancel skipped: %s", err)

    def _create_refresh_task(self, coro) -> None:
        """Create the background task on the event loop and keep a reference."""
        try:
            if hasattr(self._entry, "async_create_background_task"):
                self._refresh_task = self._entry.async_create_background_task(
                    self.hass, coro, name=f"energa_completeness_{self._meter_id}"
                )
                return
        except Exception as err:  # noqa: BLE001 - scheduling must never raise
            _LOGGER.debug("Energa: completeness background task skipped: %s", err)
        try:
            self._refresh_task = self.hass.async_create_task(coro)
        except Exception as err:  # noqa: BLE001 - scheduling must never raise
            _LOGGER.debug("Energa: completeness task skipped: %s", err)
            try:
                coro.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass

    def _schedule_refresh(self) -> None:
        """Fire-and-forget a background completeness recompute (newest wins)."""
        self._cancel_refresh_task()
        coro = self.async_refresh()
        if self._in_event_loop():
            self._create_refresh_task(coro)
        else:
            loop = getattr(self.hass, "loop", None)
            if loop is None:
                try:
                    coro.close()
                except Exception:  # noqa: BLE001 - closing must never raise
                    pass
                return
            loop.call_soon_threadsafe(self._create_refresh_task, coro)

    async def async_refresh(self) -> dict:
        """Recompute and publish the completeness status (newest wins).

        Each call takes a new generation number; a slower check that finishes
        after a date change has scheduled a newer one is discarded instead of
        overwriting the fresh verdict.
        """
        self._refresh_generation += 1
        generation = self._refresh_generation
        result = None
        try:
            from ..services import async_compute_period_completeness

            result = await async_compute_period_completeness(
                self.hass, self._entry, self._meter
            )
        except Exception as err:  # noqa: BLE001 - never break the entity loop
            _LOGGER.debug("Energa: completeness compute failed: %s", err)
            result = None

        if generation != self._refresh_generation:
            return self._attrs

        if isinstance(result, dict):
            self._apply(result)
        return self._attrs

    def _apply(self, result: dict) -> None:
        self._state = str(result.get("state") or STATE_UNKNOWN)
        self._attrs = result
        self._publish()
        self._safe_write_state()

    def _publish(self) -> None:
        """Share the status with the button via the coordinator cache + signal."""
        if not self._in_event_loop():
            self._call_in_loop(self._publish)
            return
        coordinator = self.coordinator
        if coordinator is not None:
            store = getattr(coordinator, "_period_completeness", None)
            if not isinstance(store, dict):
                store = {}
                try:
                    coordinator._period_completeness = store
                except Exception:  # noqa: BLE001 - cache set must never raise
                    store = None
            if store is not None:
                store[self._meter_id] = self._attrs
                try:
                    coordinator.async_update_listeners()
                except Exception as err:  # noqa: BLE001 - refresh is best effort
                    _LOGGER.debug("Energa: completeness listener update skipped: %s", err)
        try:
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            from ..const import SIGNAL_PERIOD_COMPLETENESS_UPDATED

            async_dispatcher_send(
                self.hass,
                SIGNAL_PERIOD_COMPLETENESS_UPDATED,
                getattr(self._entry, "entry_id", None),
            )
        except Exception as err:  # noqa: BLE001 - signal is best effort
            _LOGGER.debug("Energa: completeness signal skipped: %s", err)

    @property
    def native_value(self):
        """One of ``complete`` / ``incomplete`` / ``unknown``."""
        return self._state

    @property
    def extra_state_attributes(self) -> dict:
        return dict(self._attrs)

    @property
    def available(self) -> bool:
        """Always available: even ``unknown`` is a meaningful status."""
        return True


__all__ = ["EnergaPeriodCompletenessSensor", "STATE_COMPLETE", "STATE_INCOMPLETE", "STATE_UNKNOWN"]
