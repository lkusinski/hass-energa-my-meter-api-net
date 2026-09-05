"""Domain models for settlement lots, FIFO allocations, and ledger invariant tracking.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 7, 8 & 9.
All amounts are exact Decimals.
Physical ledger (kWh) and Monetary ledger (PLN) are kept strictly separate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass
class SettlementLot:
    """An individual credited energy (kWh) or monetary deposit (PLN) lot."""

    lot_id: str
    ppe_id: str
    unit: str                               # "kWh" or "PLN"
    zone: str                               # "total", "day", "night"
    original_amount: Decimal
    remaining_amount: Decimal
    created_at_utc: datetime
    assigned_at: date
    expires_at: date
    rule_version: str = "v1"
    provenance: str = ""

    def __post_init__(self) -> None:
        if self.unit not in ("kWh", "PLN"):
            raise ValueError(
                f"Ledger invariant violation: Invalid unit '{self.unit}', expected 'kWh' or 'PLN'"
            )

    @property
    def is_exhausted(self) -> bool:
        return self.remaining_amount <= Decimal("0.0")

    def is_expired(self, as_of: date) -> bool:
        return self.expires_at < as_of


@dataclass(frozen=True)
class LotAllocation:
    """Record of consumption consuming an unexpired lot via FIFO."""

    allocation_id: str
    lot_id: str
    consumption_target_id: str
    allocated_amount: Decimal
    allocated_at_utc: datetime
    is_reversal: bool = False
    notes: str = ""


@dataclass
class SettlementSummary:
    """Summary of settlement state derived deterministically from lots and allocations."""

    unit: str
    total_active_balance: Decimal = Decimal("0.0")
    total_deposited: Decimal = Decimal("0.0")
    total_consumed: Decimal = Decimal("0.0")
    total_expired: Decimal = Decimal("0.0")
    total_uncovered: Decimal = Decimal("0.0")
    active_lots: list[SettlementLot] = field(default_factory=list)
    allocations: list[LotAllocation] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.unit not in ("kWh", "PLN"):
            raise ValueError(
                f"Ledger invariant violation: Invalid summary unit '{self.unit}', expected 'kWh' or 'PLN'"
            )

