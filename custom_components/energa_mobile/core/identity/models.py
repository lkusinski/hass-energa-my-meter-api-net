"""Domain models for PPE and meter lifecycle (pure domain, zero external dependencies).

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 4 & 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class SettlementType(str, Enum):
    """Explicit settlement system type for a PPE point."""

    NET_METERING = "net_metering"          # Stary system: opusty (0.8 / 0.7), magazyn kWh
    NET_BILLING_RCEM = "net_billing_rcem"  # Nowy system: RCEm miesieczna cena rynkowa, depozyt PLN
    NET_BILLING_RCE = "net_billing_rce"    # Nowy system: RCE godzinowa / 15-min cena rynkowa
    CONSUMER = "consumer"                  # Czysty odbiorca bez mikroinstalacji (import-only)


@dataclass(frozen=True)
class PPE:
    """Logical Point of Delivery (Punkt Poboru Energii).

    PPE is the stable, overarching identity that survives physical meter replacements.
    One PPE may have multiple physical meters over time.
    """

    ppe_id: str
    customer_label: str = ""
    settlement_type: SettlementType = SettlementType.CONSUMER
    prosumer_coefficient: Decimal = Decimal("0.8")
    timezone: str = "Europe/Warsaw"
    effective_from: date | None = None
    effective_to: date | None = None


@dataclass(frozen=True)
class MeterLifecycle:
    """Lifecycle record for a physical meter attached to a PPE.

    Handles meter replacements, registers, zones and initial/boundary offsets.
    """

    ppe_id: str
    meter_id: str
    serial: str
    register: str                          # e.g. "1.8.0", "1.8.1", "1.8.2", "2.8.0"
    zone: str = "total"                    # "total", "day", "night"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    offset_kwh: Decimal = Decimal("0.0")   # Offset across meter replacement
    source: str = "energa"
