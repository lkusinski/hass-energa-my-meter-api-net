"""Domain models for PSE market energy prices (RCEm and RCE).

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 6, 7 & 8.
Amounts are exact Decimals.
Explicit applicable month, publication date, and revision tracking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class MarketPriceRecord:
    """An official market price record published by PSE (OIRE)."""

    price_type: str                         # "RCEM" or "RCE"
    applicable_year: int
    applicable_month: int
    publication_date: date
    revision: int = 1
    price_mwh: Decimal = Decimal("0.0")
    price_kwh: Decimal = Decimal("0.0")
    source_url: str = "https://www.pse.pl/oire/rcem-rynkowa-miesieczna-cena-energii-elektrycznej"
    is_correction: bool = False
    raw_snippet: str = ""

    @property
    def price_with_vat_multiplier(self) -> Decimal:
        """Standard 1.23 multiplier for net-billing valuation."""
        return round(self.price_kwh * Decimal("1.23"), 5)
