"""Pure tests for core.verification.build_period_invoice (Faza 1).

Reference numbers come from the five real invoices reproduced in
tests/test_tariff.py; here we additionally feed hourly recorder-shaped
series through the full salda -> invoice pipeline and assert the same
results to the grosz.
"""

from custom_components.energa_mobile.core.verification import (
    build_period_invoice,
    has_two_zones,
)
from custom_components.energa_mobile.tariff import (
    G12W_DEFAULT_FEES,
    fees_from_options,
)


def _zone_maps(plus: float, overlap: float, minus: float):
    """Two-hour hourly maps yielding saldo_plus/overlap/saldo_minus exactly.

    Hour 0 is a positive-net hour (import = ``plus``); hour 1 is a
    negative-net hour (import = ``overlap`` so it lands in the excise base,
    export = ``overlap + minus`` so the net is ``-minus``).
    """
    imp = {0: float(plus)}
    exp: dict[int, float] = {}
    if overlap or minus:
        imp[3600] = float(overlap)
        exp[3600] = float(overlap + minus)
    return imp, exp


def _hourly(zone1, zone2=None):
    imp1, exp1 = _zone_maps(*zone1)
    hourly = {"import_1": imp1, "export_1": exp1}
    if zone2 is not None:
        imp2, exp2 = _zone_maps(*zone2)
        hourly["import_2"] = imp2
        hourly["export_2"] = exp2
    return hourly


class TestAgrestowaNetBilling:
    """Net-billing G12W (Agrestowa 07 and 08.2026)."""

    def test_august_2026_matches_invoice(self):
        # plus L1/L2 398/309, overlap L1/L2 38/23, export 238+197=435.
        hourly = _hourly((398, 38, 238), (309, 23, 197))
        assert has_two_zones(hourly) is True
        res = build_period_invoice(
            hourly,
            fees=G12W_DEFAULT_FEES,
            rcem=0.29453,
            months=1,
            old_system=False,
        )
        assert res["kwh"]["saldo_plus_1"] == 398.0
        assert res["kwh"]["saldo_plus_2"] == 309.0
        assert res["kwh"]["overlap_1"] == 38.0
        assert res["kwh"]["overlap_2"] == 23.0
        assert res["kwh"]["saldo_minus_1"] == 238.0
        assert res["kwh"]["saldo_minus_2"] == 197.0
        assert res["sale_energy_day"] == 243.06
        assert res["sale_energy_night"] == 123.29
        assert res["excise_day"] == 0.19
        assert res["excise_night"] == 0.12
        assert res["netto"] == 628.55
        assert res["vat"] == 144.57
        assert res["brutto"] == 773.12
        assert res["deposit"] == 157.59
        assert res["deposit_applied"] == 157.59
        assert res["do_zaplaty"] == 615.53
        assert res["old_system"] is False
        assert res["months"] == 1
        assert res["rcem"] == 0.29453

    def test_july_2026_matches_invoice(self):
        # plus 164/237, overlap 40/29, export 300+156=456.
        hourly = _hourly((164, 40, 300), (237, 29, 156))
        res = build_period_invoice(
            hourly,
            fees=G12W_DEFAULT_FEES,
            rcem=0.26288,
            months=1,
            old_system=False,
        )
        assert res["sale_energy_day"] == 100.15
        assert res["sale_energy_night"] == 94.56
        assert res["netto"] == 343.51
        assert res["brutto"] == 422.52
        assert res["deposit"] == 147.44
        # Invoice prints 275.16 = 275.08 + 0.08 late-payment interest.
        assert res["do_zaplaty"] == 275.08

    def test_every_value_is_json_serialisable(self):
        import json

        hourly = _hourly((398, 38, 238), (309, 23, 197))
        res = build_period_invoice(
            hourly, fees=G12W_DEFAULT_FEES, rcem=0.29453, old_system=False
        )
        json.dumps(res)  # must not raise


class TestBursztynowaG11NetBilling:
    """Real G11 prosumer invoice FES/00027 (Bursztynowa)."""

    FEES = {
        "energy_day": 0.605286,
        "energy_night": 0.0,
        "excise_mwh": 5.0,
        "trade_fee": 20.32,
        "abonament": 0.74,
        "grid_fixed": 11.77,
        "grid_var_day": 0.3485,
        "grid_var_night": 0.0,
        "quality": 0.0332,
        "oze": 0.0073,
        "cogen": 0.0030,
        "capacity": 24.05,
    }

    def test_matches_to_the_grosz(self):
        # plus 30, overlap/nakładka 149, saldo ujemne 192.
        hourly = _hourly((30, 149, 192))
        assert has_two_zones(hourly) is False
        res = build_period_invoice(
            hourly,
            fees=self.FEES,
            rcem=0.1988,
            months=1,
            old_system=False,
            deposit_open_pln=129.82,
        )
        assert res["kwh"]["saldo_plus_1"] == 30.0
        assert res["kwh"]["overlap_1"] == 149.0
        assert res["sale_energy_day"] == 18.16
        assert res["excise_day"] == 0.75
        assert res["netto"] == 87.56
        assert res["brutto"] == 107.70
        assert res["deposit_applied"] == 23.26
        assert res["do_zaplaty"] == 84.44


class TestWisniowaNetMetering:
    """Real net-metering invoice FES/00042 (Wiśniowa) — old system."""

    FEES = {
        "energy_day": 0.7125,
        "energy_night": 0.4622,
        "excise_mwh": 5.0,
        "trade_fee": 16.18,
        "abonament": 0.70,
        "grid_fixed": 20.17,
        "grid_var_day": 0.4017,
        "grid_var_night": 0.0851,
        "quality": 0.0332,
        "oze": 0.0073,
        "cogen": 0.0030,
        "capacity": 24.05,
    }

    def test_warehouse_cover_and_gross_excise(self):
        # plus 83/342 (covered), gross import 120/372 used for excise.
        hourly = _hourly((83, 37, 63), (342, 30, 20))
        res = build_period_invoice(
            hourly,
            fees=self.FEES,
            rcem=0.0,
            months=2,
            old_system=True,
            cover_day=83.0,
            cover_night=342.0,
        )
        assert res["kwh"]["gross_1"] == 120.0
        assert res["kwh"]["gross_2"] == 372.0
        assert res["sale_energy_day"] == 0.0
        assert res["sale_energy_night"] == 0.0
        assert res["excise_day"] == 0.60
        assert res["excise_night"] == 1.86
        assert res["distr_oze"] == 3.10
        assert res["distr_cogen"] == 1.28
        assert res["netto"] == 129.04
        assert res["brutto"] == 158.72
        assert res["deposit"] == 0.0
        assert res["old_system"] is True


class TestWarzywnaConsumer:
    """Real G11 consumer invoice (Warzywna, 2159 kWh, 2 months, no PV)."""

    def test_full_import_without_excise_line(self):
        hourly = {"import_1": {0: 2159.0}}
        res = build_period_invoice(
            hourly,
            fees=fees_from_options({}, "G11"),
            rcem=0.0,
            months=2,
            old_system=False,
            deposit_open_pln=0.0,
        )
        assert res["kwh"]["saldo_plus_1"] == 2159.0
        assert res["sale_energy_day"] == 1320.01
        assert res["distr_var_day"] == 752.41
        assert res["distr_fixed"] == 73.04
        assert res["netto"] == 2271.74
        assert res["brutto"] == 2794.24
        assert res["deposit"] == 0.0
        assert res["do_zaplaty"] == 2794.24


class TestDefensive:
    def test_empty_window_returns_zeros_not_an_exception(self):
        res = build_period_invoice(
            {}, fees=G12W_DEFAULT_FEES, rcem=0.25, old_system=False
        )
        assert res["kwh"] == {key: 0.0 for key in res["kwh"]}
        assert res["sale_energy_day"] == 0.0
        assert res["deposit"] == 0.0
        assert res["netto"] >= 0.0

    def test_garbage_hourly_values_do_not_raise(self):
        hourly = {
            "import_1": {0: "junk", 3600: None},
            "export_1": {0: -5.0},
        }
        res = build_period_invoice(
            hourly, fees=G12W_DEFAULT_FEES, rcem=0.25, old_system=False
        )
        assert isinstance(res["netto"], float)

    def test_old_system_defaults_to_no_deposit(self):
        hourly = _hourly((100, 0, 50))
        res = build_period_invoice(
            hourly, fees=G12W_DEFAULT_FEES, rcem=0.30, old_system=True
        )
        # Even with export, old net-metering must not create a PLN deposit.
        assert res["deposit"] == 0.0
        assert res["do_zaplaty"] == res["brutto"]


class TestWisniowaNetMeteringOpeningBank:
    """Faza 3: Wiśniowa 07-08.2026 with an opening warehouse (bank_open)."""

    FEES = {
        "energy_day": 0.7125,
        "energy_night": 0.4622,
        "excise_mwh": 5.0,
        "trade_fee": 16.18,
        "abonament": 0.70,
        "grid_fixed": 20.17,
        "grid_var_day": 0.4017,
        "grid_var_night": 0.0851,
        "quality": 0.0332,
        "oze": 0.0073,
        "cogen": 0.0030,
        "capacity": 24.05,
    }

    def test_opening_bank_covers_and_matches_invoice_to_the_grosz(self):
        # saldo_plus 83/342, gross import 120/372 (see the pure helper).
        hourly = _hourly((83, 37, 63), (342, 30, 20))
        res = build_period_invoice(
            hourly,
            fees=self.FEES,
            rcem=0.0,
            months=2,
            old_system=True,
            bank_open_1=500.0,
            bank_open_2=500.0,
            prosumer_coefficient=0.8,
            tariff="G12W",
        )
        # cover = min(bank_open, salda dodatnie) -> fully covered
        assert res["kwh"]["cover_1"] == 83.0
        assert res["kwh"]["cover_2"] == 342.0
        assert res["kwh"]["bank_open_1"] == 500.0
        assert res["kwh"]["bank_open_2"] == 500.0
        assert res["kwh"]["gross_import_1"] == 120.0
        assert res["kwh"]["gross_import_2"] == 372.0
        # real invoice FES/00042: 129,04 / 29,68 / 158,72
        assert res["sale_energy_day"] == 0.0
        assert res["sale_energy_night"] == 0.0
        assert res["excise_day"] == 0.60
        assert res["excise_night"] == 1.86
        assert res["distr_var_day"] == 0.0
        assert res["distr_var_night"] == 0.0
        assert res["distr_quality"] == 0.0
        assert res["distr_oze"] == 3.10
        assert res["distr_cogen"] == 1.28
        assert res["netto"] == 129.04
        assert res["vat"] == 29.68
        assert res["brutto"] == 158.72
        assert res["do_zaplaty"] == 158.72
        assert res["coverage_unknown"] is False
        assert res["warnings"] == []
        assert res["old_system"] is True
        assert res["system"] == "net_metering"
        # bank close = open + export*opust - cover
        assert res["kwh"]["bank_close_1"] == round(500.0 + 100.0 * 0.8 - 83.0, 3)
        assert res["kwh"]["bank_close_2"] == round(500.0 + 50.0 * 0.8 - 342.0, 3)

    def test_fixed_lines_are_split_but_sum_to_distr_fixed(self):
        hourly = _hourly((83, 37, 63), (342, 30, 20))
        res = build_period_invoice(
            hourly,
            fees=self.FEES,
            rcem=0.0,
            months=2,
            old_system=True,
            bank_open_1=500.0,
            bank_open_2=500.0,
        )
        assert res["distr_abonament"] == 1.40
        assert res["distr_grid_fixed"] == 40.34
        assert res["distr_capacity"] == 48.10
        assert res["distr_fixed"] == 89.84

    def test_no_bank_history_reports_unknown_and_warns(self):
        hourly = _hourly((83, 37, 63), (342, 30, 20))
        res = build_period_invoice(
            hourly, fees=self.FEES, rcem=0.0, months=2, old_system=True
        )
        assert res["coverage_unknown"] is True
        assert res["warnings"]
        assert res["kwh"]["cover_1"] == 0.0
        assert res["kwh"]["bank_open_1"] is None
        assert res["kwh"]["bank_close_1"] is None
        # The result is still returned (never an exception), JSON-serialisable.
        assert res["netto"] > 0.0
        import json

        json.dumps(res)


class TestFullInvoiceFieldSet:
    """Every invoice line item required by the Faza 3 output contract."""

    REQUIRED = (
        "sale_energy_day",
        "sale_energy_night",
        "excise_day",
        "excise_night",
        "excise",
        "trade_fee",
        "distr_abonament",
        "distr_grid_fixed",
        "distr_var_day",
        "distr_var_night",
        "distr_quality",
        "distr_oze",
        "distr_cogen",
        "distr_capacity",
        "distr_total",
        "distr_fixed",
        "netto",
        "vat",
        "brutto",
        "deposit_generated",
        "deposit_open",
        "deposit_applied",
        "deposit_close",
        "do_zaplaty",
        "coverage_unknown",
        "warnings",
        "old_system",
        "system",
        "prosumer_coefficient",
        "tariff",
        "rcem",
        "months",
        "kwh_source",
    )

    def test_net_billing_result_has_all_lines(self):
        hourly = _hourly((398, 38, 238), (309, 23, 197))
        res = build_period_invoice(
            hourly,
            fees=G12W_DEFAULT_FEES,
            rcem=0.29453,
            months=1,
            old_system=False,
            deposit_open_pln=0.0,
            prosumer_coefficient=0.0,
            tariff="G12W",
        )
        for key in self.REQUIRED:
            assert key in res, key
        assert res["deposit_generated"] == 157.59
        assert res["deposit_open"] == 0.0
        assert res["deposit_applied"] == 157.59
        assert res["deposit_close"] == 0.0
        assert res["coverage_unknown"] is False
        assert res["netto"] == 628.55
        assert res["brutto"] == 773.12
        assert res["do_zaplaty"] == 615.53

    def test_net_billing_unknown_opening_keeps_numbers_but_flags(self):
        # No explicit opening deposit -> honest null + coverage_unknown, yet
        # the single-month amount (Agrestowa 08.2026) is still exact.
        hourly = _hourly((398, 38, 238), (309, 23, 197))
        res = build_period_invoice(
            hourly, fees=G12W_DEFAULT_FEES, rcem=0.29453, months=1,
            old_system=False,
        )
        assert res["deposit_open"] is None
        assert res["coverage_unknown"] is True
        assert res["warnings"]
        assert res["brutto"] == 773.12
        assert res["deposit_applied"] == 157.59
        assert res["do_zaplaty"] == 615.53

