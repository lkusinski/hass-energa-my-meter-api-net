"""Faza 3 tests: opening warehouse (net-metering) and deposit (net-billing).

Pure helpers live in ``core.verification``:
- ``opening_bank_from_monthly_flows`` replays the net-metering FIFO 12-month
  rule over the monthly flows that precede a period;
- ``deposit_ledger_close`` replays a net-billing PLN deposit ledger.

The service tests prove that ``verify_period`` wires the reconstructed
warehouse into ``build_period_invoice`` (the live Wiśniowa bug) and honours
the manual ``bank_open_*`` / ``deposit_open_pln`` overrides.
"""

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energa_mobile.const import (
    CONF_PROSUMER_COEFFICIENT,
    DOMAIN,
)
from custom_components.energa_mobile.core.verification import (
    deposit_ledger_close,
    opening_bank_from_monthly_flows,
)
from custom_components.energa_mobile.services import async_verify_period_data

TZ = ZoneInfo("Europe/Warsaw")


class TestOpeningBankFromMonthlyFlows:
    def test_single_zone_fifo_at_period_start(self):
        # intro 200*.8=160 - imp 100 = 60; +150*.8=120 - 50 = 130;
        # +100*.8=80 - 80 = 130.
        monthly = {
            (2025, 5): {"import": 100.0, "export": 200.0},
            (2025, 6): {"import": 50.0, "export": 150.0},
            (2025, 7): {"import": 80.0, "export": 100.0},
        }
        bank_1, bank_2, detail = opening_bank_from_monthly_flows(
            monthly, 0.8, has_zones=False, period_start=date(2025, 8, 1)
        )
        assert bank_1 == 130.0
        assert bank_2 == 0.0
        assert detail["has_data"] is True
        assert detail["months_used"] == 3

    def test_dual_zone_is_independent(self):
        monthly = {
            (2026, 7): {
                "import_1": 0.0,
                "export_1": 100.0,
                "import_2": 0.0,
                "export_2": 50.0,
            }
        }
        bank_1, bank_2, _ = opening_bank_from_monthly_flows(
            monthly, 0.8, has_zones=True, period_start=date(2026, 8, 1)
        )
        assert bank_1 == 80.0
        assert bank_2 == 40.0

    def test_explicit_initial_balances_are_added(self):
        monthly = {(2026, 7): {"import": 0.0, "export": 100.0}}
        bank_1, bank_2, _ = opening_bank_from_monthly_flows(
            monthly,
            0.8,
            has_zones=False,
            period_start=date(2026, 8, 1),
            initial_1=5.0,
            initial_2=3.0,
        )
        assert bank_1 == 85.0
        assert bank_2 == 3.0

    def test_no_history_returns_none(self):
        bank_1, bank_2, detail = opening_bank_from_monthly_flows(
            {}, 0.8, has_zones=False, period_start=date(2026, 8, 1)
        )
        assert bank_1 is None
        assert bank_2 is None
        assert detail["has_data"] is False

    def test_energy_older_than_12_months_expires(self):
        monthly = {(2024, 1): {"import": 0.0, "export": 1000.0}}
        bank_1, _, _ = opening_bank_from_monthly_flows(
            monthly, 0.8, has_zones=False, period_start=date(2025, 8, 1)
        )
        assert bank_1 == 0.0

    def test_accepts_datetime_period_start(self):
        monthly = {(2026, 7): {"import": 0.0, "export": 100.0}}
        bank_1, _, _ = opening_bank_from_monthly_flows(
            monthly,
            0.8,
            has_zones=False,
            period_start=datetime(2026, 8, 1, tzinfo=TZ),
        )
        assert bank_1 == 80.0

    def test_garbage_rows_are_skipped(self):
        monthly = {
            "junk": {"import": 1.0},
            (2026, 7): {"import": "junk", "export": 100.0},
        }
        bank_1, _, detail = opening_bank_from_monthly_flows(
            monthly, 0.8, has_zones=False, period_start=date(2026, 8, 1)
        )
        assert bank_1 == 80.0
        assert detail["has_data"] is True


class TestDepositLedgerClose:
    def test_replays_generation_and_usage(self):
        # 100-30=70; +50-120=0; +20-0=20.
        entries = [(100.0, 30.0), (50.0, 200.0), (20.0, 0.0)]
        assert deposit_ledger_close(entries) == 20.0

    def test_initial_balance(self):
        assert deposit_ledger_close([], initial=10.0) == 10.0
        assert deposit_ledger_close([(5.0, 100.0)], initial=10.0) == 0.0

    def test_defensive_inputs(self):
        assert deposit_ledger_close(None) == 0.0
        assert deposit_ledger_close([("junk", 1.0), (None, None)]) == 0.0
        assert deposit_ledger_close([(10.0, 3.0)], initial="junk") == 7.0


class TestCollectMonthlyFlows:
    @pytest.mark.asyncio
    async def test_buckets_daily_rows_by_month(self):
        from custom_components.energa_mobile import services as svc

        hass = MagicMock()
        fake_stats = {
            "id_import": [
                {"start": datetime(2026, 7, 1, tzinfo=timezone.utc), "state": 3.0},
                {"start": datetime(2026, 7, 2, tzinfo=timezone.utc), "state": 4.0},
                {"start": datetime(2026, 8, 1, tzinfo=timezone.utc), "state": 9.0},
            ],
            "id_export": [
                {"start": datetime(2026, 7, 3, tzinfo=timezone.utc), "state": 5.0},
            ],
        }
        with patch.object(
            svc,
            "_statistic_id_for",
            side_effect=lambda hass, mid, serial, suffix: f"id_{suffix}",
        ), patch.object(svc, "get_instance") as gi:
            gi.return_value.async_add_executor_job = AsyncMock(
                return_value=fake_stats
            )
            monthly = await svc._collect_monthly_flows(
                hass,
                _prosumer_meter(),
                datetime(2026, 9, 1, tzinfo=TZ),
            )

        assert monthly[(2026, 7)]["import"] == 7.0
        assert monthly[(2026, 8)]["import"] == 9.0
        assert monthly[(2026, 7)]["export"] == 5.0

    @pytest.mark.asyncio
    async def test_recorder_failure_returns_empty(self):
        from custom_components.energa_mobile import services as svc

        with patch.object(
            svc,
            "_statistic_id_for",
            side_effect=lambda hass, mid, serial, suffix: f"id_{suffix}",
        ), patch.object(svc, "get_instance") as gi:
            gi.return_value.async_add_executor_job = AsyncMock(
                side_effect=RuntimeError("boom")
            )
            monthly = await svc._collect_monthly_flows(
                MagicMock(),
                _prosumer_meter(),
                datetime(2026, 9, 1, tzinfo=TZ),
            )
        assert monthly == {}


def _hass_with_meter(meter: dict, options: dict):
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = options
    coordinator = MagicMock()
    coordinator.data = [meter]
    coordinator._verify_cache = {}
    coordinator._rce_cache = None
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    hass.data = {
        DOMAIN: {"entry_1": {"coordinator": coordinator, "api": MagicMock()}}
    }
    return hass, entry


def _prosumer_meter() -> dict:
    return {
        "meter_point_id": "1",
        "meter_serial": "S1",
        "zone_count": 1,
        "total_plus": 10.0,
        "total_minus": 5.0,
        "is_prosumer": True,
        "tariff": "G12W",
    }


class TestVerifyPeriodOpeningBalances:
    @pytest.mark.asyncio
    async def test_net_metering_bank_open_reconstructed_and_used(self):
        hass, _ = _hass_with_meter(
            _prosumer_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8}
        )
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value={"import_1": {0: 100.0}, "export_1": {0: 0.0}}),
        ), patch(
            "custom_components.energa_mobile.services._collect_monthly_flows",
            new=AsyncMock(
                return_value={(2026, 7): {"import": 0.0, "export": 200.0}}
            ),
        ):
            res = await async_verify_period_data(
                hass,
                {"start": "2026-08-01", "end": "2026-08-31", "meter_id": "1"},
            )
        assert res["old_system"] is True
        # bank_open = 200 * 0.8 = 160; cover = min(160, saldo_plus 100) = 100.
        assert res["kwh"]["bank_open_1"] == 160.0
        assert res["kwh"]["cover_1"] == 100.0
        assert res["coverage_unknown"] is False

    @pytest.mark.asyncio
    async def test_manual_bank_open_override_skips_history(self):
        hass, _ = _hass_with_meter(
            _prosumer_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8}
        )
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value={"import_1": {0: 100.0}}),
        ), patch(
            "custom_components.energa_mobile.services._collect_monthly_flows",
            new=AsyncMock(return_value={}),
        ) as monthly_mock:
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "meter_id": "1",
                    "bank_open_1": 40.0,
                },
            )
        monthly_mock.assert_not_awaited()
        assert res["kwh"]["bank_open_1"] == 40.0
        assert res["kwh"]["cover_1"] == 40.0

    @pytest.mark.asyncio
    async def test_net_billing_deposit_open_override(self):
        hass, _ = _hass_with_meter(
            _prosumer_meter(), {CONF_PROSUMER_COEFFICIENT: 0.0}
        )
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(
                return_value={"import_1": {0: 10.0}, "export_1": {0: 100.0}}
            ),
        ):
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "meter_id": "1",
                    "deposit_open_pln": 50.0,
                    "rcem_pln": 0.3,
                },
            )
        assert res["old_system"] is False
        assert res["deposit_open"] == 50.0
        assert res["coverage_unknown"] is False
        # export (saldo ujemne) 90 * 0.3 * 1.23 = 33.21
        assert res["deposit_generated"] == 33.21

    @pytest.mark.asyncio
    async def test_net_billing_without_opening_flags_unknown(self):
        hass, _ = _hass_with_meter(
            _prosumer_meter(), {CONF_PROSUMER_COEFFICIENT: 0.0}
        )
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(
                return_value={"import_1": {0: 10.0}, "export_1": {0: 100.0}}
            ),
        ):
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "meter_id": "1",
                    "rcem_pln": 0.3,
                },
            )
        assert res["deposit_open"] is None
        assert res["coverage_unknown"] is True
        assert res["warnings"]
