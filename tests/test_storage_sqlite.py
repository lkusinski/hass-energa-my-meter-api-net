"""Unit tests for SQLite WAL Canonical Storage.

Tests:
- Schema migrations & table creation.
- PPE logical identity and meter lifecycle (replacement handling).
- Raw payload observation storage and deduplication.
- Idempotent interval readings (ON CONFLICT DO NOTHING).
- Revision handling and latest-revision query resolution.
- Resumable job checkpoints.
- Zero float precision loss (Decimal assertions).
"""

from datetime import date, datetime
from decimal import Decimal
import tempfile

import pytest

from custom_components.energa_mobile.core.identity.models import (
    PPE,
    MeterLifecycle,
    SettlementType,
)
from custom_components.energa_mobile.core.readings.models import (
    IntervalReading,
    SourceObservation,
)
from custom_components.energa_mobile.storage.sqlite.database import CanonicalStorage


@pytest.fixture
def storage():
    """Create an in-memory CanonicalStorage instance for testing."""
    return CanonicalStorage(":memory:")


@pytest.fixture
def file_storage():
    """Create a temporary file-backed CanonicalStorage instance to test WAL and persistence."""
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        yield CanonicalStorage(tmp.name)


def test_ppe_crud(storage: CanonicalStorage):
    ppe = PPE(
        ppe_id="PL_123456789012345678",
        customer_label="Dom G12w Net-metering",
        settlement_type=SettlementType.NET_METERING,
        prosumer_coefficient=Decimal("0.8"),
        timezone="Europe/Warsaw",
        effective_from=date(2022, 1, 1),
    )
    storage.upsert_ppe(ppe)

    fetched = storage.get_ppe("PL_123456789012345678")
    assert fetched is not None
    assert fetched.ppe_id == "PL_123456789012345678"
    assert fetched.customer_label == "Dom G12w Net-metering"
    assert fetched.settlement_type == SettlementType.NET_METERING
    assert fetched.prosumer_coefficient == Decimal("0.8")
    assert fetched.effective_from == date(2022, 1, 1)

    # Test update (upsert)
    updated_ppe = PPE(
        ppe_id="PL_123456789012345678",
        customer_label="Dom G12w - Zaktualizowano",
        settlement_type=SettlementType.NET_BILLING_RCEM,
        prosumer_coefficient=Decimal("0.0"),
    )
    storage.upsert_ppe(updated_ppe)
    refetched = storage.get_ppe("PL_123456789012345678")
    assert refetched.customer_label == "Dom G12w - Zaktualizowano"
    assert refetched.settlement_type == SettlementType.NET_BILLING_RCEM


def test_meter_replacement_lifecycle(storage: CanonicalStorage):
    """Test that two serials under one PPE record continuous lifecycle."""
    ppe_id = "PL_TEST_PPE_G12W_NB"
    storage.upsert_ppe(PPE(ppe_id=ppe_id, settlement_type=SettlementType.NET_BILLING_RCEM))

    # Old meter (active until 2026-05-09)
    m1 = MeterLifecycle(
        ppe_id=ppe_id,
        meter_id="meter_old_111",
        serial="SER_OLD_111",
        register="1.8.0",
        zone="total",
        valid_from=datetime(2024, 1, 1),
        valid_to=datetime(2026, 5, 9),
        offset_kwh=Decimal("0.0"),
    )
    storage.add_meter_lifecycle(m1)

    # New meter (installed 2026-05-09, carrying offset)
    m2 = MeterLifecycle(
        ppe_id=ppe_id,
        meter_id="meter_new_222",
        serial="SER_NEW_222",
        register="1.8.0",
        zone="total",
        valid_from=datetime(2026, 5, 9),
        valid_to=None,
        offset_kwh=Decimal("5192.350"),
    )
    storage.add_meter_lifecycle(m2)

    history = storage.get_meter_lifecycles(ppe_id)
    assert len(history) == 2
    assert history[0].serial == "SER_OLD_111"
    assert history[1].serial == "SER_NEW_222"
    assert history[1].offset_kwh == Decimal("5192.350")


def test_raw_observation_deduplication(storage: CanonicalStorage):
    payload = '{"measurements": [{"dt": "2026-09-04 12:00", "val": 1.23}]}'
    obs1 = SourceObservation.create("energa", "/dp/chart", 200, payload)
    inserted1 = storage.save_observation(obs1)
    assert inserted1 is True
    assert storage.has_observation_hash(obs1.payload_hash) is True

    # Same payload again
    obs2 = SourceObservation.create("energa", "/dp/chart", 200, payload)
    # The observation_id is deterministic or unique, but payload hash exists:
    assert storage.has_observation_hash(obs2.payload_hash) is True


def test_idempotent_readings_insert(storage: CanonicalStorage):
    ppe_id = "PL_TEST_001"
    storage.upsert_ppe(PPE(ppe_id=ppe_id))

    t1 = datetime(2026, 9, 4, 12, 0, 0)
    t2 = datetime(2026, 9, 4, 13, 0, 0)

    r1 = IntervalReading(
        ppe_id=ppe_id,
        meter_id="m1",
        register="1.8.1",
        interval_start_utc=t1,
        resolution="1h",
        import_kwh=Decimal("0.852"),
        export_kwh=Decimal("0.000"),
    )
    r2 = IntervalReading(
        ppe_id=ppe_id,
        meter_id="m1",
        register="1.8.1",
        interval_start_utc=t2,
        resolution="1h",
        import_kwh=Decimal("1.104"),
        export_kwh=Decimal("0.000"),
    )

    # First insert
    count1 = storage.insert_readings_idempotent([r1, r2])
    assert count1 == 2

    # Second insert with exact same readings: should be NO-OP (0 inserted)
    count2 = storage.insert_readings_idempotent([r1, r2])
    assert count2 == 0

    # Query readings
    readings = storage.get_readings(ppe_id, register="1.8.1")
    assert len(readings) == 2
    assert readings[0].import_kwh == Decimal("0.852")
    assert readings[1].import_kwh == Decimal("1.104")


def test_reading_revisions(storage: CanonicalStorage):
    """Test that higher revisions supersede earlier ones without deleting history."""
    ppe_id = "PL_REV_001"
    storage.upsert_ppe(PPE(ppe_id=ppe_id))

    t = datetime(2026, 9, 4, 10, 0, 0)

    # Initial revision 1
    r_v1 = IntervalReading(
        ppe_id=ppe_id,
        meter_id="m1",
        register="1.8.0",
        interval_start_utc=t,
        resolution="1h",
        import_kwh=Decimal("1.000"),
        revision=1,
    )
    storage.insert_readings_idempotent([r_v1])

    # Verified initial value
    results = storage.get_readings(ppe_id, register="1.8.0")
    assert len(results) == 1
    assert results[0].import_kwh == Decimal("1.000")
    assert results[0].revision == 1

    # Late correction revision 2 (e.g. OSD adjustment)
    r_v2 = IntervalReading(
        ppe_id=ppe_id,
        meter_id="m1",
        register="1.8.0",
        interval_start_utc=t,
        resolution="1h",
        import_kwh=Decimal("1.250"),
        revision=2,
    )
    storage.insert_readings_idempotent([r_v2])

    # Query returns revision 2
    results_after = storage.get_readings(ppe_id, register="1.8.0")
    assert len(results_after) == 1
    assert results_after[0].import_kwh == Decimal("1.250")
    assert results_after[0].revision == 2


def test_job_checkpoint(storage: CanonicalStorage):
    storage.save_checkpoint(
        job_name="backfill_730d",
        ppe_id="PL_001",
        cursor="2025-01-01",
        status="in_progress",
    )
    cp = storage.get_checkpoint("backfill_730d")
    assert cp is not None
    assert cp["cursor"] == "2025-01-01"
    assert cp["status"] == "in_progress"

    # Resume & complete
    storage.save_checkpoint(
        job_name="backfill_730d",
        ppe_id="PL_001",
        cursor="2026-09-04",
        status="completed",
    )
    cp_done = storage.get_checkpoint("backfill_730d")
    assert cp_done["cursor"] == "2026-09-04"
    assert cp_done["status"] == "completed"
    assert cp_done["last_success_utc"] is not None


def test_schema_v1_to_v2_migration():
    """Verify that an existing Schema V1 database safely migrates to Schema V2 without data loss."""
    import sqlite3
    from custom_components.energa_mobile.storage.sqlite.database import SCHEMA_V1_SQL

    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        # Create explicit V1 database
        conn = sqlite3.connect(tmp.name)
        conn.executescript(SCHEMA_V1_SQL)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (1, '2026-09-01T00:00:00');"
        )
        conn.execute(
            "INSERT INTO ppe (ppe_id, settlement_type, prosumer_coefficient, timezone) "
            "VALUES ('PL_MIGRATE_TEST', 'net_billing_rcem', '0.0', 'Europe/Warsaw');"
        )
        conn.commit()
        conn.close()

        # Open with CanonicalStorage (triggers V1 -> V2 migration)
        storage = CanonicalStorage(tmp.name)

        ppe = storage.get_ppe("PL_MIGRATE_TEST")
        assert ppe is not None
        assert ppe.ppe_id == "PL_MIGRATE_TEST"

        # Verify Schema version is now 2
        with storage._connection() as c:
            cur = c.execute("SELECT MAX(version) FROM schema_version;")
            assert cur.fetchone()[0] == 2

            # Verify V2 tables exist
            cur2 = c.execute("SELECT count(*) FROM market_price;")
            assert cur2.fetchone()[0] == 0


def test_market_prices_crud_and_effective_lookup(storage: CanonicalStorage):
    from custom_components.energa_mobile.adapters.pse.models import MarketPriceRecord

    # Revision 1 published on 11.08.2026
    p_v1 = MarketPriceRecord(
        price_type="RCEM",
        applicable_year=2026,
        applicable_month=7,
        publication_date=date(2026, 8, 11),
        revision=1,
        price_mwh=Decimal("262.88"),
        price_kwh=Decimal("0.26288"),
    )
    # Late correction revision 2 published on 25.08.2026
    p_v2 = MarketPriceRecord(
        price_type="RCEM",
        applicable_year=2026,
        applicable_month=7,
        publication_date=date(2026, 8, 25),
        revision=2,
        price_mwh=Decimal("265.10"),
        price_kwh=Decimal("0.26510"),
        is_correction=True,
    )

    storage.save_market_prices([p_v1, p_v2])

    # Fetch all prices for 2026-07
    all_prices = storage.get_market_prices("RCEM", year=2026, month=7)
    assert len(all_prices) == 2

    # As of 15.08.2026 (before correction) -> should return v1
    eff_early = storage.get_effective_market_price("RCEM", 2026, 7, as_of=date(2026, 8, 15))
    assert eff_early is not None
    assert eff_early.revision == 1
    assert eff_early.price_kwh == Decimal("0.26288")

    # As of 30.08.2026 (after correction) -> should return v2
    eff_late = storage.get_effective_market_price("RCEM", 2026, 7, as_of=date(2026, 8, 30))
    assert eff_late is not None
    assert eff_late.revision == 2
    assert eff_late.price_kwh == Decimal("0.26510")


def test_settlement_lots_and_allocations(storage: CanonicalStorage):
    from custom_components.energa_mobile.core.settlement.models import LotAllocation, SettlementLot

    ppe_id = "PL_SETTLEMENT_TEST"
    storage.upsert_ppe(PPE(ppe_id=ppe_id, settlement_type=SettlementType.NET_BILLING_RCEM))

    lot = SettlementLot(
        lot_id="lot_pln_2026_06",
        ppe_id=ppe_id,
        unit="PLN",
        zone="total",
        original_amount=Decimal("150.00"),
        remaining_amount=Decimal("150.00"),
        created_at_utc=datetime(2026, 7, 1, 0, 0),
        assigned_at=date(2026, 7, 1),
        expires_at=date(2027, 7, 31),
    )
    storage.save_settlement_lots([lot])

    lots = storage.get_settlement_lots(ppe_id, unit="PLN", active_only=True)
    assert len(lots) == 1
    assert lots[0].remaining_amount == Decimal("150.00")

    # Record an allocation consuming 50 PLN
    alloc = LotAllocation(
        allocation_id="alloc_1",
        lot_id="lot_pln_2026_06",
        consumption_target_id="inv_2026_07_energy",
        allocated_amount=Decimal("50.00"),
        allocated_at_utc=datetime(2026, 8, 1, 10, 0),
    )
    storage.save_lot_allocations([alloc])

    # Update lot remaining amount
    lot.remaining_amount = Decimal("100.00")
    storage.save_settlement_lots([lot])

    lots_updated = storage.get_settlement_lots(ppe_id, unit="PLN")
    assert lots_updated[0].remaining_amount == Decimal("100.00")

    allocs = storage.get_lot_allocations("lot_pln_2026_06")
    assert len(allocs) == 1
    assert allocs[0].allocated_amount == Decimal("50.00")


def test_invoice_reconciliation_storage_and_approval(storage: CanonicalStorage):
    from custom_components.energa_mobile.core.tariffs.models import InvoiceReconciliation

    recon = InvoiceReconciliation(
        invoice_number="1200222768/FES/00017",
        period_start=date(2026, 2, 4),
        period_end=date(2026, 4, 5),
        computed_gross=Decimal("2794.24"),
        invoiced_gross=Decimal("2794.24"),
        variance_gross=Decimal("0.00"),
        variance_percent=Decimal("0.00"),
        line_variances=[
            {
                "rate_id": "energy_day",
                "name": "Energia czynna całodobowa",
                "computed_gross": Decimal("1623.61"),
                "invoiced_gross": Decimal("1623.61"),
                "diff_gross": Decimal("0.00"),
            },
            {
                "rate_id": "grid_var_day",
                "name": "Składnik zmienny",
                "computed_gross": Decimal("925.46"),
                "invoiced_gross": Decimal("925.46"),
                "diff_gross": Decimal("0.00"),
            },
        ],
        status="MATCH",
        notes="Faktura sprawdzona",
    )

    storage.save_invoice_reconciliation(recon, ppe_id="590243891043576725")

    saved = storage.get_invoice_reconciliation("1200222768/FES/00017")
    assert saved is not None
    assert saved.invoice_number == "1200222768/FES/00017"
    assert saved.status == "MATCH"
    assert len(saved.line_variances) == 2
    assert saved.variance_gross == Decimal("0.00")

    # Test user approval workflow
    ok = storage.set_reconciliation_approval("1200222768/FES/00017", approved=True, approved_by="admin")
    assert ok is True

