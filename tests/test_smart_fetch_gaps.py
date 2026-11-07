"""Regression tests for issue #7: per-register statistics watermarks."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.energa_mobile.coordinator import EnergaCoordinator

TZ = ZoneInfo("Europe/Warsaw")


def _coordinator(register_stats):
    coordinator = EnergaCoordinator.__new__(EnergaCoordinator)
    coordinator._last_register_stats = register_stats
    return coordinator


def test_start_uses_earliest_register_watermark():
    """Export lagging behind import must pull the fetch window back."""
    now = datetime(2026, 11, 7, 22, 0, tzinfo=TZ)
    coordinator = _coordinator(
        {
            "m1": {
                "import": {"sum": 100.0, "start": now - timedelta(hours=1)},
                "export": {"sum": 50.0, "start": now - timedelta(hours=4)},
            }
        }
    )

    start = coordinator._smart_start_from_registers("m1", now)

    assert start == now - timedelta(hours=3)


def test_start_uses_earliest_zone_register():
    """G12W zones are checked independently, not only import zone 1."""
    now = datetime(2026, 11, 7, 22, 0, tzinfo=TZ)
    coordinator = _coordinator(
        {
            "m1": {
                "import_1": {"sum": 100.0, "start": now - timedelta(hours=1)},
                "import_2": {"sum": 40.0, "start": now - timedelta(hours=2)},
                "export_1": {"sum": 20.0, "start": now - timedelta(hours=6)},
                "export_2": {"sum": 10.0, "start": now - timedelta(hours=5)},
            }
        }
    )

    start = coordinator._smart_start_from_registers("m1", now)

    assert start == now - timedelta(hours=5)


def test_start_is_bounded_to_max_catchup_window():
    """A permanently stale register must not trigger an unbounded refetch."""
    now = datetime(2026, 11, 7, 22, 0, tzinfo=TZ)
    coordinator = _coordinator(
        {
            "m1": {
                "import": {"sum": 100.0, "start": now - timedelta(hours=1)},
                "export": {"sum": 0.0, "start": now - timedelta(days=10)},
            }
        }
    )

    start = coordinator._smart_start_from_registers("m1", now)

    assert start == now - timedelta(hours=47)


def test_start_returns_none_without_prefetched_registers():
    now = datetime(2026, 11, 7, 22, 0, tzinfo=TZ)

    assert _coordinator({})._smart_start_from_registers("m1", now) is None
    assert _coordinator({"m1": {}})._smart_start_from_registers("m1", now) is None
