"""Tests for the canonical identity shared by the historical import and live updater.

Bug 1 (v1.9.3-beta.1): the 730-day history import archived interval readings as
``ppe_id=<meter_point_id>`` / ``meter_id=<serial>`` while the live data updater
used ``ppe_id=PPE_<meter_point_id>`` / ``meter_id=<meter_point_id>``. The same
physical meter therefore ended up with two identities in ``interval_reading``;
a reader matching across PPE-prefix/meter_id variants could see both rows for
the same hour (double count) or miss the live series when filtering on the
legacy identity. These tests pin the shared helper and the deduplicating read.
"""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.energa_mobile.core.identity import (
    canonical_meter_id,
    canonical_ppe_id,
)
from custom_components.energa_mobile.core.readings.models import IntervalReading
from custom_components.energa_mobile.data_updater import EnergaDataUpdater
from custom_components.energa_mobile.storage.sqlite.database import CanonicalStorage


@pytest.fixture
def utc_noop():
    """The test env mocks homeassistant.util.dt, so as_utc must be a no-op."""
    with patch(
        "custom_components.energa_mobile.data_updater.dt_util.as_utc",
        side_effect=lambda d: d,
    ):
        yield


class TestCanonicalIdentityHelper:
    def test_explicit_entry_ppe_wins(self):
        assert (
            canonical_ppe_id({"ppe_id": "590243891022973835"}, {"meter_point_id": 372197})
            == "590243891022973835"
        )

    def test_synthetic_ppe_matches_live_writer(self):
        assert canonical_ppe_id({}, {"meter_point_id": 372197}) == "PPE_372197"

    def test_meter_id_is_meter_point_id(self):
        assert canonical_meter_id({"meter_point_id": 372197}, fallback="x") == "372197"
        assert canonical_meter_id({}, fallback="11685328") == "11685328"

    def test_empty_inputs_are_safe(self):
        assert canonical_ppe_id(None, None) == ""
        assert canonical_meter_id(None) == ""


def test_live_updater_persists_canonical_identity(utc_noop):
    """Live persistence must not create a second identity for the same meter."""
    storage = CanonicalStorage(":memory:")
    entry = SimpleNamespace(data={}, options={})
    updater = EnergaDataUpdater(MagicMock(), entry, storage=storage)

    t = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
    updater._persist_canonical_readings(
        "372197", "import_1", [{"dt": t, "value": 1.234}]
    )

    rows = storage.get_readings(ppe_id="PPE_372197", resolution="1h")
    assert len(rows) == 1
    assert rows[0].ppe_id == "PPE_372197"
    assert rows[0].meter_id == "372197"
    assert rows[0].import_kwh == Decimal("1.234")

    # The historical import helper produces the exact same identity.
    assert canonical_ppe_id(entry.data, {"meter_point_id": "372197"}) == rows[0].ppe_id
    assert canonical_meter_id({"meter_point_id": "372197"}) == rows[0].meter_id


def test_export_register_archived_as_export(utc_noop):
    storage = CanonicalStorage(":memory:")
    entry = SimpleNamespace(data={}, options={})
    updater = EnergaDataUpdater(MagicMock(), entry, storage=storage)

    t = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
    updater._persist_canonical_readings("372197", "export_2", [{"dt": t, "value": 0.5}])

    rows = storage.get_readings(ppe_id="PPE_372197", register="export_2", resolution="1h")
    assert len(rows) == 1
    assert rows[0].export_kwh == Decimal("0.5")
    assert rows[0].import_kwh == Decimal("0.0")


def test_count_and_latest_match_identity_variants():
    """Diagnostics querying the real PPE must still see the synthetic-PPE rows."""
    storage = CanonicalStorage(":memory:")
    t = datetime(2026, 9, 15, 10, 0)
    storage.insert_readings_idempotent(
        [
            IntervalReading(
                ppe_id="PPE_372197",
                meter_id="372197",
                register="import_1",
                interval_start_utc=t,
                resolution="1h",
                import_kwh=Decimal("1.000"),
            )
        ]
    )
    real_ppe = "590243891022973835"
    assert storage.get_readings_count(real_ppe, meter_id="372197") == 1
    assert storage.get_readings_count("PPE_372197") == 1
    assert storage.get_readings_count() == 1
    assert storage.get_latest_reading_time(real_ppe, meter_id="372197") == t


def test_get_readings_dedupes_dual_identity_within_one_hour():
    """Legacy raw identity + live canonical identity at the same hour collapse."""
    storage = CanonicalStorage(":memory:")
    t = datetime(2026, 9, 15, 10, 0, 0)

    legacy = IntervalReading(
        ppe_id="372197",
        meter_id="11685328",
        register="import_1",
        interval_start_utc=t,
        resolution="1h",
        import_kwh=Decimal("1.000"),
        source="energa",
    )
    live = IntervalReading(
        ppe_id="PPE_372197",
        meter_id="372197",
        register="import_1",
        interval_start_utc=t,
        resolution="1h",
        import_kwh=Decimal("1.000"),
        source="energa",
    )
    storage.insert_readings_idempotent([legacy, live])

    # Reader asking for the canonical identity gets exactly one row, canonical.
    canonical = storage.get_readings(ppe_id="PPE_372197", resolution="1h")
    assert len(canonical) == 1
    assert canonical[0].ppe_id == "PPE_372197"

    # A reader asking for the legacy raw identity still gets one row, no dupes.
    raw = storage.get_readings(ppe_id="372197", resolution="1h")
    assert len(raw) == 1
    assert raw[0].ppe_id == "372197"

    # Cross-identity match via meter_id must not double count either.
    mixed = storage.get_readings(
        ppe_id="PPE_372197", meter_id="11685328", resolution="1h"
    )
    assert len(mixed) == 1
