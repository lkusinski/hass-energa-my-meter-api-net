"""Unit tests for pure settlement engines (Net-Metering and Net-Billing FIFO).

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 6, 7 & 8.
- Physical energy ledger (kWh): FIFO 12m rolling expiry.
- Monetary ledger (PLN): FIFO 12m expiry from assigned_at, eligible line items only.
- Strict Decimal precision.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from custom_components.energa_mobile.core.settlement.fifo_net_billing import (
    InvoiceLineCharge,
    run_fifo_net_billing,
)
from custom_components.energa_mobile.core.settlement.fifo_net_metering import (
    run_fifo_net_metering,
)
from custom_components.energa_mobile.core.settlement.models import SettlementLot


class TestNetMeteringPureFIFO:
    def test_basic_fifo_consumption(self):
        # 3 months of flows:
        # Jan 2025: export 1000 kWh -> 800 kWh credited
        # Feb 2025: import 300 kWh -> consumed from Jan
        # Mar 2025: import 200 kWh -> consumed from Jan
        # Balance = 300 kWh
        flows = [
            {"year": 2025, "month": 1, "import_kwh": "0.0", "export_kwh": "1000.0"},
            {"year": 2025, "month": 2, "import_kwh": "300.0", "export_kwh": "0.0"},
            {"year": 2025, "month": 3, "import_kwh": "200.0", "export_kwh": "0.0"},
        ]
        res = run_fifo_net_metering(
            ppe_id="PL_TEST",
            monthly_flows=flows,
            coefficient=Decimal("0.8"),
            today=date(2025, 4, 1),
        )
        assert res.total_deposited == Decimal("800.0")
        assert res.total_consumed == Decimal("500.0")
        assert res.total_active_balance == Decimal("300.0")
        assert res.total_expired == Decimal("0.0")
        assert len(res.allocations) == 2

    def test_twelve_month_expiry(self):
        # Jan 2025 export credited (expires Jan 31, 2026)
        # In Feb 2026 (after Jan 31, 2026), unconsumed balance must expire
        flows = [
            {"year": 2025, "month": 1, "import_kwh": "0.0", "export_kwh": "1000.0"},
            {"year": 2025, "month": 6, "import_kwh": "200.0", "export_kwh": "0.0"},
        ]
        # Evaluated on 2026-01-15 (still valid)
        res_valid = run_fifo_net_metering(
            ppe_id="PL_TEST",
            monthly_flows=flows,
            coefficient=Decimal("0.8"),
            today=date(2026, 1, 15),
        )
        assert res_valid.total_active_balance == Decimal("600.0")
        assert res_valid.total_expired == Decimal("0.0")

        # Evaluated on 2026-02-01 (expired)
        res_expired = run_fifo_net_metering(
            ppe_id="PL_TEST",
            monthly_flows=flows,
            coefficient=Decimal("0.8"),
            today=date(2026, 2, 1),
        )
        assert res_expired.total_active_balance == Decimal("0.0")
        assert res_expired.total_expired == Decimal("600.0")


class TestNetBillingPureFIFO:
    def test_fifo_allocates_only_to_eligible_energy_lines(self):
        # Lot 1: 500 PLN assigned 2026-06-01
        lot1 = SettlementLot(
            lot_id="lot_001",
            ppe_id="PL_TEST",
            unit="PLN",
            zone="total",
            original_amount=Decimal("500.00"),
            remaining_amount=Decimal("500.00"),
            created_at_utc=datetime.now(timezone.utc),
            assigned_at=date(2026, 6, 1),
            expires_at=date(2027, 6, 1),
        )

        # Invoice charges:
        # 1. Active energy purchase (eligible): 123.00 PLN gross
        # 2. Distribution variable (ineligible): 65.00 PLN gross
        # 3. Distribution fixed & capacity (ineligible): 45.00 PLN gross
        charges = [
            InvoiceLineCharge(
                line_id="line_sale",
                description="Sprzedaż energii elektrycznej",
                amount_gross=Decimal("123.00"),
                is_deposit_eligible=True,
                period_start=date(2026, 7, 1),
                period_end=date(2026, 7, 31),
            ),
            InvoiceLineCharge(
                line_id="line_distr_var",
                description="Dystrybucja zmienna",
                amount_gross=Decimal("65.00"),
                is_deposit_eligible=False,
                period_start=date(2026, 7, 1),
                period_end=date(2026, 7, 31),
            ),
            InvoiceLineCharge(
                line_id="line_distr_fix",
                description="Opłaty stałe i mocowa",
                amount_gross=Decimal("45.00"),
                is_deposit_eligible=False,
                period_start=date(2026, 7, 1),
                period_end=date(2026, 7, 31),
            ),
        ]

        summary, invoice_res = run_fifo_net_billing(
            ppe_id="PL_TEST",
            deposit_lots=[lot1],
            charges=charges,
            today=date(2026, 8, 1),
        )

        # Invariant checks:
        # Deposit should ONLY cover line_sale (123.00)
        assert invoice_res[0]["deposit_applied"] == Decimal("123.00")
        assert invoice_res[0]["remaining_due"] == Decimal("0.00")

        # Distribution MUST NOT be covered by deposit
        assert invoice_res[1]["deposit_applied"] == Decimal("0.00")
        assert invoice_res[1]["remaining_due"] == Decimal("65.00")

        assert invoice_res[2]["deposit_applied"] == Decimal("0.00")
        assert invoice_res[2]["remaining_due"] == Decimal("45.00")

        # Total remaining deposit balance = 500 - 123 = 377 PLN
        assert summary.total_consumed == Decimal("123.00")
        assert summary.total_active_balance == Decimal("377.00")

    def test_net_billing_expiry_and_cash_out(self):
        # Deposit lot expired 2026-05-01
        lot_old = SettlementLot(
            lot_id="lot_old",
            ppe_id="PL_TEST",
            unit="PLN",
            zone="total",
            original_amount=Decimal("1000.00"),
            remaining_amount=Decimal("1000.00"),
            created_at_utc=datetime.now(timezone.utc),
            assigned_at=date(2025, 5, 1),
            expires_at=date(2026, 5, 1),
        )

        summary, _ = run_fifo_net_billing(
            ppe_id="PL_TEST",
            deposit_lots=[lot_old],
            charges=[],
            today=date(2026, 6, 1),
            cash_out_percent=Decimal("20.0"),
        )
        assert summary.total_active_balance == Decimal("0.00")
        assert summary.total_expired == Decimal("1000.00")
