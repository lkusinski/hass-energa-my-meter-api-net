"""Pure domain FIFO settlement engine for Net-Metering (Physical Energy Ledger, kWh).

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 6, 7 & 8.
- Amounts in exact Decimal.
- Expiry: 12 rolling months from the end of introduction month M (i.e. end of M+12).
- Allocation order: oldest unexpired lot first (FIFO).
- Full provenance and allocation tracing.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from decimal import Decimal

from .models import LotAllocation, SettlementLot, SettlementSummary


def _month_end(year: int, month: int) -> date:
    """Return the last calendar day of a given year and month."""
    days = calendar.monthrange(year, month)[1]
    return date(year, month, days)


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Add delta months to year/month."""
    total_m = (year * 12 + (month - 1)) + delta
    return (total_m // 12, (total_m % 12) + 1)


def run_fifo_net_metering(
    ppe_id: str,
    monthly_flows: list[dict],
    coefficient: Decimal = Decimal("0.8"),
    today: date | None = None,
    zone: str = "total",
) -> SettlementSummary:
    """Run pure FIFO allocation for net-metering physical energy warehouse.

    Args:
        ppe_id: Logical delivery point ID.
        monthly_flows: List of dicts with keys {"year": int, "month": int, "import_kwh": Decimal, "export_kwh": Decimal}.
        coefficient: Prosumer discount factor (e.g. 0.8 for <=10kW, 0.7 for >10kW).
        today: Evaluation reference date.
        zone: Register / tariff zone ("total", "day", "night").

    Returns:
        SettlementSummary with active balance, expired kWh, and allocations.
    """
    as_of = today or date.today()
    summary = SettlementSummary(unit="kWh")

    # Sort flows chronologically
    sorted_flows = sorted(monthly_flows, key=lambda x: (int(x["year"]), int(x["month"])))

    lots: list[SettlementLot] = []
    allocations: list[LotAllocation] = []

    total_deposited = Decimal("0.0")
    total_consumed = Decimal("0.0")
    total_uncovered = Decimal("0.0")

    for flow in sorted_flows:
        y = int(flow["year"])
        m = int(flow["month"])
        imp = Decimal(str(flow.get("import_kwh", "0.0")))
        exp = Decimal(str(flow.get("export_kwh", "0.0")))

        # Check for expiry of existing lots up to the start of this month
        month_start = date(y, m, 1)
        for lot in lots:
            if not lot.is_exhausted and lot.is_expired(month_start):
                summary.total_expired += lot.remaining_amount
                lot.remaining_amount = Decimal("0.0")

        # 1. New deposit lot from export in this month
        if exp > Decimal("0.0"):
            credited = round(exp * coefficient, 3)
            assigned_at = _month_end(y, m)
            exp_y, exp_m = _add_months(y, m, 12)
            expires_at = _month_end(exp_y, exp_m)

            lot_id = f"lot_{ppe_id}_{zone}_{y}_{m:02d}"
            lot = SettlementLot(
                lot_id=lot_id,
                ppe_id=ppe_id,
                unit="kWh",
                zone=zone,
                original_amount=credited,
                remaining_amount=credited,
                created_at_utc=datetime.now(timezone.utc),
                assigned_at=assigned_at,
                expires_at=expires_at,
                rule_version="net_metering_fifo_12m",
                provenance=f"Export {exp} kWh * coeff {coefficient} in {y}-{m:02d}",
            )
            lots.append(lot)
            total_deposited += credited

        # 2. Consume import from oldest unexpired lots (FIFO)
        remaining_import = imp
        if remaining_import > Decimal("0.0"):
            for lot in lots:
                if lot.is_exhausted or lot.is_expired(month_start):
                    continue

                take = min(lot.remaining_amount, remaining_import)
                if take > Decimal("0.0"):
                    lot.remaining_amount -= take
                    remaining_import -= take
                    total_consumed += take
                    alloc = LotAllocation(
                        allocation_id=f"alloc_{lot.lot_id}_{y}_{m:02d}",
                        lot_id=lot.lot_id,
                        consumption_target_id=f"imp_{ppe_id}_{zone}_{y}_{m:02d}",
                        allocated_amount=take,
                        allocated_at_utc=datetime.now(timezone.utc),
                        notes=f"Covered import {y}-{m:02d}",
                    )
                    allocations.append(alloc)

                if remaining_import <= Decimal("0.0"):
                    break

            if remaining_import > Decimal("0.0"):
                total_uncovered += remaining_import

    # Final pass for expiry as of `as_of` date
    for lot in lots:
        if not lot.is_exhausted and lot.is_expired(as_of):
            summary.total_expired += lot.remaining_amount
            lot.remaining_amount = Decimal("0.0")

    active_lots = [lot for lot in lots if not lot.is_exhausted]
    total_active = sum((lot.remaining_amount for lot in active_lots), Decimal("0.0"))

    summary.total_active_balance = round(total_active, 2)
    summary.total_deposited = round(total_deposited, 2)
    summary.total_consumed = round(total_consumed, 2)
    summary.total_expired = round(summary.total_expired, 2)
    summary.total_uncovered = round(total_uncovered, 2)
    summary.active_lots = active_lots
    summary.allocations = allocations

    return summary
