"""Identity domain package (PPE and Meter Lifecycle)."""

from .models import PPE, MeterLifecycle, SettlementType


def canonical_ppe_id(entry_data: dict | None, meter: dict | None) -> str:
    """Canonical PPE id shared by interval readings and settlement lots.

    The historical import (``_import_meter_history``) and the live
    persistence (``EnergaDataUpdater``) as well as the ``verify_period``
    opening snapshots must all agree on one identity for a physical meter.
    Otherwise ``interval_reading`` ends up with two identities for the same
    meter (e.g. ``372197`` vs ``PPE_372197``) and readers silently mix or
    miss the live series.

    Prefer an explicit ``ppe_id`` stored on the entry; otherwise fall back to
    the stable synthetic ``PPE_<meter_point_id>``.
    """
    data = entry_data if isinstance(entry_data, dict) else {}
    value = data.get("ppe_id")
    if isinstance(value, str) and value:
        return value
    mid = ""
    if isinstance(meter, dict):
        mid = str(meter.get("meter_point_id") or "").strip()
    return f"PPE_{mid}" if mid else ""


def canonical_meter_id(meter: dict | None, fallback: str = "") -> str:
    """Stable ``meter_id`` stored alongside :func:`canonical_ppe_id`.

    Uses ``meter_point_id`` (the value the live sensor path already persists)
    so the historical import stops writing the serial under a second identity.
    """
    if isinstance(meter, dict):
        mid = meter.get("meter_point_id")
        if mid not in (None, ""):
            return str(mid)
    return str(fallback or "")


__all__ = [
    "PPE",
    "MeterLifecycle",
    "SettlementType",
    "canonical_ppe_id",
    "canonical_meter_id",
]
