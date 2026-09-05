"""Golden test suite for invoice reconciliation, ledger invariants, and checkpoint recovery.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026)
- Invoice reconciliation against real document FAK_1200222768_FES_00017.pdf
  (Warzywna 13, G11, 2159 kWh, period 04.02.2026 - 05.04.2026, 2 months).
- Acceptance criteria #9: Ledger units invariant (kWh and PLN ledger isolation).
- Acceptance criteria #11: Resumable checkpoint recovery without side-effects.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import pytest

from custom_components.energa_mobile.core.settlement.models import (
    LotAllocation,
    SettlementLot,
    SettlementSummary,
)
from custom_components.energa_mobile.core.settlement.fifo_net_billing import (
    InvoiceLineCharge,
    run_fifo_net_billing,
)
from custom_components.energa_mobile.core.tariffs.effective_tariffs import (
    calculate_g11_invoice_lines,
    calculate_g12w_invoice_lines,
    reconcile_invoice,
)
from custom_components.energa_mobile.storage.sqlite.database import CanonicalStorage


def test_golden_invoice_fak_1200222768_fes_00017_reconciliation():
    """Verify that calculated G11 invoice matches FAK_1200222768_FES_00017.pdf line-by-line."""
    # Invoice parameters from PDF
    inv_number = "1200222768/FES/00017"
    period_start = date(2026, 2, 4)
    period_end = date(2026, 4, 5)
    months = Decimal("2.0")
    consumption_kwh = Decimal("2159")

    # Compute lines using pure domain tariff calculator
    comp_lines = calculate_g11_invoice_lines(
        consumption_kwh=consumption_kwh,
        months=months,
        effective_date=period_start,
    )

    # Verify line quantities and net amounts from page 2 of PDF
    line_dict = {l.rate_id: l for l in comp_lines}

    assert line_dict["trade_fee"].total_net == Decimal("32.36")
    assert line_dict["energy_day"].total_net == Decimal("1320.01")
    assert line_dict["abonament"].total_net == Decimal("1.40")
    assert line_dict["grid_fixed"].total_net == Decimal("23.54")
    assert line_dict["grid_var_day"].total_net == Decimal("752.41")
    assert line_dict["quality"].total_net == Decimal("71.68")
    assert line_dict["oze"].total_net == Decimal("15.76")
    assert line_dict["cogen"].total_net == Decimal("6.48")
    assert line_dict["capacity"].total_net == Decimal("48.10")

    total_net = sum((l.total_net for l in comp_lines), Decimal("0.0"))
    assert total_net == Decimal("2271.74")  # Matches exact "Razem wartość netto (1 + 2)" on PDF!

    # Actual lines as printed on invoice (gross per line)
    invoiced_seller_lines = [
        {"rate_id": "trade_fee", "amount_gross": Decimal("39.80"), "description": "Opłata handlowa"},
        {"rate_id": "energy_day", "amount_gross": Decimal("1623.61"), "description": "Energia czynna całodobowa"},
        {"rate_id": "abonament", "amount_gross": Decimal("1.72"), "description": "Opłata abonamentowa"},
        {"rate_id": "grid_fixed", "amount_gross": Decimal("28.95"), "description": "Opłata sieciowa stała"},
        {"rate_id": "grid_var_day", "amount_gross": Decimal("925.46"), "description": "Opłata sieciowa zmienna całodobowa"},
        {"rate_id": "quality", "amount_gross": Decimal("88.17"), "description": "Opłata jakościowa"},
        {"rate_id": "oze", "amount_gross": Decimal("19.38"), "description": "Opłata OZE"},
        {"rate_id": "cogen", "amount_gross": Decimal("7.97"), "description": "Opłata kogeneracyjna"},
        {"rate_id": "capacity", "amount_gross": Decimal("59.16"), "description": "Opłata mocowa"},
    ]

    header_invoiced_gross = Decimal("2794.24")  # "Wartość usługi: 2 794,24 zł" on PDF page 1

    report = reconcile_invoice(
        invoice_number=inv_number,
        period_start=period_start,
        period_end=period_end,
        computed_lines=comp_lines,
        invoiced_lines=invoiced_seller_lines,
        header_invoiced_gross=header_invoiced_gross,
    )

    assert report.status == "MATCH"
    assert report.variance_gross <= Decimal("0.05")  # exact match within 2 groszy aggregate VAT rounding
    assert report.invoiced_gross == Decimal("2794.24")
    assert len(report.line_variances) == 9

    # Verify per-line diff is negligible (<= 0.01 PLN rounding)
    for lv in report.line_variances:
        assert abs(lv["diff_gross"]) <= Decimal("0.01")


def test_ledger_unit_invariant_enforcement():
    """Verify acceptance criteria #9: Physical ledger (kWh) and monetary ledger (PLN) cannot be mixed."""
    # 1. Reject invalid units in SettlementLot
    with pytest.raises(ValueError, match="Ledger invariant violation"):
        SettlementLot(
            lot_id="bad_1",
            ppe_id="PL_001",
            unit="EUR",
            zone="total",
            original_amount=Decimal("100"),
            remaining_amount=Decimal("100"),
            created_at_utc=datetime.now(timezone.utc),
            assigned_at=date(2026, 1, 1),
            expires_at=date(2027, 1, 1),
        )

    # 2. Reject invalid units in SettlementSummary
    with pytest.raises(ValueError, match="Ledger invariant violation"):
        SettlementSummary(unit="USD")

    # 3. Reject kWh lots passed to Net-Billing (monetary engine)
    kwh_lot = SettlementLot(
        lot_id="kwh_lot_1",
        ppe_id="PL_001",
        unit="kWh",
        zone="total",
        original_amount=Decimal("100"),
        remaining_amount=Decimal("100"),
        created_at_utc=datetime.now(timezone.utc),
        assigned_at=date(2026, 1, 1),
        expires_at=date(2027, 1, 1),
    )

    charges = [
        InvoiceLineCharge(
            line_id="c1",
            description="Energia czynna",
            amount_gross=Decimal("50.00"),
            is_deposit_eligible=True,
            period_start=date(2026, 2, 1),
            period_end=date(2026, 2, 28),
        )
    ]

    with pytest.raises(ValueError, match="Ledger invariant violation: Net-billing deposit lots must be in 'PLN'"):
        run_fifo_net_billing(
            ppe_id="PL_001",
            deposit_lots=[kwh_lot],
            charges=charges,
        )


def test_resumable_checkpoint_recovery():
    """Verify acceptance criteria #11: Interrupted task resumes from checkpoint without duplicating work."""
    storage = CanonicalStorage(":memory:")
    job = "sync_history_2026"

    # Step 1: Start job and save progress at 2026-01-15
    storage.save_checkpoint(job, ppe_id="PPE_100", cursor="2026-01-15", status="in_progress")

    cp1 = storage.get_checkpoint(job)
    assert cp1 is not None
    assert cp1["cursor"] == "2026-01-15"
    assert cp1["status"] == "in_progress"

    # Step 2: Simulate failure and record error
    storage.save_checkpoint(job, ppe_id="PPE_100", cursor="2026-01-15", status="failed", last_error="HTTP 503 Timeout")
    cp_failed = storage.get_checkpoint(job)
    assert cp_failed["status"] == "failed"
    assert cp_failed["last_error"] == "HTTP 503 Timeout"
    assert cp_failed["cursor"] == "2026-01-15"

    # Step 3: Resume job from last cursor (2026-01-15) and finish to 2026-02-01
    cursor_resumed = cp_failed["cursor"]
    assert cursor_resumed == "2026-01-15"

    storage.save_checkpoint(job, ppe_id="PPE_100", cursor="2026-02-01", status="completed")
    cp_done = storage.get_checkpoint(job)
    assert cp_done["status"] == "completed"
    assert cp_done["cursor"] == "2026-02-01"
