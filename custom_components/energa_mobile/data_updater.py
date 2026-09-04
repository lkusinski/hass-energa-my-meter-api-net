"""
Data updater for Energa My Meter - Smart Statistics.

Forward-only calculation: adds hourly values to last known sum (or 0).
Guarantees monotonically increasing, non-negative sums.
"""

from decimal import Decimal
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    MAX_HOURLY_KWH,
    get_price_for_key,
)
from .core.readings.models import IntervalReading
from .storage.sqlite.database import CanonicalStorage

_LOGGER = logging.getLogger(__name__)


class EnergaDataUpdater:
    """Handle incremental statistics updates for Energa sensors with canonical persistence."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        pre_fetched_stats: dict | None = None,
        storage: CanonicalStorage | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._pre_fetched_stats = pre_fetched_stats or {}
        if storage is not None:
            self.storage = storage
        elif hass and entry and DOMAIN in getattr(hass, "data", {}) and entry.entry_id in hass.data.get(DOMAIN, {}):
            self.storage = hass.data[DOMAIN][entry.entry_id].get("storage")
        else:
            self.storage = None

    def gather_stats_for_sensor(
        self,
        meter_id: str,
        data_key: str,
        hourly_data: list[dict],
        entity_id: str,
    ) -> tuple[list, list]:
        """Build statistics for import into recorder.

        Always uses forward calculation: adds hourly values to last known sum.
        Starts from sum=0 when no existing stats are available.
        """
        if not hourly_data:
            _LOGGER.debug("No hourly data for %s", entity_id)
            return [], []

        # Persist to SQLite Canonical Storage if available
        if self.storage:
            self._persist_canonical_readings(meter_id, data_key, hourly_data)

        # Get price for cost calculation
        price = get_price_for_key(dict(self.entry.options), data_key, meter_id=meter_id)

        # Forward calculation - from last known sum or 0
        pre_fetched = self._pre_fetched_stats.get(entity_id)

        if pre_fetched and pre_fetched.get("sum") is not None:
            energy_stats = self._forward_calculation(
                hourly_data, pre_fetched, entity_id
            )
        else:
            # First run or after stats clear - start from 0
            energy_stats = self._forward_calculation(
                hourly_data, {"sum": 0, "start": None}, entity_id
            )

        if not energy_stats:
            return [], []

        # v0.3.0: export has no static cost (old net-metering sells
        # nothing; new net-billing pays live RCEm×1.23 via price entity).
        if data_key.startswith("export"):
            return energy_stats, []

        # Build cost statistics (derived from energy sum)
        cost_stats = []
        for stat in energy_stats:
            hourly_energy = stat["state"] or 0
            cost_stats.append(
                {
                    "start": stat["start"],
                    "sum": stat["sum"] * price,
                    "state": hourly_energy * price,
                }
            )

        _LOGGER.info(
            "DataUpdater built %d energy stats, %d cost stats for %s",
            len(energy_stats),
            len(cost_stats),
            entity_id,
        )

        return energy_stats, cost_stats

    def _forward_calculation(
        self, hourly_data: list[dict], pre_fetched: dict, entity_id: str
    ) -> list[dict]:
        """Forward calculation: add hourly values to last known sum.

        Guarantees monotonically increasing sums, consistent with existing stats.
        Only writes NEW points (after last known stat).
        """
        last_sum = pre_fetched.get("sum", 0)
        last_start = pre_fetched.get("start")

        # Sort oldest first
        sorted_data = sorted(hourly_data, key=lambda x: x["dt"])

        # Filter: only points AFTER last known stat
        if last_start is not None:
            if isinstance(last_start, (int, float)):
                last_dt = dt_util.utc_from_timestamp(last_start)
            else:
                last_dt = last_start
            sorted_data = [p for p in sorted_data if p["dt"] > last_dt]

        if not sorted_data:
            _LOGGER.debug(
                "Forward calc: no new points after last stat for %s", entity_id
            )
            return []

        running_sum = last_sum
        energy_stats = []

        for point in sorted_data:
            hourly_value = point.get("value") if point.get("value") is not None else 0

            if hourly_value < 0 or hourly_value > MAX_HOURLY_KWH:
                _LOGGER.warning(
                    "Spike guard: skipping %.1f kWh for %s", hourly_value, entity_id
                )
                continue

            running_sum += hourly_value
            energy_stats.append(
                {
                    "start": point["dt"],
                    "sum": running_sum,
                    "state": hourly_value,
                }
            )

        _LOGGER.debug(
            "Forward calc for %s: last_sum=%.3f, new_points=%d, final_sum=%.3f",
            entity_id,
            last_sum,
            len(energy_stats),
            running_sum,
        )

        return energy_stats

    def _persist_canonical_readings(
        self, meter_id: str, data_key: str, hourly_data: list[dict]
    ) -> None:
        """Canonically archive interval readings into SQLite WAL database."""
        ppe_id = self.entry.data.get("ppe_id") or f"PPE_{meter_id}"
        readings: list[IntervalReading] = []
        is_export = data_key.startswith("export")
        for point in hourly_data:
            dt = point.get("dt")
            val = point.get("value")
            if dt is None or val is None or val < 0:
                continue
            val_dec = Decimal(str(round(float(val), 4)))
            utc_dt = dt_util.as_utc(dt) if hasattr(dt, "tzinfo") and dt.tzinfo else dt
            readings.append(
                IntervalReading(
                    ppe_id=ppe_id,
                    meter_id=str(meter_id),
                    register=data_key,
                    interval_start_utc=utc_dt,
                    resolution="1h",
                    import_kwh=Decimal("0.0") if is_export else val_dec,
                    export_kwh=val_dec if is_export else Decimal("0.0"),
                    quality="ok",
                    source="energa",
                )
            )
        if readings and self.storage:
            try:
                self.storage.insert_readings_idempotent(readings)
            except Exception as err:
                _LOGGER.debug("CanonicalStorage insert failed for %s: %s", data_key, err)


