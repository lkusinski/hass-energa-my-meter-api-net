"""Tests for v1.9.2-beta.3 named product presets and provenance warnings.

Four invoice-verified presets exist (net PLN):

- ``G11_STANDARD``  — Warzywna FES/00017 (G11 consumer).
- ``G11_OFERTA``    — Bursztynowa FES/00027 (G11 net-billing prosumer).
- ``G12W_URZEDOWA`` — Agrestowa FES/00045 (net-billing, no handlowa).
- ``G12W_OFERTA``   — Wiśniowa FES/00042 (net-metering, handlowa 16,18).

Every preset must reproduce its own invoice; an explicit selection reports
``fee_source=product`` and emits no warning, while a defaulted/partial table
(or an unrecognized product) raises a loud PL warning.
"""

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "tariff_products_under_test",
    "custom_components/energa_mobile/tariff.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

compute_bill = _mod.compute_bill
fees_from_options = _mod.fees_from_options
fee_source = _mod.fee_source
fee_warnings = _mod.fee_warnings
resolve_product = _mod.resolve_product
product_option_values = _mod.product_option_values

G11_STANDARD_FEES = _mod.G11_STANDARD_FEES
G11_OFERTA_FEES = _mod.G11_OFERTA_FEES
G12W_URZEDOWA_FEES = _mod.G12W_URZEDOWA_FEES
G12W_OFERTA_FEES = _mod.G12W_OFERTA_FEES
PRODUCT_G11_STANDARD = _mod.PRODUCT_G11_STANDARD
PRODUCT_G11_OFERTA = _mod.PRODUCT_G11_OFERTA
PRODUCT_G12W_URZEDOWA = _mod.PRODUCT_G12W_URZEDOWA
PRODUCT_G12W_OFERTA = _mod.PRODUCT_G12W_OFERTA
PRODUCT_AUTO = _mod.PRODUCT_AUTO


class TestPresetTables:
    def test_g11_standard_is_warzywna(self):
        assert G11_STANDARD_FEES["energy_day"] == 0.6114
        assert G11_STANDARD_FEES["trade_fee"] == 16.18
        assert G11_STANDARD_FEES["abonament"] == 0.70
        assert G11_STANDARD_FEES["grid_fixed"] == 11.77
        assert G11_STANDARD_FEES["grid_var_day"] == 0.3485

    def test_g11_oferta_is_bursztynowa(self):
        assert G11_OFERTA_FEES["energy_day"] == 0.605286
        assert G11_OFERTA_FEES["trade_fee"] == 20.32
        assert G11_OFERTA_FEES["abonament"] == 0.74
        # everything else identical to the G11 standard table
        for key, value in G11_STANDARD_FEES.items():
            if key not in ("energy_day", "trade_fee", "abonament"):
                assert G11_OFERTA_FEES[key] == value

    def test_g12w_urzedowa_is_agrestowa(self):
        assert G12W_URZEDOWA_FEES["energy_day"] == 0.6107
        assert G12W_URZEDOWA_FEES["trade_fee"] == 0.0
        assert G12W_URZEDOWA_FEES["abonament"] == 0.74

    def test_g12w_oferta_is_wisniowa(self):
        assert G12W_OFERTA_FEES["energy_day"] == 0.7125
        assert G12W_OFERTA_FEES["energy_night"] == 0.4622
        assert G12W_OFERTA_FEES["trade_fee"] == 16.18
        assert G12W_OFERTA_FEES["abonament"] == 0.70


class TestPresetReproducesInvoice:
    def test_g11_standard_warzywna_invoice(self):
        fees = fees_from_options({"tariff_product": PRODUCT_G11_STANDARD}, "G11")
        assert fees == G11_STANDARD_FEES
        res = compute_bill(
            2159.0, 0.0, 0.0, 0.0, fees, months=2, deposit_pln=0.0
        )
        assert res["sale_energy_day"] == 1320.01
        assert res["trade_fee"] == 32.36
        assert res["sale_total"] == 1352.37
        assert res["distr_total"] == 919.37
        assert res["netto"] == 2271.74
        assert res["brutto"] == 2794.24
        assert res["deposit"] == 0.0

    def test_g11_oferta_bursztynowa_invoice(self):
        fees = fees_from_options({"tariff_product": PRODUCT_G11_OFERTA}, "G11")
        assert fees == G11_OFERTA_FEES
        res = compute_bill(
            import_day=30.0,
            import_night=0.0,
            export_kwh=192.0,
            rcem=0.1988,
            fees=fees,
            excise_day=149.0,
            excise_night=0.0,
            add_excise=True,
            deposit_pln=129.82,
        )
        assert res["sale_energy_day"] == 18.16
        assert res["excise_day"] == 0.75
        assert res["trade_fee"] == 20.32
        assert res["distr_abonament"] == 0.74
        assert res["netto"] == 87.56
        assert res["vat"] == 20.14
        assert res["brutto"] == 107.70
        assert res["deposit_applied"] == 23.26
        assert res["do_zaplaty"] == 84.44

    def test_g12w_urzedowa_agrestowa_invoice(self):
        fees = fees_from_options(
            {"tariff_product": PRODUCT_G12W_URZEDOWA}, "G12W"
        )
        assert fees == G12W_URZEDOWA_FEES
        res = compute_bill(
            398.0, 309.0, 435.0, 0.29453,
            fees, excise_day=38.0, excise_night=23.0, add_excise=True,
        )
        assert res["netto"] == 628.55
        assert res["vat"] == 144.57
        assert res["brutto"] == 773.12
        assert res["deposit"] == 157.59
        assert res["do_zaplaty"] == 615.53

    def test_g12w_oferta_wisniowa_invoice(self):
        fees = fees_from_options({"tariff_product": PRODUCT_G12W_OFERTA}, "G12W")
        assert fees == G12W_OFERTA_FEES
        res = compute_bill(
            83.0, 342.0, 1904.0, 0.0, fees, months=2,
            cover_day=83.0, cover_night=342.0,
            excise_day=120.0, excise_night=372.0, add_excise=True,
            deposit_pln=0.0,
        )
        assert res["trade_fee"] == 32.36
        assert res["netto"] == 129.04
        assert res["vat"] == 29.68
        assert res["brutto"] == 158.72


class TestG11OfertaVsStandard:
    def test_distinguished(self):
        std = fees_from_options({"tariff_product": PRODUCT_G11_STANDARD}, "G11")
        off = fees_from_options({"tariff_product": PRODUCT_G11_OFERTA}, "G11")
        assert std["energy_day"] != off["energy_day"]
        assert std["trade_fee"] != off["trade_fee"]
        assert std["abonament"] != off["abonament"]
        assert std["grid_fixed"] == off["grid_fixed"]

    def test_fee_source_product_after_selection(self):
        for product, tariff in (
            (PRODUCT_G11_STANDARD, "G11"),
            (PRODUCT_G11_OFERTA, "G11"),
            (PRODUCT_G12W_URZEDOWA, "G12W"),
            (PRODUCT_G12W_OFERTA, "G12W"),
        ):
            assert fee_source({"tariff_product": product}, tariff)[0] == "product"


class TestResolveProduct:
    def test_g11_never_infers(self):
        assert resolve_product({}, "G11", True) == (None, "none")
        assert resolve_product({}, "G11", False) == (None, "none")

    def test_g12w_infers_from_system(self):
        assert resolve_product({}, "G12W", True) == (PRODUCT_G12W_OFERTA, "inferred")
        assert resolve_product({}, "G12W", False) == (
            PRODUCT_G12W_URZEDOWA,
            "inferred",
        )
        assert resolve_product({}, "G12W", None) == (None, "none")

    def test_explicit_wins(self):
        assert resolve_product(
            {"tariff_product": PRODUCT_G11_OFERTA}, "G11", False
        ) == (PRODUCT_G11_OFERTA, "explicit")

    def test_manual_rates_block_inference(self):
        opts = {"tariff_trade_fee": 16.18}
        assert resolve_product(opts, "G12W", True) == (None, "none")


class TestProductOptionValues:
    def test_materialises_every_rate(self):
        values = product_option_values(PRODUCT_G11_OFERTA)
        assert len(values) == 12
        assert values["tariff_energy_day"] == 0.605286
        assert values["tariff_trade_fee"] == 20.32
        assert values["tariff_abonament"] == 0.74

    def test_auto_or_unknown_is_empty(self):
        assert product_option_values(PRODUCT_AUTO) == {}
        assert product_option_values(None) == {}
        assert product_option_values("NOPE") == {}


class TestFeeWarnings:
    def test_defaults_warns_and_names_g11_standard(self):
        assert fee_source({}, "G11")[0] == "defaults"
        warnings = fee_warnings({}, "G11")
        assert any("tabeli domyślnej" in w for w in warnings)
        assert any(PRODUCT_G11_STANDARD in w for w in warnings)

    def test_partial_warns(self):
        opts = {"tariff_trade_fee": 16.18}
        assert fee_source(opts, "G11")[0] == "partial"
        assert any("tabeli domyślnej" in w for w in fee_warnings(opts, "G11"))

    def test_explicit_preset_is_silent(self):
        for product, tariff in (
            (PRODUCT_G11_STANDARD, "G11"),
            (PRODUCT_G11_OFERTA, "G11"),
            (PRODUCT_G12W_OFERTA, "G12W"),
            (PRODUCT_G12W_URZEDOWA, "G12W"),
        ):
            assert fee_warnings({"tariff_product": product}, tariff) == []

    def test_inferred_g12w_warns_without_default_table_claim(self):
        warnings = fee_warnings({}, "G12W", old_system=True)
        assert warnings
        assert not any("tabeli domyślnej" in w for w in warnings)
        assert any(PRODUCT_G12W_OFERTA in w for w in warnings)

    def test_unknown_system_defaults_warns(self):
        warnings = fee_warnings({}, "G12W", old_system=None)
        assert any("tabeli domyślnej" in w for w in warnings)

    def test_unrecognized_product_warns(self):
        warnings = fee_warnings(
            {"tariff_product": "NOPE"}, "G12W", old_system=False
        )
        assert any("Nieznany produkt" in w for w in warnings)
