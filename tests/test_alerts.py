"""Tests for ProsumerAlertManager and early warning system.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 10 & 11.
Kryteria akceptacji:
- Wykrywanie przerw w danych > 48h.
- Wykrywanie wygasających lotów depozytu (ostrzeżenie przed upływem 12 miesięcy).
- Wykrywanie niezaakceptowanych rozbieżności faktur.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from custom_components.energa_mobile.ha.alerts import ProsumerAlertManager, AlertItem
from custom_components.energa_mobile.core.settlement.models import SettlementLot


def test_alert_data_freshness():
    """Verify data freshness checking (> 48h triggers alert)."""
    storage_mock = MagicMock()
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)

    # 1. Fresh readings (12 hours old)
    storage_mock.get_latest_reading_time.return_value = now - timedelta(hours=12)
    mgr = ProsumerAlertManager(storage=storage_mock)
    alert = mgr.check_data_freshness("PL_001", reference_time=now)
    assert alert is None

    # 2. Stale readings (55 hours old)
    storage_mock.get_latest_reading_time.return_value = now - timedelta(hours=55)
    alert_stale = mgr.check_data_freshness("PL_001", reference_time=now)
    assert alert_stale is not None
    assert alert_stale.alert_type == "data_staleness"
    assert alert_stale.severity == "warning"
    assert alert_stale.details["staleness_hours"] == 55.0

    # 3. No readings at all
    storage_mock.get_latest_reading_time.return_value = None
    alert_none = mgr.check_data_freshness("PL_001", reference_time=now)
    assert alert_none is not None
    assert alert_none.alert_type == "no_readings"


def test_alert_expiring_lots():
    """Verify detection of lots expiring within 30 days (12-month prosumer rule)."""
    storage_mock = MagicMock()
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)

    lots = [
        # Lot 1: expires in 5 days -> critical alert
        SettlementLot(
            lot_id="LOT_01",
            ppe_id="PL_001",
            unit="PLN",
            zone="total",
            original_amount=Decimal("350.00"),
            remaining_amount=Decimal("120.00"),
            created_at_utc=now - timedelta(days=360),
            assigned_at=(now - timedelta(days=360)).date(),
            expires_at=(now + timedelta(days=5)).date(),
        ),
        # Lot 2: expires in 25 days -> warning alert
        SettlementLot(
            lot_id="LOT_02",
            ppe_id="PL_001",
            unit="kWh",
            zone="total",
            original_amount=Decimal("500.00"),
            remaining_amount=Decimal("200.00"),
            created_at_utc=now - timedelta(days=340),
            assigned_at=(now - timedelta(days=340)).date(),
            expires_at=(now + timedelta(days=25)).date(),
        ),
        # Lot 3: expires in 90 days -> no alert (safe)
        SettlementLot(
            lot_id="LOT_03",
            ppe_id="PL_001",
            unit="PLN",
            zone="total",
            original_amount=Decimal("200.00"),
            remaining_amount=Decimal("200.00"),
            created_at_utc=now - timedelta(days=275),
            assigned_at=(now - timedelta(days=275)).date(),
            expires_at=(now + timedelta(days=90)).date(),
        ),
    ]

    storage_mock.get_settlement_lots.return_value = lots
    mgr = ProsumerAlertManager(storage=storage_mock)

    alerts = mgr.check_expiring_lots("PL_001", reference_time=now)
    assert len(alerts) == 2

    # Check first alert (5 days)
    assert alerts[0].severity == "critical"
    assert alerts[0].details["days_left"] == 5
    assert alerts[0].details["unit"] == "PLN"

    # Check second alert (25 days)
    assert alerts[1].severity == "warning"
    assert alerts[1].details["days_left"] == 25
    assert alerts[1].details["unit"] == "kWh"


def test_alert_pending_invoice_variances():
    """Verify alert when an unapproved invoice has status DISCREPANCY."""
    storage_mock = MagicMock()
    storage_mock._connection = MagicMock()
    cur_mock = MagicMock()
    storage_mock._connection.cursor.return_value = cur_mock

    cur_mock.execute.return_value.fetchall.return_value = [
        ("FAK/2026/01", 1250.00, 1315.50, 65.50, 5.24, "DISCREPANCY")
    ]

    mgr = ProsumerAlertManager(storage=storage_mock)
    alerts = mgr.check_pending_invoice_variances("PL_001")

    assert len(alerts) == 1
    assert alerts[0].alert_type == "unapproved_invoice_discrepancy"
    assert alerts[0].details["variance_gross"] == 65.50
    assert alerts[0].details["variance_percent"] == 5.24
