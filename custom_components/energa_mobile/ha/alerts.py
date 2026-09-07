"""Prosumer Health and Alerting System for Home Assistant.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 10 & 11.
Invariants:
- Detects data staleness (>48h without canonical interval readings).
- Detects expiring FIFO lots (12-month legal expiry warning at 30 days).
- Detects unreviewed / unapproved invoice variances.
- Never raises exceptions that crash coordinator or sensor updates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging
from typing import Any

from ..storage.sqlite.database import CanonicalStorage

_LOGGER = logging.getLogger(__name__)


@dataclass
class AlertItem:
    """Represents a prosumer health alert item."""
    alert_type: str
    severity: str  # "info", "warning", "critical"
    title: str
    message: str
    created_at_utc: datetime = datetime.now(timezone.utc)
    details: dict[str, Any] | None = None


class ProsumerAlertManager:
    """Monitors canonical storage and settlement status to raise prosumer alerts."""

    def __init__(self, storage: CanonicalStorage | None = None) -> None:
        """Initialize alert manager."""
        self.storage = storage

    def check_data_freshness(
        self,
        ppe_id: str,
        max_staleness_hours: int = 48,
        reference_time: datetime | None = None,
    ) -> AlertItem | None:
        """Verify if readings have arrived within the expected staleness window."""
        if not self.storage:
            return None

        now = reference_time or datetime.now(timezone.utc)
        latest_dt = self.storage.get_latest_reading_time(ppe_id)

        if latest_dt is None:
            return AlertItem(
                alert_type="no_readings",
                severity="warning",
                title="Brak odczytów w bazie",
                message=f"Dla punktu poboru {ppe_id} nie znaleziono jeszcze żadnych odczytów.",
                details={"ppe_id": ppe_id},
            )

        if latest_dt.tzinfo is None:
            latest_dt = latest_dt.replace(tzinfo=timezone.utc)

        age = now - latest_dt
        age_hours = age.total_seconds() / 3600.0

        if age_hours > max_staleness_hours:
            return AlertItem(
                alert_type="data_staleness",
                severity="warning",
                title="Opóźnienie w odczytach z licznika",
                message=(
                    f"Ostatni odczyt dla {ppe_id} pochodzi z {latest_dt.strftime('%Y-%m-%d %H:%M UTC')} "
                    f"({int(age_hours)} godz. temu). Energa Operator może mieć opóźnienie w publikacji."
                ),
                details={
                    "ppe_id": ppe_id,
                    "latest_reading_utc": latest_dt.isoformat(),
                    "staleness_hours": round(age_hours, 1),
                },
            )

        return None

    def check_expiring_lots(
        self,
        ppe_id: str,
        days_ahead: int = 30,
        reference_time: datetime | None = None,
    ) -> list[AlertItem]:
        """Detect FIFO lots (kWh or PLN) approaching the 12-month legal expiration."""
        if not self.storage:
            return []

        now = reference_time or datetime.now(timezone.utc)
        now_date = now.date() if isinstance(now, datetime) else now
        deadline_date = (now + timedelta(days=days_ahead)).date() if isinstance(now, datetime) else (now + timedelta(days=days_ahead))
        alerts: list[AlertItem] = []

        try:
            active_lots = self.storage.get_settlement_lots(ppe_id, include_exhausted=False)
        except Exception as err:
            _LOGGER.debug("Could not query settlement lots: %s", err)
            return []

        for lot in active_lots:
            if not lot.expires_at:
                continue

            exp = lot.expires_at
            exp_date = exp.date() if isinstance(exp, datetime) else exp

            if exp_date <= deadline_date:
                days_left = max(0, (exp_date - now_date).days)
                unit_label = "kWh" if lot.unit == "kWh" else "zł"
                assigned_str = lot.assigned_at.strftime("%Y-%m-%d") if hasattr(lot.assigned_at, "strftime") else str(lot.assigned_at)
                exp_str = exp_date.strftime("%Y-%m-%d") if hasattr(exp_date, "strftime") else str(exp_date)
                alerts.append(
                    AlertItem(
                        alert_type="expiring_lot",
                        severity="critical" if days_left <= 7 else "warning",
                        title=f"Wygasający depozyt prosumencki ({lot.unit})",
                        message=(
                            f"Lot z dnia {assigned_str} w kwocie/ilości "
                            f"{lot.remaining_amount} {unit_label} wygasa za {days_left} dni ({exp_str})."
                        ),
                        details={
                            "lot_id": lot.lot_id,
                            "remaining_amount": float(lot.remaining_amount),
                            "unit": lot.unit,
                            "expires_at": exp_str,
                            "days_left": days_left,
                        },
                    )
                )

        return alerts

    def check_pending_invoice_variances(self, ppe_id: str) -> list[AlertItem]:
        """Detect unapproved invoice reconciliations with discrepancies."""
        if not self.storage:
            return []

        alerts: list[AlertItem] = []
        try:
            # Query reconciliations with status DISCREPANCY and approved = False
            cur = self.storage._connection.cursor()
            rows = cur.execute(
                """SELECT invoice_number, computed_gross, invoiced_gross, variance_gross, variance_percent, status
                   FROM invoice_reconciliation
                   WHERE ppe_id = ? AND approved = 0 AND status = 'DISCREPANCY'
                """,
                (ppe_id,),
            ).fetchall()

            for row in rows:
                inv_num, comp, invoiced, var, pct, status = row
                alerts.append(
                    AlertItem(
                        alert_type="unapproved_invoice_discrepancy",
                        severity="warning",
                        title=f"Nierozliczona rozbieżność faktury {inv_num}",
                        message=(
                            f"Faktura {inv_num} wykazuje rozbieżność {var} zł ({pct}%). "
                            f"Wyliczona kwota: {comp} zł, na fakturze: {invoiced} zł. Wymaga weryfikacji."
                        ),
                        details={
                            "invoice_number": inv_num,
                            "computed_gross": comp,
                            "invoiced_gross": invoiced,
                            "variance_gross": var,
                            "variance_percent": pct,
                        },
                    )
                )
        except Exception as err:
            _LOGGER.debug("Could not query invoice reconciliations: %s", err)

        return alerts

    def get_all_alerts(
        self,
        ppe_id: str,
        reference_time: datetime | None = None,
    ) -> list[AlertItem]:
        """Run all alert checks for a PPE and return unified list."""
        all_alerts: list[AlertItem] = []

        freshness = self.check_data_freshness(ppe_id, reference_time=reference_time)
        if freshness:
            all_alerts.append(freshness)

        all_alerts.extend(self.check_expiring_lots(ppe_id, reference_time=reference_time))
        all_alerts.extend(self.check_pending_invoice_variances(ppe_id))

        return all_alerts
