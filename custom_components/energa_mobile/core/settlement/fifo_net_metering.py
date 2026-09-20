"""Pure domain FIFO settlement engine for Net-Metering (Physical Energy Ledger, kWh).

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 6, 7 & 8.
- Amounts in exact Decimal.
- Expiry: 12 rolling months from the end of introduction month M (i.e. end of M+12).
- Allocation order: oldest unexpired lot first (FIFO).
- Full provenance and allocation tracing.

NOTE (audyt P2.1): this is now a thin presentation layer over the SHARED engine
`core/settlement/fifo_engine.py` — the same allocation algorithm that production
(`settlement.fifo_kwh_bank`) uses. It no longer re-implements FIFO, so tests and
production can no longer drift apart. The engine output is pinned byte-for-byte
by `tests/test_fifo_golden.py`.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from decimal import Decimal

from .fifo_engine import fifo_kwh_bank_trace
from .models import LotAllocation, SettlementLot, SettlementSummary


def _month_end(year: int, month: int) -> date:
    """Return the last calendar day of a given year and month."""
    days = calendar.monthrange(year, month)[1]
    return date(year, month, days)


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Add delta months to year/month."""
    total_m = (year * 12 + (month - 1)) + delta
    return (total_m // 12, (total_m % 12) + 1)


def _dec(value) -> Decimal:
    """Exact Decimal from a float/int/str engine value."""
    return Decimal(str(value))


def run_fifo_net_metering(
    ppe_id: str,
    monthly_flows: list[dict],
    coefficient: Decimal = Decimal("0.8"),
    today: date | None = None,
    zone: str = "total",
) -> SettlementSummary:
    """Run FIFO allocation for the net-metering physical energy warehouse.

    Delegates the allocation to `fifo_engine.fifo_kwh_bank_trace` (shared with
    production) and wraps the resulting trace in the rich domain models.

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

    rows = []
    for flow in monthly_flows or []:
        rows.append(
            (
                flow["year"],
                flow["month"],
                flow.get("import_kwh", "0.0"),
                flow.get("export_kwh", "0.0"),
            )
        )

    _bank, detail, trace = fifo_kwh_bank_trace(rows, coefficient, today=as_of)

    lot_models: dict[int, SettlementLot] = {}
    for lot in trace["lots"]:
        y, m = int(lot["year"]), int(lot["month"])
        exp_y, exp_m = _add_months(y, m, 12)
        expired = lot["expired_kwh"] > 0.0
        model = SettlementLot(
            lot_id=f"lot_{ppe_id}_{zone}_{y}_{m:02d}",
            ppe_id=ppe_id,
            unit="kWh",
            zone=zone,
            original_amount=_dec(lot["credited_kwh"]),
            remaining_amount=Decimal("0.0") if expired else _dec(max(0.0, lot["remaining_kwh"])),
            created_at_utc=datetime.now(timezone.utc),
            assigned_at=_month_end(y, m),
            expires_at=_month_end(exp_y, exp_m),
            rule_version="net_metering_fifo_12m",
            provenance=(
                f"Export credited {lot['credited_kwh']} kWh in {y}-{m:02d} "
                f"(coeff {coefficient})"
            ),
        )
        lot_models[id(lot)] = model

    allocations: list[LotAllocation] = []
    consumed = 0.0
    for alloc in trace["allocations"]:
        take = alloc["amount_kwh"]
        consumed += take
        lot = lot_models[id(alloc["lot"])]
        allocations.append(
            LotAllocation(
                allocation_id=f"alloc_{lot.lot_id}_{alloc['year']}_{alloc['month']:02d}",
                lot_id=lot.lot_id,
                consumption_target_id=(
                    f"imp_{ppe_id}_{zone}_{alloc['year']}_{alloc['month']:02d}"
                ),
                allocated_amount=_dec(take),
                allocated_at_utc=datetime.now(timezone.utc),
                notes=f"Covered import {alloc['year']}-{alloc['month']:02d}",
            )
        )

    all_lots = list(lot_models.values())
    active_lots = [lot for lot in all_lots if not lot.is_exhausted]

    summary.total_active_balance = _dec(_bank)
    summary.total_deposited = _dec(detail["deposits_kwh"])
    summary.total_consumed = _dec(round(consumed, 2))
    summary.total_expired = _dec(detail["expired_kwh"])
    summary.total_uncovered = _dec(detail["uncovered_kwh"])
    summary.active_lots = active_lots
    summary.allocations = allocations

    return summary


def run_dual_zone_fifo_net_metering(
    ppe_id: str,
    monthly_flows_l1: list[dict],
    monthly_flows_l2: list[dict],
    coefficient: Decimal = Decimal("0.8"),
    today: date | None = None,
) -> tuple[SettlementSummary, SettlementSummary, Decimal]:
    """Run FIFO allocation for dual-zone net-metering physical energy warehouse.

    Args:
        ppe_id: Logical delivery point ID.
        monthly_flows_l1: Monthly flows for Zone 1 (Day/Peak).
        monthly_flows_l2: Monthly flows for Zone 2 (Night/Off-peak).
        coefficient: Prosumer discount factor (e.g. 0.8 for <=10kW, 0.7 for >10kW).
        today: Evaluation reference date.

    Returns:
        tuple (summary_l1, summary_l2, total_active_balance)
    """
    summary_1 = run_fifo_net_metering(
        ppe_id, monthly_flows_l1, coefficient=coefficient, today=today, zone="day"
    )
    summary_2 = run_fifo_net_metering(
        ppe_id, monthly_flows_l2, coefficient=coefficient, today=today, zone="night"
    )
    total_active = round(summary_1.total_active_balance + summary_2.total_active_balance, 2)
    return summary_1, summary_2, total_active
