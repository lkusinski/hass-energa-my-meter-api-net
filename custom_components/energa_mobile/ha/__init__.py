"""Home Assistant Adapter Layer for Energa My Meter.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 9 & 10.
"""

from .alerts import AlertItem, ProsumerAlertManager
from .migration_map import MigrationMap
from .recorder_adapter import RecorderAdapter, validate_and_clean_statistics

__all__ = [
    "AlertItem",
    "MigrationMap",
    "ProsumerAlertManager",
    "RecorderAdapter",
    "validate_and_clean_statistics",
]
