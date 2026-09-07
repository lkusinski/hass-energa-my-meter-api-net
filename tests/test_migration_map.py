"""Tests for MigrationMap and meter replacement continuity.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 9 & 12 (Kryteria akceptacji: Meter replacement).
Warunek testu: Dwa seriale jednego PPE daja ciaglosc per register/zone, bez skoku i bez utraty historii.
"""

from decimal import Decimal
import pytest

from custom_components.energa_mobile.ha.migration_map import MigrationMap


def test_migration_map_ppe_and_serials_registration():
    """Verify registration of multiple meter serials under a single logical PPE."""
    m_map = MigrationMap()
    m_map.register_meter("PL_001", "SERIAL_A")
    m_map.register_meter("PL_001", "SERIAL_B")

    assert m_map.get_ppe_for_serial("SERIAL_A") == "PL_001"
    assert m_map.get_ppe_for_serial("SERIAL_B") == "PL_001"
    assert m_map.get_serials_for_ppe("PL_001") == ["SERIAL_A", "SERIAL_B"]


def test_migration_map_id_generators():
    """Verify stable canonical PPE statistic ID and legacy entity ID generation."""
    m_map = MigrationMap()
    canonical_id = m_map.get_canonical_statistic_id("PL_48001:123/A", "grid_import", "total")
    assert canonical_id == "energa_mobile:PL_48001_123_A__grid_import_total"

    legacy_id = m_map.get_legacy_statistic_id("00112233", "import")
    assert legacy_id == "sensor.energa_00112233_import"


def test_meter_replacement_continuity():
    """Kryterium akceptacji: Meter replacement.

    Old meter (SERIAL_OLD) reached final cumulative reading of 12 500.00 kWh.
    New meter (SERIAL_NEW) starts from 0.00 kWh.
    Verify seamless cumulative curve across meter replacement boundary.
    """
    m_map = MigrationMap()
    ppe = "PL_WARZYWNA_13"
    old_serial = "OLD_METER_1"
    new_serial = "NEW_METER_2"
    register = "import"

    old_final_sum = Decimal("12500.00")
    new_initial_reading = Decimal("0.00")

    # Register replacement
    offset = m_map.register_meter_replacement(
        ppe_id=ppe,
        old_serial=old_serial,
        new_serial=new_serial,
        register=register,
        old_meter_final_sum=old_final_sum,
        new_meter_initial_reading=new_initial_reading,
    )

    assert offset == Decimal("12500.00")

    # Day 1 of new meter: physical meter displays 14.50 kWh
    new_meter_reading_day1 = Decimal("14.50")
    cont_sum_day1 = m_map.compute_continuous_reading(
        ppe_id=ppe,
        serial=new_serial,
        register=register,
        current_reading=new_meter_reading_day1,
    )
    # Total continuous sum should smoothly advance to 12 514.50 kWh
    assert cont_sum_day1 == Decimal("12514.50")

    # Day 2 of new meter: physical meter displays 32.80 kWh
    new_meter_reading_day2 = Decimal("32.80")
    cont_sum_day2 = m_map.compute_continuous_reading(
        ppe_id=ppe,
        serial=new_serial,
        register=register,
        current_reading=new_meter_reading_day2,
    )
    assert cont_sum_day2 == Decimal("12532.80")


def test_meter_replacement_with_non_zero_initial_reading():
    """Verify offset calculation when the replacement meter has initial factory testing reading."""
    m_map = MigrationMap()
    ppe = "PL_WISNIOWA_4"
    old_final = Decimal("5420.30")
    new_initial = Decimal("1.20")  # e.g. Factory test 1.20 kWh

    offset = m_map.register_meter_replacement(
        ppe_id=ppe,
        old_serial="OLD_1",
        new_serial="NEW_2",
        register="import_1",
        old_meter_final_sum=old_final,
        new_meter_initial_reading=new_initial,
    )

    # Offset = 5420.30 - 1.20 = 5419.10
    assert offset == Decimal("5419.10")

    # At initial reading 1.20 kWh, continuous sum must equal old final sum exactly:
    at_start = m_map.compute_continuous_reading(ppe, "NEW_2", "import_1", new_initial)
    assert at_start == Decimal("5420.30")
