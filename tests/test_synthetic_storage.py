"""Unit tests for synthetic storage calculation engine."""

from datetime import datetime, timedelta, timezone
import pytest

from custom_components.energa_mobile.synthetic_storage import calculate_synthetic_storage


def test_empty_records():
    res = calculate_synthetic_storage([], coeff=0.8, has_zones=False, initial_bank_1=100.0)
    assert res["series"] == {}
    assert res["ending_bank"] == 100.0


def test_single_zone_calculation():
    now = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    records = [
        # Hour 1: 10 kWh export, 2 kWh import -> charge 8, fee 2, discharge 2, net_import 0, bank=6
        {"dt": now, "import": 2.0, "export": 10.0},
        # Hour 2: 0 kWh export, 8 kWh import -> charge 0, fee 0, discharge 6, net_import 2, bank=0
        {"dt": now + timedelta(hours=1), "import": 8.0, "export": 0.0},
    ]

    res = calculate_synthetic_storage(records, coeff=0.8, has_zones=False, initial_bank_1=0.0)
    series = res["series"]

    # Hour 1 checks
    assert series["magazyn_ladowanie"][0]["state"] == 8.0
    assert series["siec_oddanie"][0]["state"] == 2.0
    assert series["magazyn_rozladowanie"][0]["state"] == 2.0
    assert series["siec_pobor"][0]["state"] == 0.0

    # Hour 2 checks
    assert series["magazyn_ladowanie"][1]["state"] == 0.0
    assert series["siec_oddanie"][1]["state"] == 0.0
    assert series["magazyn_rozladowanie"][1]["state"] == 6.0
    assert series["siec_pobor"][1]["state"] == 2.0

    # Cumulative sums
    assert series["magazyn_ladowanie"][1]["sum"] == 8.0
    assert series["siec_oddanie"][1]["sum"] == 2.0
    assert series["magazyn_rozladowanie"][1]["sum"] == 8.0
    assert series["siec_pobor"][1]["sum"] == 2.0

    assert res["ending_bank"] == 0.0


def test_multi_zone_calculation_and_cross_zone_coverage():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    records = [
        # Hour 1 (Day/L1): Export L1=10 kWh -> Charge L1=8, Fee=2. Import L1=10 kWh -> Dis L1 from L1 = 8, Dis L1 from L2 = 0, NetImp L1 = 2
        {
            "dt": now,
            "import_1": 10.0,
            "import_2": 0.0,
            "export_1": 10.0,
            "export_2": 0.0,
        },
        # Hour 2 (Night/L2): Export L2=5 kWh -> Charge L2=4, Fee=1. Import L2=6 kWh -> Dis L2 from L2 = 4, Dis L2 from L1 = 0, NetImp L2 = 2
        {
            "dt": now + timedelta(hours=1),
            "import_1": 0.0,
            "import_2": 6.0,
            "export_1": 0.0,
            "export_2": 5.0,
        },
        # Hour 3 (Night/L2 with cross-zone): Initial bank L1=5, L2=0. Import L2=3 -> Dis from L2=0, Dis from L1=3, NetImp L2=0.
        {
            "dt": now + timedelta(hours=2),
            "import_1": 0.0,
            "import_2": 3.0,
            "export_1": 0.0,
            "export_2": 0.0,
        },
    ]

    res = calculate_synthetic_storage(
        records,
        coeff=0.8,
        has_zones=True,
        initial_bank_1=5.0, # Starting with 5 in L1
        initial_bank_2=0.0,
    )
    series = res["series"]

    # Hour 1:
    # Starting bank1=5, charge1=8 -> bank1=13. imp1=10 -> dis1_from_1=10 -> rem bank1=3.
    assert series["magazyn_l1_ladowanie"][0]["state"] == 8.0
    assert series["magazyn_l1_rozladowanie"][0]["state"] == 10.0
    assert series["siec_pobor_strefa_1"][0]["state"] == 0.0

    # Hour 2:
    # Bank1=3, Bank2=0. charge2=4 -> bank2=4. imp2=6 -> dis2_from_2=4, dis2_from_1=2 (cross-zone!). NetImp2=0. Bank1=1, Bank2=0.
    assert series["magazyn_l2_ladowanie"][1]["state"] == 4.0
    assert series["magazyn_l2_rozladowanie"][1]["state"] == 4.0
    assert series["magazyn_l1_rozladowanie"][1]["state"] == 2.0
    assert series["siec_pobor_strefa_2"][1]["state"] == 0.0

    # Hour 3:
    # Bank1=1, Bank2=0. imp2=3 -> dis2_from_2=0, dis2_from_1=1 (cross-zone). NetImp2 = 2. Bank1=0, Bank2=0.
    assert series["magazyn_l1_rozladowanie"][2]["state"] == 1.0
    assert series["siec_pobor_strefa_2"][2]["state"] == 2.0

    assert res["ending_bank_1"] == 0.0
    assert res["ending_bank_2"] == 0.0
    assert res["ending_bank"] == 0.0


def test_invariants_hold_exactly():
    """Verify that (Charge + Fee == Export) and (Discharge + NetImport == Import) for every hour."""
    now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    records = []
    for h in range(24):
        records.append({
            "dt": now + timedelta(hours=h),
            "import_1": 1.5 + (h % 3),
            "import_2": 0.8 + (h % 2),
            "export_1": 3.0 if 8 <= h <= 17 else 0.0,
            "export_2": 0.5 if h > 18 else 0.0,
        })

    res = calculate_synthetic_storage(records, coeff=0.8, has_zones=True, initial_bank_1=10.0, initial_bank_2=15.0)
    s = res["series"]

    for i in range(24):
        rec = records[i]
        exp_tot = rec["export_1"] + rec["export_2"]
        imp_tot = rec["import_1"] + rec["import_2"]

        ch_tot = s["magazyn_l1_ladowanie"][i]["state"] + s["magazyn_l2_ladowanie"][i]["state"]
        fee_tot = s["siec_oddanie_strefa_1"][i]["state"] + s["siec_oddanie_strefa_2"][i]["state"]

        dis_tot = s["magazyn_l1_rozladowanie"][i]["state"] + s["magazyn_l2_rozladowanie"][i]["state"]
        net_imp_tot = s["siec_pobor_strefa_1"][i]["state"] + s["siec_pobor_strefa_2"][i]["state"]

        assert abs((ch_tot + fee_tot) - exp_tot) < 0.0001, f"Export balance mismatch at hour {i}"
        assert abs((dis_tot + net_imp_tot) - imp_tot) < 0.0001, f"Import balance mismatch at hour {i}"
