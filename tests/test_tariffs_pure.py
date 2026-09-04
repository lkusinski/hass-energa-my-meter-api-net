"""Unit tests for effective-dated tariffs and invoice reconciliation engine.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 7 & 8.
Verifies:
- Tariff rates have effective dates and legal source documents.
- Strict classification of deposit eligibility (energy sale vs distribution).
- Per-line invoice reconciliation and tolerance classification.
"""

from datetime import date
from decimal import Decimal

from custom_components.energa_mobile.core.tariffs.effective_tariffs import (
    get_g11_tariff_plan,
    get_g12w_tariff_plan,
    reconcile_invoice,
)
from custom_components.energa_mobile.core.tariffs.models import InvoiceLineItem


def test_tariff_plan_rates_and_deposit_eligibility():
    plan_g12w = get_g12w_tariff_plan()
    rate_day = plan_g12w.get_rate("energy_day", date(2026, 7, 1))
    assert rate_day is not None
    assert rate_day.rate_net == Decimal("0.6107")
    assert rate_day.is_deposit_eligible is True
    assert "Faktura G12w" in rate_day.source_document

    rate_grid = plan_g12w.get_rate("grid_fixed", date(2026, 7, 1))
    assert rate_grid is not None
    assert rate_grid.is_deposit_eligible is False

    rate_cap = plan_g12w.get_rate("capacity", date(2026, 7, 1))
    assert rate_cap is not None
    assert rate_cap.is_deposit_eligible is False
    assert "URE" in rate_cap.source_document


def test_invoice_reconciliation_exact_match():
    # Calculated lines
    comp_lines = [
        InvoiceLineItem(
            line_id="l1",
            rate_id="energy_day",
            name="Energia czynna",
            quantity=Decimal("100"),
            unit="kWh",
            unit_price_net=Decimal("0.6114"),
            total_net=Decimal("61.14"),
            vat_rate=Decimal("0.23"),
            total_gross=Decimal("75.20"),
            is_deposit_eligible=True,
        ),
        InvoiceLineItem(
            line_id="l2",
            rate_id="grid_fixed",
            name="Dystrybucja stała",
            quantity=Decimal("1"),
            unit="msc",
            unit_price_net=Decimal("11.77"),
            total_net=Decimal("11.77"),
            vat_rate=Decimal("0.23"),
            total_gross=Decimal("14.48"),
            is_deposit_eligible=False,
        ),
    ]

    # Invoiced seller lines matching exactly
    invoiced_lines = [
        {"rate_id": "energy_day", "amount_gross": Decimal("75.20"), "description": "Energia czynna"},
        {"rate_id": "grid_fixed", "amount_gross": Decimal("14.48"), "description": "Dystrybucja stała"},
    ]

    rep = reconcile_invoice(
        invoice_number="INV_12345",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        computed_lines=comp_lines,
        invoiced_lines=invoiced_lines,
    )

    assert rep.status == "MATCH"
    assert rep.variance_gross == Decimal("0.00")
    assert rep.variance_percent == Decimal("0.00")
    assert len(rep.line_variances) == 2


def test_invoice_reconciliation_within_tolerance_vs_discrepancy():
    comp_lines = [
        InvoiceLineItem(
            line_id="l1",
            rate_id="energy_day",
            name="Energia czynna",
            quantity=Decimal("1000"),
            unit="kWh",
            unit_price_net=Decimal("0.60"),
            total_net=Decimal("600.00"),
            vat_rate=Decimal("0.23"),
            total_gross=Decimal("738.00"),
            is_deposit_eligible=True,
        )
    ]

    # Seller billed 745.00 (+7.00 PLN diff, ~0.95% variance)
    inv_small_diff = [{"rate_id": "energy_day", "amount_gross": Decimal("745.00")}]
    rep_ok = reconcile_invoice("INV_OK", date(2026, 1, 1), date(2026, 1, 31), comp_lines, inv_small_diff, tolerance_percent=Decimal("2.5"))
    assert rep_ok.status == "WITHIN_TOLERANCE"
    assert rep_ok.variance_percent < Decimal("2.5")

    # Seller billed 800.00 (+62.00 PLN diff, ~8.4% variance)
    inv_big_diff = [{"rate_id": "energy_day", "amount_gross": Decimal("800.00")}]
    rep_bad = reconcile_invoice("INV_BAD", date(2026, 1, 1), date(2026, 1, 31), comp_lines, inv_big_diff, tolerance_percent=Decimal("2.5"))
    assert rep_bad.status == "DISCREPANCY"
    assert rep_bad.variance_percent > Decimal("5.0")
