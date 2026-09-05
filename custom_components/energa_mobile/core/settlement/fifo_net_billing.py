"""Pure domain FIFO settlement engine for Net-Billing (Monetary Ledger, PLN).

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 6, 7 & 8.
- Amounts in exact Decimal.
- Assigned_at: derived from seller statement (default 1st day of month M+1).
- Expiry: 12 rolling months from assigned_at date.
- Allocation: consumed strictly by eligible energy purchase lines (never distribution).
- Cash-out: max 20% (RCEm) or 30% (RCE) refund of unexhausted deposit upon expiry.
- Negative price floor: configurable / temporal (e.g. max(price, 0.0) where mandated).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from .models import LotAllocation, SettlementLot, SettlementSummary


@dataclass(frozen=True)
class InvoiceLineCharge:
    """An individual charge line on an energy invoice."""

    line_id: str
    description: str
    amount_gross: Decimal
    is_deposit_eligible: bool               # True for active energy purchase; False for distribution/fixed fees
    period_start: date
    period_end: date


def run_fifo_net_billing(
    ppe_id: str,
    deposit_lots: list[SettlementLot],
    charges: list[InvoiceLineCharge],
    today: date | None = None,
    cash_out_percent: Decimal = Decimal("20.0"),  # 20% for RCEm, 30% for RCE
) -> tuple[SettlementSummary, list[dict]]:
    """Run pure FIFO allocation for net-billing monetary deposit.

    Args:
        ppe_id: Logical delivery point ID.
        deposit_lots: List of active/historical PLN deposit lots.
        charges: List of invoice line items to settle.
        today: Evaluation reference date.
        cash_out_percent: Percentage of unexhausted expired deposit returned as cash-out.

    Returns:
        (SettlementSummary, invoice_settlement_results)
    """
    as_of = today or date.today()
    summary = SettlementSummary(unit="PLN")

    for lot in deposit_lots:
        if lot.unit != "PLN":
            raise ValueError(
                f"Ledger invariant violation: Net-billing deposit lots must be in 'PLN', got '{lot.unit}' in lot {lot.lot_id}"
            )

    # Sort deposit lots by assigned_at ascending (FIFO order)
    sorted_lots = sorted(deposit_lots, key=lambda l: (l.assigned_at, l.lot_id))
    allocations: list[LotAllocation] = []

    # Check expiration as of today
    total_expired = Decimal("0.0")
    total_cash_out = Decimal("0.0")

    for lot in sorted_lots:
        if not lot.is_exhausted and lot.is_expired(as_of):
            expired_amt = lot.remaining_amount
            total_expired += expired_amt
            refund = round(expired_amt * (cash_out_percent / Decimal("100.0")), 2)
            total_cash_out += refund
            lot.remaining_amount = Decimal("0.0")

    invoice_results = []
    total_applied = Decimal("0.0")

    # Settle each invoice charge line
    for charge in charges:
        line_due = charge.amount_gross
        line_applied = Decimal("0.0")

        if charge.is_deposit_eligible and line_due > Decimal("0.0"):
            # Consume from oldest unexpired lots
            for lot in sorted_lots:
                if lot.is_exhausted or lot.is_expired(as_of):
                    continue

                take = min(lot.remaining_amount, line_due)
                if take > Decimal("0.0"):
                    lot.remaining_amount -= take
                    line_due -= take
                    line_applied += take
                    total_applied += take

                    alloc = LotAllocation(
                        allocation_id=f"alloc_nb_{lot.lot_id}_{charge.line_id}",
                        lot_id=lot.lot_id,
                        consumption_target_id=charge.line_id,
                        allocated_amount=take,
                        allocated_at_utc=datetime.now(timezone.utc),
                        notes=f"Settled {charge.description}",
                    )
                    allocations.append(alloc)

                if line_due <= Decimal("0.0"):
                    break

        invoice_results.append({
            "line_id": charge.line_id,
            "description": charge.description,
            "amount_gross": charge.amount_gross,
            "is_deposit_eligible": charge.is_deposit_eligible,
            "deposit_applied": line_applied,
            "remaining_due": line_due,
        })

    active_lots = [lot for lot in sorted_lots if not lot.is_exhausted]
    total_active = sum((lot.remaining_amount for lot in active_lots), Decimal("0.0"))

    summary.total_active_balance = round(total_active, 2)
    summary.total_consumed = round(total_applied, 2)
    summary.total_expired = round(total_expired, 2)
    summary.active_lots = active_lots
    summary.allocations = allocations

    return summary, invoice_results
