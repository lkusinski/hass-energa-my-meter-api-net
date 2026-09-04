"""Effective-dated tariff plans and per-line invoice reconciliation engine.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 7 & 8.
- Incorporates official 2026 tariffs with legal provenance.
- Calculates per-line invoice items with deposit eligibility tracking.
- Implements reconciliation comparing computed lines with seller invoice lines.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .models import (
    InvoiceLineItem,
    InvoiceReconciliation,
    TariffPlan,
    TariffRate,
)

VAT_23 = Decimal("0.23")


def get_g12w_tariff_plan() -> TariffPlan:
    """Construct standard G12w tariff plan with 2026 effective rates."""
    plan = TariffPlan(tariff_code="G12w", description="Taryfa dwustrefowa weekendowa")
    d_2026 = date(2026, 1, 1)

    plan.rates = {
        "energy_day": [
            TariffRate(
                rate_id="energy_day",
                name="Energia czynna - szczyt (L1)",
                unit="PLN/kWh",
                rate_net=Decimal("0.6107"),
                valid_from=d_2026,
                source_document="Faktura G12w 07.2026",
                is_deposit_eligible=True,
            )
        ],
        "energy_night": [
            TariffRate(
                rate_id="energy_night",
                name="Energia czynna - pozaszczyt (L2)",
                unit="PLN/kWh",
                rate_net=Decimal("0.3990"),
                valid_from=d_2026,
                source_document="Faktura G12w 07.2026",
                is_deposit_eligible=True,
            )
        ],
        "grid_var_day": [
            TariffRate(
                rate_id="grid_var_day",
                name="Składnik zmienny stawki sieciowej - szczyt",
                unit="PLN/kWh",
                rate_net=Decimal("0.4017"),
                valid_from=d_2026,
                source_document="Taryfa OSD Energa-Operator 2026",
                is_deposit_eligible=False,
            )
        ],
        "grid_var_night": [
            TariffRate(
                rate_id="grid_var_night",
                name="Składnik zmienny stawki sieciowej - pozaszczyt",
                unit="PLN/kWh",
                rate_net=Decimal("0.0851"),
                valid_from=d_2026,
                source_document="Taryfa OSD Energa-Operator 2026",
                is_deposit_eligible=False,
            )
        ],
        "grid_fixed": [
            TariffRate(
                rate_id="grid_fixed",
                name="Składnik stały stawki sieciowej",
                unit="PLN/month",
                rate_net=Decimal("20.17"),
                valid_from=d_2026,
                source_document="Taryfa OSD Energa-Operator 2026",
                is_deposit_eligible=False,
            )
        ],
        "abonament": [
            TariffRate(
                rate_id="abonament",
                name="Stawka abonamentowa",
                unit="PLN/month",
                rate_net=Decimal("0.74"),
                valid_from=d_2026,
                source_document="Taryfa OSD Energa-Operator 2026",
                is_deposit_eligible=False,
            )
        ],
        "quality": [
            TariffRate(
                rate_id="quality",
                name="Stawka jakościowa",
                unit="PLN/kWh",
                rate_net=Decimal("0.0332"),
                valid_from=d_2026,
                source_document="Taryfa OSD Energa-Operator 2026",
                is_deposit_eligible=False,
            )
        ],
        "oze": [
            TariffRate(
                rate_id="oze",
                name="Stawka OZE",
                unit="PLN/kWh",
                rate_net=Decimal("0.0073"),
                valid_from=d_2026,
                source_document="Taryfa OSD Energa-Operator 2026",
                is_deposit_eligible=False,
            )
        ],
        "cogen": [
            TariffRate(
                rate_id="cogen",
                name="Stawka kogeneracyjna",
                unit="PLN/kWh",
                rate_net=Decimal("0.0030"),
                valid_from=d_2026,
                source_document="Taryfa OSD Energa-Operator 2026",
                is_deposit_eligible=False,
            )
        ],
        "capacity": [
            TariffRate(
                rate_id="capacity",
                name="Opłata mocowa ryczałt (>2800 kWh)",
                unit="PLN/month",
                rate_net=Decimal("24.05"),
                valid_from=d_2026,
                source_document="Informacja Prezesa URE 58/2025",
                is_deposit_eligible=False,
            )
        ],
    }
    return plan


def get_g11_tariff_plan() -> TariffPlan:
    """Construct standard G11 tariff plan with exact rates from consumer invoice."""
    plan = TariffPlan(tariff_code="G11", description="Taryfa jednostrefowa")
    d_2026 = date(2026, 1, 1)

    plan.rates = {
        "energy_day": [
            TariffRate(
                rate_id="energy_day",
                name="Energia czynna całodobowa",
                unit="PLN/kWh",
                rate_net=Decimal("0.6114"),
                valid_from=d_2026,
                source_document="Faktura G11 1200222768/FES/00017",
                is_deposit_eligible=True,
            )
        ],
        "trade_fee": [
            TariffRate(
                rate_id="trade_fee",
                name="Opłata handlowa",
                unit="PLN/month",
                rate_net=Decimal("16.18"),
                valid_from=d_2026,
                source_document="Faktura G11 1200222768/FES/00017",
                is_deposit_eligible=True,
            )
        ],
        "grid_var_day": [
            TariffRate(
                rate_id="grid_var_day",
                name="Składnik zmienny stawki sieciowej",
                unit="PLN/kWh",
                rate_net=Decimal("0.3485"),
                valid_from=d_2026,
                source_document="Faktura G11 1200222768/FES/00017",
                is_deposit_eligible=False,
            )
        ],
        "grid_fixed": [
            TariffRate(
                rate_id="grid_fixed",
                name="Składnik stały stawki sieciowej",
                unit="PLN/month",
                rate_net=Decimal("11.77"),
                valid_from=d_2026,
                source_document="Faktura G11 1200222768/FES/00017",
                is_deposit_eligible=False,
            )
        ],
        "abonament": [
            TariffRate(
                rate_id="abonament",
                name="Stawka abonamentowa",
                unit="PLN/month",
                rate_net=Decimal("0.70"),
                valid_from=d_2026,
                source_document="Faktura G11 1200222768/FES/00017",
                is_deposit_eligible=False,
            )
        ],
        "quality": [
            TariffRate(
                rate_id="quality",
                name="Stawka jakościowa",
                unit="PLN/kWh",
                rate_net=Decimal("0.0332"),
                valid_from=d_2026,
                source_document="Faktura G11 1200222768/FES/00017",
                is_deposit_eligible=False,
            )
        ],
        "oze": [
            TariffRate(
                rate_id="oze",
                name="Stawka OZE",
                unit="PLN/kWh",
                rate_net=Decimal("0.0073"),
                valid_from=d_2026,
                source_document="Faktura G11 1200222768/FES/00017",
                is_deposit_eligible=False,
            )
        ],
        "cogen": [
            TariffRate(
                rate_id="cogen",
                name="Stawka kogeneracyjna",
                unit="PLN/kWh",
                rate_net=Decimal("0.0030"),
                valid_from=d_2026,
                source_document="Faktura G11 1200222768/FES/00017",
                is_deposit_eligible=False,
            )
        ],
        "capacity": [
            TariffRate(
                rate_id="capacity",
                name="Opłata mocowa ryczałt (>2800 kWh)",
                unit="PLN/month",
                rate_net=Decimal("24.05"),
                valid_from=d_2026,
                source_document="Informacja Prezesa URE 58/2025",
                is_deposit_eligible=False,
            )
        ],
    }
    return plan


def reconcile_invoice(
    invoice_number: str,
    period_start: date,
    period_end: date,
    computed_lines: list[InvoiceLineItem],
    invoiced_lines: list[dict],
    tolerance_percent: Decimal = Decimal("2.5"),
) -> InvoiceReconciliation:
    """Perform per-line invoice reconciliation and audit variance report.

    Args:
        invoice_number: Invoice identifier (e.g. "1200222768/FES/00017").
        period_start: Billing period start.
        period_end: Billing period end.
        computed_lines: List of computed InvoiceLineItem objects from meter data.
        invoiced_lines: List of dicts representing actual lines from seller document:
                        {"rate_id": str, "amount_gross": Decimal, "description": str}.
        tolerance_percent: Tolerance threshold for automatic match classification.

    Returns:
        InvoiceReconciliation report.
    """
    comp_gross = sum((line.total_gross for line in computed_lines), Decimal("0.0"))
    inv_gross = sum(
        (Decimal(str(l.get("amount_gross", "0.0"))) for l in invoiced_lines),
        Decimal("0.0"),
    )

    diff = comp_gross - inv_gross
    abs_diff = abs(diff)

    if inv_gross > Decimal("0.0"):
        var_pct = round((abs_diff / inv_gross) * Decimal("100.0"), 2)
    else:
        var_pct = Decimal("0.0") if comp_gross == Decimal("0.0") else Decimal("100.0")

    # Per-line comparison
    line_variances = []
    inv_map = {l.get("rate_id"): l for l in invoiced_lines}

    for comp in computed_lines:
        inv_match = inv_map.get(comp.rate_id)
        if inv_match:
            actual = Decimal(str(inv_match.get("amount_gross", "0.0")))
            line_diff = comp.total_gross - actual
            line_variances.append({
                "rate_id": comp.rate_id,
                "name": comp.name,
                "computed_gross": comp.total_gross,
                "invoiced_gross": actual,
                "diff_gross": line_diff,
            })
        else:
            line_variances.append({
                "rate_id": comp.rate_id,
                "name": comp.name,
                "computed_gross": comp.total_gross,
                "invoiced_gross": None,
                "diff_gross": comp.total_gross,
            })

    if abs_diff == Decimal("0.0"):
        status = "MATCH"
    elif var_pct <= tolerance_percent:
        status = "WITHIN_TOLERANCE"
    else:
        status = "DISCREPANCY"

    return InvoiceReconciliation(
        invoice_number=invoice_number,
        period_start=period_start,
        period_end=period_end,
        computed_gross=round(comp_gross, 2),
        invoiced_gross=round(inv_gross, 2),
        variance_gross=round(diff, 2),
        variance_percent=var_pct,
        line_variances=line_variances,
        status=status,
        notes=f"Reconciliation {status}: difference {diff} PLN ({var_pct}%) vs tolerance {tolerance_percent}%",
    )
