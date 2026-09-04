"""Domain models for versioned, effective-dated tariffs and invoice reconciliation.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 7 & 8.
Solves anti-pattern:
- No hardcoded eternal constants; rates have valid_from/valid_to and source document provenance.
- Strict distinction between deposit-eligible lines (energy purchase) and ineligible lines (distribution).
- Per-line invoice reconciliation with variance auditing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class TariffRate:
    """An individual rate with effective validity dates and legal provenance."""

    rate_id: str                           # e.g. "energy_day", "energy_night", "grid_fixed", "capacity"
    name: str                              # Polish description on invoice
    unit: str                              # "PLN/kWh", "PLN/month", "PLN/MWh"
    rate_net: Decimal
    valid_from: date
    valid_to: date | None = None
    source_document: str = ""              # e.g. "Taryfa Energa-Operator 2026", "Informacja URE 58/2025"
    is_deposit_eligible: bool = False      # True ONLY for active energy purchase; False for distribution/fixed

    def is_effective_on(self, d: date) -> bool:
        if d < self.valid_from:
            return False
        if self.valid_to and d > self.valid_to:
            return False
        return True


@dataclass
class TariffPlan:
    """A complete tariff definition composed of effective-dated rates."""

    tariff_code: str                       # e.g. "G11", "G12w", "G12"
    description: str
    rates: dict[str, list[TariffRate]] = field(default_factory=dict)
    vat_rate: Decimal = Decimal("0.23")

    def get_rate(self, rate_id: str, on_date: date) -> TariffRate | None:
        """Find the effective rate for a given date."""
        candidates = self.rates.get(rate_id, [])
        for r in candidates:
            if r.is_effective_on(on_date):
                return r
        return None


@dataclass(frozen=True)
class InvoiceLineItem:
    """A calculated or invoiced line item."""

    line_id: str
    rate_id: str
    name: str
    quantity: Decimal
    unit: str
    unit_price_net: Decimal
    total_net: Decimal
    vat_rate: Decimal
    total_gross: Decimal
    is_deposit_eligible: bool


@dataclass
class InvoiceReconciliation:
    """Audit report comparing calculated projection against seller's actual invoice."""

    invoice_number: str
    period_start: date
    period_end: date
    computed_gross: Decimal
    invoiced_gross: Decimal
    variance_gross: Decimal
    variance_percent: Decimal
    line_variances: list[dict] = field(default_factory=list)
    status: str = "MATCH"                  # "MATCH", "WITHIN_TOLERANCE", "DISCREPANCY"
    notes: str = ""
