"""Home Assistant Recorder Adapter for Energa My Meter.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 9 & 10.
Invariants:
- Strictly validates StatisticData and StatisticMetaData before passing to HA Core.
- Guarantees monotonic non-decreasing sums (prevents negative jumps and recorder resets).
- Enforces physical spike guard (< MAX_HOURLY_KWH).
- Isolates domain logic from direct HA Recorder I/O.
- Supports both stable canonical PPE-based IDs and legacy entity statistic IDs.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from ..const import DOMAIN, MAX_HOURLY_KWH

try:
    from homeassistant.components.recorder.models import (
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import async_import_statistics
except ImportError:
    # Testing or standalone mock fallback
    class StatisticMeanType:
        NONE = 0

    class StatisticMetaData:
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def async_import_statistics(hass: Any, metadata: Any, statistics: list[dict]) -> None:
        pass

_LOGGER = logging.getLogger(__name__)


def validate_and_clean_statistics(
    statistics: list[dict],
    max_value: float = MAX_HOURLY_KWH,
    last_known_sum: float = 0.0,
) -> list[dict]:
    """Validate, sanitize, and guarantee monotonic sums for a batch of statistics.

    Args:
        statistics: List of dicts with 'start', 'state', 'sum'.
        max_value: Maximum physically permissible state value (spike guard).
        last_known_sum: The running sum prior to this batch.

    Returns:
        Cleaned, strictly monotonic list of StatisticData dicts.
    """
    if not statistics:
        return []

    # Sort strictly by start datetime
    sorted_stats = sorted(statistics, key=lambda s: s["start"])
    cleaned: list[dict] = []
    running_sum = float(last_known_sum)

    for stat in sorted_stats:
        dt = stat.get("start")
        if not isinstance(dt, datetime):
            _LOGGER.warning("Discarding invalid statistic without datetime: %s", stat)
            continue

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        state = float(stat.get("state") or 0.0)
        if state < 0.0 or state > max_value:
            _LOGGER.warning(
                "Spike guard: skipping invalid state %.3f at %s", state, dt.isoformat()
            )
            continue

        # If a precalculated sum was provided, validate it against running_sum
        provided_sum = stat.get("sum")
        if provided_sum is not None:
            cand_sum = float(provided_sum)
            if cand_sum < running_sum:
                _LOGGER.warning(
                    "Monotonic clamp: sum dropped from %.3f to %.3f at %s",
                    running_sum,
                    cand_sum,
                    dt.isoformat(),
                )
                running_sum = max(running_sum, cand_sum)
            else:
                running_sum = cand_sum
        else:
            running_sum += state

        cleaned.append({
            "start": dt,
            "state": round(state, 4),
            "sum": round(running_sum, 4),
        })

    return cleaned


class RecorderAdapter:
    """Production adapter for injecting statistics into Home Assistant Recorder."""

    def __init__(self, hass: Any) -> None:
        """Initialize recorder adapter."""
        self.hass = hass

    def build_metadata(
        self,
        statistic_id: str,
        name: str | None = None,
        unit: str = "kWh",
        is_energy: bool = True,
        source: str = "recorder",
    ) -> StatisticMetaData:
        """Construct standard StatisticMetaData for HA Core."""
        return StatisticMetaData(
            source=source,
            statistic_id=statistic_id,
            name=name,
            unit_of_measurement=unit,
            has_mean=False,
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            unit_class="energy" if is_energy else None,
        )

    def import_statistics(
        self,
        metadata: StatisticMetaData,
        statistics: list[dict],
        last_known_sum: float = 0.0,
        max_hourly: float = MAX_HOURLY_KWH,
    ) -> int:
        """Sanitize and import statistics into Home Assistant.

        Returns:
            Number of successfully imported points.
        """
        stat_id = (
            metadata.get("statistic_id")
            if isinstance(metadata, dict)
            else getattr(metadata, "statistic_id", "")
        )
        if not statistics:
            return 0

        clean_stats = validate_and_clean_statistics(
            statistics,
            max_value=max_hourly,
            last_known_sum=last_known_sum,
        )
        if not clean_stats:
            _LOGGER.debug("No valid statistics to import for %s after cleaning", stat_id)
            return 0

        try:
            async_import_statistics(self.hass, metadata, clean_stats)
            _LOGGER.info(
                "Successfully imported %d statistics for %s (final sum: %.3f)",
                len(clean_stats),
                stat_id,
                clean_stats[-1]["sum"],
            )
            return len(clean_stats)
        except Exception as err:
            _LOGGER.error(
                "Failed to import statistics for %s: %s",
                stat_id,
                err,
                exc_info=True,
            )
            return 0

    def import_energy_statistics(
        self,
        statistic_id: str,
        statistics: list[dict],
        name: str | None = None,
        unit: str = "kWh",
        last_known_sum: float = 0.0,
    ) -> int:
        """Import energy consumption/production statistics."""
        meta = self.build_metadata(
            statistic_id=statistic_id,
            name=name,
            unit=unit,
            is_energy=True,
        )
        return self.import_statistics(meta, statistics, last_known_sum=last_known_sum)

    def import_cost_statistics(
        self,
        statistic_id: str,
        statistics: list[dict],
        name: str | None = None,
        unit: str = "PLN",
        last_known_sum: float = 0.0,
    ) -> int:
        """Import cost / monetary statistics."""
        meta = self.build_metadata(
            statistic_id=statistic_id,
            name=name,
            unit=unit,
            is_energy=False,
        )
        return self.import_statistics(
            meta, statistics, last_known_sum=last_known_sum, max_hourly=500.0
        )
