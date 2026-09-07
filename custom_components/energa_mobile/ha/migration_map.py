"""Compatibility and Migration Map for Energa Home Assistant Statistics.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 9 & 12.
Invariants:
- Maps legacy meter-serial based IDs (sensor.energa_{serial}_...) to stable PPE-based IDs.
- Meter replacement continuity: ensures replacing physical meter keeps continuous cumulative sums
  for the PPE without step jumps, negative drops, or loss of history in Energy Dashboard.
"""

from __future__ import annotations

from decimal import Decimal
import logging
from typing import Any

from ..projections.statistics import build_statistic_id
from ..core.identity.models import MeterLifecycle, MeterReadingOffset

_LOGGER = logging.getLogger(__name__)


class MigrationMap:
    """Manage mapping between physical meter serials, PPEs, and statistic IDs."""

    def __init__(self) -> None:
        """Initialize migration map."""
        # ppe_id -> list of serials in chronological order
        self._ppe_to_serials: dict[str, list[str]] = {}
        # serial -> ppe_id
        self._serial_to_ppe: dict[str, str] = {}
        # (ppe_id, serial, register) -> offset
        self._offsets: dict[tuple[str, str, str], Decimal] = {}

    def register_meter(
        self,
        ppe_id: str,
        serial: str,
        is_active: bool = True,
    ) -> None:
        """Register a meter serial under a logical PPE point."""
        clean_ppe = ppe_id.strip()
        clean_serial = str(serial).strip()

        self._serial_to_ppe[clean_serial] = clean_ppe
        if clean_ppe not in self._ppe_to_serials:
            self._ppe_to_serials[clean_ppe] = []

        if clean_serial not in self._ppe_to_serials[clean_ppe]:
            self._ppe_to_serials[clean_ppe].append(clean_serial)

    def get_ppe_for_serial(self, serial: str) -> str | None:
        """Find PPE ID for a given meter serial."""
        return self._serial_to_ppe.get(str(serial).strip())

    def get_serials_for_ppe(self, ppe_id: str) -> list[str]:
        """Get all meter serials associated with a PPE."""
        return list(self._ppe_to_serials.get(ppe_id.strip(), []))

    def get_canonical_statistic_id(
        self,
        ppe_id: str,
        metric: str,
        zone: str = "total",
    ) -> str:
        """Return the stable canonical statistic ID tied to PPE."""
        return build_statistic_id(ppe_id, metric, zone)

    def get_legacy_statistic_id(
        self,
        serial: str,
        metric: str,
    ) -> str:
        """Return legacy entity statistic ID for backward compatibility."""
        return f"sensor.energa_{serial}_{metric}"

    def register_meter_replacement(
        self,
        ppe_id: str,
        old_serial: str,
        new_serial: str,
        register: str,
        old_meter_final_sum: Decimal,
        new_meter_initial_reading: Decimal = Decimal("0.0"),
    ) -> Decimal:
        """Register replacement of a meter and calculate the continuous offset.

        Offset formula:
            offset = old_meter_final_sum - new_meter_initial_reading
        Such that:
            continuous_sum = new_meter_reading + offset
            continuous_sum(initial) = new_meter_initial_reading + (old_meter_final_sum - new_meter_initial_reading)
                                    = old_meter_final_sum
        """
        clean_ppe = ppe_id.strip()
        self.register_meter(clean_ppe, old_serial, is_active=False)
        self.register_meter(clean_ppe, new_serial, is_active=True)

        offset = old_meter_final_sum - new_meter_initial_reading
        key = (clean_ppe, str(new_serial).strip(), register.lower())
        self._offsets[key] = offset

        _LOGGER.info(
            "Meter replacement registered for PPE %s [%s -> %s, reg %s]: offset=%.3f (old_sum=%.3f, new_initial=%.3f)",
            clean_ppe,
            old_serial,
            new_serial,
            register,
            float(offset),
            float(old_meter_final_sum),
            float(new_meter_initial_reading),
        )
        return offset

    def get_offset(
        self,
        ppe_id: str,
        serial: str,
        register: str,
    ) -> Decimal:
        """Retrieve registered cumulative offset for meter and register."""
        key = (ppe_id.strip(), str(serial).strip(), register.lower())
        return self._offsets.get(key, Decimal("0.0"))

    def compute_continuous_reading(
        self,
        ppe_id: str,
        serial: str,
        register: str,
        current_reading: Decimal,
    ) -> Decimal:
        """Compute the adjusted continuous cumulative reading applying lifecycle offset."""
        offset = self.get_offset(ppe_id, serial, register)
        return current_reading + offset
