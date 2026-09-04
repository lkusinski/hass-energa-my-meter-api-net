"""Tests for v0.2.14 tariff math (hidden fees), v0.3.0 G11 table.

 Fee tables: G12W_DEFAULT_FEES, G11_DEFAULT_FEES (exact values from G11
 consumer invoice (G11, 02-04.2026, 2159 kWh, no PV):
 sale 1352.37 + distribution 919.37 = netto 2271.74 -> brutto 2794.24).
 Reference numbers come from real invoices:
 - G12W-nowe 07.2026: sale 195.06 (100.15+0.20+94.56+0.15), distribution
   148.45, netto 343.51, brutto 422.52, deposit 147.44, payable 275.16.
 - G12W-stare 05-06.2026 (2 months, fully covered by the warehouse):
   sale 34.18 (0+0.55+0+1.27 + handlowa 2x16.18), distribution 92.92
   (variable + quality rows 0.00 when covered), netto 127.10,
   brutto 156.33.
 - v0.3.0: excise (5 PLN/MWh) is INFORMATIONAL — already inside the
   energy price (G11 invoice matches to the grosz only without adding
   it). compute_bill still reports it under "excise" but excludes it
   from sale_total/netto.
"""

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "tariff_under_test",
    "custom_components/energa_mobile/tariff.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
compute_bill = _mod.compute_bill
fees_from_options = _mod.fees_from_options
split_cover = _mod.split_cover
G12W_DEFAULT_FEES = _mod.G12W_DEFAULT_FEES
G11_DEFAULT_FEES = _mod.G11_DEFAULT_FEES
tariff_family = _mod.tariff_family
capacity_for_annual_use = _mod.capacity_for_annual_use


class TestNewSystemJuly:
    def test_matches_invoice_within_one_percent(self):
        # Hourly-netted sums as the statistics-based sensor sees them.
        res = compute_bill(
            import_day=164.0, import_night=237.0, export_kwh=456.0,
            rcem=0.26288,
        )
        assert res["sale_energy_day"] == 100.15
        assert res["sale_energy_night"] == 94.56
        assert res["distr_var_day"] == 65.88
        assert res["distr_var_night"] == 20.17
        assert res["distr_quality"] == 13.31
        assert res["distr_oze"] == 2.93
        assert res["distr_cogen"] == 1.2
        assert res["deposit"] == 147.44
        assert abs(res["netto"] - 343.51) / 343.51 < 0.01
        assert abs(res["brutto"] - 422.52) / 422.52 < 0.01
        assert abs(res["do_zaplaty"] - 275.16) / 275.16 < 0.01

    def test_deposit_capped_at_brutto(self):
        res = compute_bill(
            import_day=1.0, import_night=0.0, export_kwh=10000.0,
            rcem=0.26288,
        )
        assert res["do_zaplaty"] == 0.0
        assert res["deposit_applied"] == res["brutto"]


class TestOldSystemCovered:
    def test_fully_covered_two_months(self):
        res = compute_bill(
            import_day=77.0, import_night=222.0, export_kwh=0.0,
            rcem=0.0, months=2,
            fees={"trade_fee": 16.18, "abonament": 0.70},
            cover_day=77.0, cover_night=222.0,
        )
        assert res["sale_energy_day"] == 0.0
        assert res["sale_energy_night"] == 0.0
        assert res["distr_var_day"] == 0.0
        assert res["distr_var_night"] == 0.0
        assert res["distr_quality"] == 0.0
        # Excise + OZE/cogen reported on the full import (excise is
        # informational only, excluded from the total since v0.3.0):
        assert res["excise"] == 1.5
        assert res["distr_oze"] == 2.18
        assert res["distr_cogen"] == 0.9
        assert res["trade_fee"] == 32.36
        # Exact model values (no excise in total):
        assert res["sale_total"] == 32.36
        assert res["netto"] == 125.28
        assert res["brutto"] == 154.09
        # Invoice (127.10 / 156.33) had a small UNCOVERED remainder
        # (energy lines 0.55 + 1.27 = 1.82); our fully-covered
        # reconstruction matches it within 2%.
        assert abs(res["netto"] - 127.10) / 127.10 < 0.02
        assert abs(res["brutto"] - 156.33) / 156.33 < 0.02
        assert abs(res["do_zaplaty"] - 156.33) / 156.33 < 0.02

    def test_partial_cover_pays_rest(self):
        res = compute_bill(
            import_day=100.0, import_night=0.0, export_kwh=0.0,
            rcem=0.0, cover_day=40.0,
        )
        assert res["sale_energy_day"] == round(60.0 * 0.6107, 2)
        # Distribution variable follows the uncovered part:
        assert res["distr_var_day"] == round(60.0 * 0.4017, 2)


class TestDefensive:
    def test_negative_flows_clamped(self):
        res = compute_bill(
            import_day=-5.0, import_night=-1.0, export_kwh=-3.0, rcem=0.5
        )
        assert res["do_zaplaty"] >= 0.0
        assert res["sale_energy_day"] == 0.0


class TestFeesFromOptions:
    def test_empty_options_give_defaults(self):
        assert fees_from_options({}) == G12W_DEFAULT_FEES
        assert fees_from_options(None) == G12W_DEFAULT_FEES

    def test_override_single_fee(self):
        fees = fees_from_options({"tariff_trade_fee": 16.18})
        assert fees["trade_fee"] == 16.18
        # rest untouched
        assert fees["energy_day"] == G12W_DEFAULT_FEES["energy_day"]

    def test_invalid_value_keeps_default(self):
        fees = fees_from_options({"tariff_capacity": "junk", "tariff_oze": None})
        assert fees["capacity"] == G12W_DEFAULT_FEES["capacity"]
        assert fees["oze"] == G12W_DEFAULT_FEES["oze"]

    def test_unknown_keys_ignored(self):
        fees = fees_from_options({"import_price_1": 9.99})
        assert fees == G12W_DEFAULT_FEES

    def test_const_defaults_parity(self):
        # const.py DEFAULT_TARIFF_* must mirror tariff.G12W_DEFAULT_FEES
        from custom_components.energa_mobile import const as _const

        mapping = {
            "energy_day": _const.DEFAULT_TARIFF_ENERGY_DAY,
            "energy_night": _const.DEFAULT_TARIFF_ENERGY_NIGHT,
            "excise_mwh": _const.DEFAULT_TARIFF_EXCISE_MWH,
            "trade_fee": _const.DEFAULT_TARIFF_TRADE_FEE,
            "abonament": _const.DEFAULT_TARIFF_ABONAMENT,
            "grid_fixed": _const.DEFAULT_TARIFF_GRID_FIXED,
            "grid_var_day": _const.DEFAULT_TARIFF_GRID_VAR_DAY,
            "grid_var_night": _const.DEFAULT_TARIFF_GRID_VAR_NIGHT,
            "quality": _const.DEFAULT_TARIFF_QUALITY,
            "oze": _const.DEFAULT_TARIFF_OZE,
            "cogen": _const.DEFAULT_TARIFF_COGEN,
            "capacity": _const.DEFAULT_TARIFF_CAPACITY,
        }
        assert mapping == G12W_DEFAULT_FEES


class TestSplitCover:
    def test_proportional_split(self):
        day, night = split_cover(100.0, 164.0, 237.0)
        assert day + night == 100.0
        assert day == round(100.0 * 164.0 / 401.0, 2)

    def test_capped_at_import(self):
        day, night = split_cover(9999.0, 77.0, 222.0)
        assert (day, night) == (77.0, 222.0)

    def test_zero_import(self):
        assert split_cover(50.0, 0.0, 0.0) == (0.0, 0.0)
        assert split_cover(0.0, 10.0, 10.0) == (0.0, 0.0)

    def test_single_zone_all_day(self):
        assert split_cover(40.0, 100.0, 0.0) == (40.0, 0.0)

    def test_defensive_inputs(self):
        assert split_cover(-5.0, 10.0, 10.0) == (0.0, 0.0)
        assert split_cover("junk", 10.0, 10.0) == (0.0, 0.0)


class TestBillSensorWiring:
    """End-to-end math as EnergaBillForecastSensor.native_value uses it."""

    def test_new_system_mtd_matches_july_invoice(self):
        fees = fees_from_options({})
        res = compute_bill(164.0, 237.0, 456.0, 0.26288, fees)
        assert abs(res["brutto"] - 422.52) / 422.52 < 0.01
        assert abs(res["do_zaplaty"] - 275.16) / 275.16 < 0.01

    def test_old_system_covered_by_warehouse(self):
        fees = fees_from_options({"tariff_trade_fee": 16.18, "tariff_abonament": 0.70})
        day, night = split_cover(9999.0, 77.0, 222.0)
        res = compute_bill(77.0, 222.0, 0.0, 0.0, fees, months=2,
                           cover_day=day, cover_night=night)
        assert res["sale_energy_day"] == 0.0
        assert res["sale_energy_night"] == 0.0
        # Fully-covered reconstruction vs invoice with a small uncovered
        # remainder (see TestOldSystemCovered): within 2%.
        assert abs(res["brutto"] - 156.33) / 156.33 < 0.02

    def test_old_system_never_applies_deposit(self):
        # Old net-metering with export must NOT get a PLN deposit:
        # coverage only (deposit_pln=0.0, as the sensor passes).
        fees = fees_from_options({})
        day, night = split_cover(500.0, 100.0, 200.0)
        res = compute_bill(100.0, 200.0, 500.0, 0.26288, fees,
                           cover_day=day, cover_night=night,
                           deposit_pln=0.0)
        assert res["deposit"] == 0.0
        assert res["do_zaplaty"] == res["brutto"]

    def test_consumer_full_import_bill(self):
        # G11 consumer (no PV): no export, no cover — pays everything.
        fees = fees_from_options({})
        res = compute_bill(150.0, 0.0, 0.0, 0.26288, fees, deposit_pln=0.0)
        assert res["sale_energy_day"] == round(150.0 * 0.6107, 2)
        assert res["deposit"] == 0.0
        assert res["do_zaplaty"] == res["brutto"]
        assert res["brutto"] > res["netto"]  # VAT included


class TestCapacityBrackets:
    """URE 2026 brackets (Informacja 58/2025, netto PLN/month)."""

    def test_brackets(self):
        assert capacity_for_annual_use(499.9) == 4.29
        assert capacity_for_annual_use(500.0) == 10.31
        assert capacity_for_annual_use(1200.0) == 17.18
        assert capacity_for_annual_use(2800.0) == 24.05
        assert capacity_for_annual_use(20000.0) == 24.05

    def test_defensive_fallback_top_bracket(self):
        assert capacity_for_annual_use(None) == 24.05
        assert capacity_for_annual_use(0) == 24.05
        assert capacity_for_annual_use(-5) == 24.05
        assert capacity_for_annual_use("junk") == 24.05

    def test_bracket_changes_bill(self):
        small = dict(fees_from_options({}), capacity=capacity_for_annual_use(800.0))
        big = dict(fees_from_options({}), capacity=capacity_for_annual_use(5000.0))
        r_small = compute_bill(100.0, 0.0, 0.0, 0.0, small, deposit_pln=0.0)
        r_big = compute_bill(100.0, 0.0, 0.0, 0.0, big, deposit_pln=0.0)
        assert r_small["distr_fixed"] == round(0.74 + 20.17 + 10.31, 2)
        assert r_big["distr_fixed"] == round(0.74 + 20.17 + 24.05, 2)
        assert r_big["do_zaplaty"] > r_small["do_zaplaty"]


class TestG11Invoice:
    """G11 consumer invoice (no PV, v0.3.0).

    Period 02-04.2026 (2 months), 2159 kWh single-zone, meter
    8927.584 -> 11086.221. Every line must match to the grosz.
    """

    def test_exact_lines(self):
        fees = fees_from_options({}, "G11")
        res = compute_bill(2159.0, 0.0, 0.0, 0.0, fees, months=2,
                           deposit_pln=0.0)
        assert res["sale_energy_day"] == 1320.01
        assert res["sale_energy_night"] == 0.0
        assert res["trade_fee"] == 32.36
        assert res["sale_total"] == 1352.37
        assert res["distr_var_day"] == 752.41
        assert res["distr_quality"] == 71.68
        assert res["distr_oze"] == 15.76
        assert res["distr_cogen"] == 6.48
        assert res["distr_fixed"] == 73.04
        assert res["distr_total"] == 919.37
        assert res["netto"] == 2271.74
        assert res["vat"] == 522.50
        assert res["brutto"] == 2794.24
        assert res["deposit"] == 0.0
        assert res["do_zaplaty"] == 2794.24

    def test_excise_informational_only(self):
        # 2159 kWh x 5 PLN/MWh = 10.795 reported (10.79 after binary
        # round-half-even; invoice prints 10.80), NOT added to the total.
        fees = fees_from_options({}, "G11")
        res = compute_bill(2159.0, 0.0, 0.0, 0.0, fees, months=2,
                           deposit_pln=0.0)
        assert res["excise"] == 10.79
        assert res["sale_total"] == round(1320.01 + 32.36, 2)

    def test_capacity_bracket_matches_invoice(self):
        # Invoice annual use 7312 kWh -> top bracket 24.05 (2 x 24.05).
        assert capacity_for_annual_use(7312.0) == 24.05
        fees = fees_from_options({}, "G11")
        res = compute_bill(2159.0, 0.0, 0.0, 0.0, fees, months=2,
                           deposit_pln=0.0)
        assert res["distr_fixed"] == round(2 * (0.70 + 11.77 + 24.05), 2)


class TestTariffFamily:
    def test_g11(self):
        assert tariff_family("G11") == "G11"
        assert tariff_family("g11") == "G11"

    def test_two_zone_falls_back_to_g12w(self):
        for t in ("G12W", "G12", "G12AS", "G12R", "G12w", None, "", "junk", 123):
            assert tariff_family(t) == "G12W"

    def test_fees_default_per_tariff(self):
        assert fees_from_options({}, "G11") == G11_DEFAULT_FEES
        assert fees_from_options({}, "G12W") == G12W_DEFAULT_FEES
        assert fees_from_options({}) == G12W_DEFAULT_FEES
        assert fees_from_options(None, "G11") == G11_DEFAULT_FEES

    def test_override_wins(self):
        fees = fees_from_options({"tariff_trade_fee": 9.99}, "G11")
        assert fees["trade_fee"] == 9.99
        assert fees["energy_day"] == G11_DEFAULT_FEES["energy_day"]

    def test_g11_migration_untouched_g12w_defaults(self):
        # Options form used to bake G12W defaults everywhere: a G11
        # meter with all-G12W tariff_* values gets the G11 table.
        stale = {f"tariff_{k}": v for k, v in {
            "energy_day": 0.6107, "energy_night": 0.3990,
            "excise_mwh": 5.00, "trade_fee": 0.0, "abonament": 0.74,
            "grid_fixed": 20.17, "grid_var_day": 0.4017,
            "grid_var_night": 0.0851, "quality": 0.0332, "oze": 0.0073,
            "cogen": 0.0030, "capacity": 24.05,
        }.items()}
        assert fees_from_options(stale, "G11") == G11_DEFAULT_FEES

    def test_g11_migration_customized_value_respected(self):
        opts = {"tariff_trade_fee": 16.18, "tariff_abonament": 0.70}
        fees = fees_from_options(opts, "G11")
        assert fees["trade_fee"] == 16.18
        assert fees["abonament"] == 0.70
        # untouched fields fall back to the G11 table, not G12W
        assert fees["grid_fixed"] == G11_DEFAULT_FEES["grid_fixed"]
        assert fees["energy_day"] == G11_DEFAULT_FEES["energy_day"]

    def test_g12w_never_migrates(self):
        stale_g11 = {f"tariff_{k}": v for k, v in {
            "energy_day": 0.6114, "energy_night": 0.0,
            "excise_mwh": 5.00, "trade_fee": 16.18, "abonament": 0.70,
            "grid_fixed": 11.77, "grid_var_day": 0.3485,
            "grid_var_night": 0.0, "quality": 0.0332, "oze": 0.0073,
            "cogen": 0.0030, "capacity": 24.05,
        }.items()}
        assert fees_from_options(stale_g11, "G12W")["trade_fee"] == 16.18
