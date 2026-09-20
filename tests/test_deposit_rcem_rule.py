"""Bug B tests (v1.9.3-beta.2): generated-deposit RCEm rule investigation.

The seller prices the *generated* deposit with its own per-period rate, shown
on the invoice only as "N/D" with a before/after-coefficient split. The
investigation tested the hypothesis that this is the RCEm of the month
preceding delivery (M-1). The on-invoice evidence disproves a blanket M-1 (or
M) rule:

* Bursztynowa 4980469971/FES/00025 (06.2026): 258 kWh, before 49,66,
  after 61,08 -> implied rate ~0,19248. PSE RCEm: 0,19137 (M-1=05) /
  0,27320 (M=06). Neither matches.
* Bursztynowa 4980469971/FES/00027 (08.2026): 192 kWh, before 38,17,
  after 46,95 -> implied rate ~0,19880. PSE RCEm: 0,26288 (M-1=07) /
  0,29453 (M=08). Neither matches.
* Agrestowa 3253905811/FES/00045 (08.2026): 435 kWh x 0,29453 x 1,23 =
  157,59 — the delivery-month RCEm (M), and equal to M-1's July? No: July is
  0,26288, so Agrestowa uses M. Wiśniowa's periods similarly use PSE RCEm(M).

A blanket M-1 rule would therefore break Agrestowa (157,59 -> 140,65) and
Wiśniowa, while a blanket M rule would overstate both Bursztynowa deposits. The
seller's rate is a contract-specific value: the service keeps the period RCEm
and exposes it via the explicit ``rcem_deposit_pln`` override (Bug 2). These
tests pin the evidence and the override behaviour so a future change cannot
silently regress the verified invoices.
"""

from decimal import ROUND_HALF_UP, Decimal

import pytest

from custom_components.energa_mobile.core.verification import (
    build_period_invoice,
    deposit_rcem_from_table,
    deposit_rcem_month,
)


def _round2(value: float) -> float:
    return float(Decimal(str(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# PSE RCEm (PLN/kWh) parsed from the official PSE table (2026).
PSE_2026 = {
    (2026, 3): 0.19195,
    (2026, 4): 0.13292,
    (2026, 5): 0.19137,
    (2026, 6): 0.27320,
    (2026, 7): 0.26288,
    (2026, 8): 0.29453,
}


class TestBursztynowaEvidence:
    """The two Bursztynowa invoices pin the seller's implied deposit rates."""

    def test_june_2026_implied_rate_and_total(self):
        export, before, after = 258.0, 49.66, 61.08
        implied = before / export
        assert implied == pytest.approx(0.19248, abs=1e-5)
        # The invoice applies the 1.23 coefficient on the before value.
        assert _round2(before * 1.23) == after
        # PSE RCEm for the delivery month and the preceding month.
        assert PSE_2026[(2026, 6)] == 0.27320  # M
        assert PSE_2026[(2026, 5)] == 0.19137  # M-1
        # Neither PSE value reproduces the invoice rate.
        assert abs(PSE_2026[(2026, 6)] - implied) > 0.05
        assert abs(PSE_2026[(2026, 5)] - implied) > 0.001

    def test_august_2026_implied_rate_and_total(self):
        export, before, after = 192.0, 38.17, 46.95
        implied = before / export
        assert implied == pytest.approx(0.19880, abs=1e-5)
        assert _round2(before * 1.23) == after
        assert PSE_2026[(2026, 8)] == 0.29453  # M
        assert PSE_2026[(2026, 7)] == 0.26288  # M-1
        assert abs(PSE_2026[(2026, 8)] - implied) > 0.05
        assert abs(PSE_2026[(2026, 7)] - implied) > 0.05


class TestAgrestowaAndWisniowaUseDeliveryMonth:
    """Agrestowa/Wiśniowa invoices match the delivery-month RCEm (M)."""

    def test_agrestowa_august_deposit_is_rcm_m(self):
        export = 435.0
        assert _round2(export * PSE_2026[(2026, 8)] * 1.23) == 157.59
        # The M-1 (July) rate would give a different, wrong value.
        assert _round2(export * PSE_2026[(2026, 7)] * 1.23) == 140.65

    def test_agrestowa_july_deposit_is_rcm_m(self):
        export = 456.0
        assert _round2(export * PSE_2026[(2026, 7)] * 1.23) == 147.44


class TestMMinusOneRuleInvestigatedNotApplied:
    def test_helper_prefers_m_minus_one_with_fallback(self):
        # Helper encodes the *investigated* rule; the service does not use it.
        assert deposit_rcem_month(2026, 6) == (2026, 5)
        value, fell_back = deposit_rcem_from_table(PSE_2026, 2026, 6)
        assert value == PSE_2026[(2026, 5)] and fell_back is False
        value, fell_back = deposit_rcem_from_table({(2026, 6): 0.27320}, 2026, 6)
        assert value == 0.27320 and fell_back is True

    def test_default_period_rcem_keeps_agrestowa_exact(self):
        """Without an override the period RCEm keeps the Agrestowa invoice."""
        hourly = {
            "import_1": {0: 398.0, 3600: 38.0},
            "export_1": {3600: 276.0},
            "import_2": {0: 309.0, 3600: 23.0},
            "export_2": {3600: 220.0},
        }
        base = build_period_invoice(
            hourly, fees=_g12w_fees(), rcem=0.29453, months=1, old_system=False,
            deposit_open_pln=0.0, prosumer_coefficient=0.0, tariff="G12W",
        )
        override = build_period_invoice(
            hourly, fees=_g12w_fees(), rcem=0.29453, deposit_rcem=0.26288,
            months=1, old_system=False, deposit_open_pln=0.0,
            prosumer_coefficient=0.0, tariff="G12W",
        )
        assert base["deposit_generated"] == 157.59
        # Explicit override revalues ONLY the generated deposit.
        assert override["deposit_generated"] == 140.65
        assert override["rcem"] == 0.29453
        assert override["rcem_deposit"] == 0.26288
        # Energy and the payable amount are untouched by the deposit RCEm.
        assert override["netto"] == base["netto"]
        assert override["brutto"] == base["brutto"]


def _g12w_fees() -> dict:
    return {
        "energy_day": 0.6107,
        "energy_night": 0.3990,
        "excise_mwh": 5.0,
        "trade_fee": 0.0,
        "abonament": 0.74,
        "grid_fixed": 20.17,
        "grid_var_day": 0.4017,
        "grid_var_night": 0.0851,
        "quality": 0.0332,
        "oze": 0.0073,
        "cogen": 0.0030,
        "capacity": 24.05,
    }
