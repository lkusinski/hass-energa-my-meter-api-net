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
    CONF_TARIFF_ABONAMENT,
    CONF_TARIFF_CAPACITY,
    CONF_TARIFF_COGEN,
    CONF_TARIFF_ENERGY_DAY,
    CONF_TARIFF_ENERGY_NIGHT,
    CONF_TARIFF_EXCISE_MWH,
    CONF_TARIFF_GRID_FIXED,
    CONF_TARIFF_GRID_VAR_DAY,
    CONF_TARIFF_GRID_VAR_NIGHT,
    CONF_TARIFF_OZE,
    CONF_TARIFF_QUALITY,
    CONF_TARIFF_TRADE_FEE,
    DOMAIN,
)
from custom_components.energa_mobile.core.verification import (
    deposit_ledger_close,
    opening_bank_from_monthly_flows,
    opening_deposit_from_monthly_flows,
)
from custom_components.energa_mobile.services import async_verify_period_data
from custom_components.energa_mobile.tariff import G12W_DEFAULT_FEES

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


class TestOpeningDepositFromMonthlyFlows:
    """Net-billing deposit opening balance reconstructed from full history."""

    def test_generation_then_partial_draw_rolls_over(self):
        # June: export 100 x 0.20 x 1.23 = 24.60 PLN credited (assigned July).
        # July: import 10 kWh -> cap (10*0.6107 + 10*0.005) * 1.23 = 7.57 PLN.
        # Opening for August = 24.60 - 7.57 = 17.03 PLN.
        monthly = {
            (2026, 6): {"import_1": 0.0, "export_1": 100.0},
            (2026, 7): {"import_1": 10.0, "export_1": 0.0},
        }
        rcem = {(2026, 6): 0.20, (2026, 7): 0.20}
        opening, detail = opening_deposit_from_monthly_flows(
            monthly, rcem, G12W_DEFAULT_FEES, period_start=date(2026, 8, 1)
        )
        assert opening == 17.03
        assert detail["has_data"] is True
        assert detail["generated_pln"] == 24.60
        assert detail["applied_pln"] == 7.57
        assert detail["missing_rcem"] == []

    def test_same_month_generation_pays_its_own_bill(self):
        # June export 100 x 0.20 x 1.23 = 24.60 can offset June's own energy
        # charge (cap 75.77), so the carry-over into July is zero.
        monthly = {(2026, 6): {"import_1": 100.0, "export_1": 100.0}}
        opening, detail = opening_deposit_from_monthly_flows(
            monthly,
            {(2026, 6): 0.20},
            G12W_DEFAULT_FEES,
            period_start=date(2026, 7, 1),
        )
        assert opening == 0.0
        assert detail["generated_pln"] == 24.60
        assert detail["applied_pln"] == 24.60

    def test_old_deposit_expires_after_12_rolling_months(self):
        # Deposit generated in 2025-01 expires end of 2026-02; a period starting
        # 2026-04 must no longer see it.
        monthly = {(2025, 1): {"import_1": 0.0, "export_1": 1000.0}}
        opening_live, _ = opening_deposit_from_monthly_flows(
            monthly,
            {(2025, 1): 0.5},
            G12W_DEFAULT_FEES,
            period_start=date(2026, 2, 1),
        )
        opening_expired, detail = opening_deposit_from_monthly_flows(
            monthly,
            {(2025, 1): 0.5},
            G12W_DEFAULT_FEES,
            period_start=date(2026, 4, 1),
        )
        assert opening_live == 615.0
        assert opening_expired == 0.0
        assert detail["expired_pln"] == 615.0

    def test_missing_rcem_falls_back_and_is_reported(self):
        monthly = {(2026, 6): {"import_1": 0.0, "export_1": 100.0}}
        opening, detail = opening_deposit_from_monthly_flows(
            monthly,
            {},
            G12W_DEFAULT_FEES,
            period_start=date(2026, 8, 1),
            fallback_rcem=0.25,
        )
        assert opening == 30.75  # 100 * 0.25 * 1.23
        assert detail["missing_rcem"] == ["2026-06"]

    def test_no_rcem_at_all_skips_the_month(self):
        monthly = {(2026, 6): {"import_1": 0.0, "export_1": 100.0}}
        opening, detail = opening_deposit_from_monthly_flows(
            monthly, {}, G12W_DEFAULT_FEES, period_start=date(2026, 8, 1)
        )
        assert opening == 0.0
        assert detail["missing_rcem"] == ["2026-06"]

    def test_per_month_rcem_values_are_used(self):
        monthly = {
            (2026, 5): {"import_1": 0.0, "export_1": 100.0},
            (2026, 6): {"import_1": 0.0, "export_1": 100.0},
        }
        opening, detail = opening_deposit_from_monthly_flows(
            monthly,
            {(2026, 5): 0.10, (2026, 6): 0.20},
            G12W_DEFAULT_FEES,
            period_start=date(2026, 8, 1),
        )
        # 100*0.10*1.23 + 100*0.20*1.23 = 12.30 + 24.60 = 36.90
        assert opening == 36.90
        assert detail["generated_pln"] == 36.90
        assert detail["missing_rcem"] == []

    def test_no_history_returns_none(self):
        opening, detail = opening_deposit_from_monthly_flows(
            {}, {}, G12W_DEFAULT_FEES, period_start=date(2026, 8, 1)
        )
        assert opening is None
        assert detail["has_data"] is False

    def test_start_month_is_ignored(self):
        # Data from the period-start month itself must not feed the opening.
        monthly = {(2026, 8): {"import_1": 0.0, "export_1": 100.0}}
        opening, _ = opening_deposit_from_monthly_flows(
            monthly,
            {(2026, 8): 0.30},
            G12W_DEFAULT_FEES,
            period_start=date(2026, 8, 1),
        )
        assert opening is None

    def test_garbage_rows_are_skipped(self):
        monthly = {
            "junk": {"import_1": 1.0},
            (2026, 7): {"import_1": "junk", "export_1": 100.0},
        }
        opening, detail = opening_deposit_from_monthly_flows(
            monthly,
            {(2026, 7): 0.20},
            G12W_DEFAULT_FEES,
            period_start=date(2026, 8, 1),
        )
        assert opening == 24.60
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
                {"start": datetime(2026, 7, 1, tzinfo=timezone.utc), "change": 3.0},
                {"start": datetime(2026, 7, 2, tzinfo=timezone.utc), "change": 4.0},
                {"start": datetime(2026, 8, 1, tzinfo=timezone.utc), "change": 9.0},
            ],
            "id_export": [
                {"start": datetime(2026, 7, 3, tzinfo=timezone.utc), "change": 5.0},
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
    async def test_state_column_is_ignored_change_is_the_flow(self):
        """Regression (Wiśniowa live bug): daily ``state`` is not the increment."""
        from custom_components.energa_mobile import services as svc

        hass = MagicMock()
        # old broken shape: state is the raw sensor value (not the daily flow)
        fake_stats = {
            "id_import": [
                {
                    "start": datetime(2026, 7, 1, tzinfo=timezone.utc),
                    "state": 0.0,
                    "change": 40.0,
                },
                {
                    "start": datetime(2026, 7, 2, tzinfo=timezone.utc),
                    "state": 0.0,
                    "change": 43.0,
                },
            ],
            "id_export": [
                {
                    "start": datetime(2026, 7, 3, tzinfo=timezone.utc),
                    "state": 0.0,
                    "change": 561.0,
                },
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
        assert monthly[(2026, 7)]["import"] == 83.0
        assert monthly[(2026, 7)]["export"] == 561.0

    @pytest.mark.asyncio
    async def test_sum_fallback_uses_reset_aware_delta(self):
        from custom_components.energa_mobile import services as svc

        hass = MagicMock()
        fake_stats = {
            "id_import": [
                {"start": datetime(2026, 7, 1, tzinfo=timezone.utc), "sum": 10.0},
                {"start": datetime(2026, 7, 2, tzinfo=timezone.utc), "sum": 15.0},
            ]
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
        assert monthly[(2026, 7)]["import"] == 5.0

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

    @pytest.mark.asyncio
    async def test_net_metering_bank_from_coordinator_monthly_cache(self):
        """Recorder empty but the coordinator/API monthly cache has export."""
        hass, _ = _hass_with_meter(
            _prosumer_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8}
        )
        coordinator = hass.data[DOMAIN]["entry_1"]["coordinator"]
        coordinator._monthly = {"1": {(2026, 7): {"import": 0.0, "export": 200.0}}}
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value={"import_1": {0: 100.0}}),
        ), patch(
            "custom_components.energa_mobile.services._collect_monthly_flows",
            new=AsyncMock(return_value={}),
        ):
            res = await async_verify_period_data(
                hass,
                {"start": "2026-08-01", "end": "2026-08-31", "meter_id": "1"},
            )
        # 200 x 0.8 = 160 kWh warehouse from the coordinator monthly cache.
        assert res["kwh"]["bank_open_1"] == 160.0
        assert res["coverage_unknown"] is False

    @pytest.mark.asyncio
    async def test_zero_bank_with_export_history_is_known(self):
        """A fully consumed warehouse with export history is a valid 0 kWh."""
        hass, _ = _hass_with_meter(
            _prosumer_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8}
        )
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value={"import_1": {0: 100.0}}),
        ), patch(
            "custom_components.energa_mobile.services._collect_monthly_flows",
            new=AsyncMock(
                return_value={(2026, 7): {"import": 200.0, "export": 100.0}}
            ),
        ):
            res = await async_verify_period_data(
                hass,
                {"start": "2026-08-01", "end": "2026-08-31", "meter_id": "1"},
            )
        # export 100 x 0.8 = 80 kWh credited, import 200 kWh -> bank 0.
        assert res["kwh"]["bank_open_1"] == 0.0
        assert res["coverage_unknown"] is False


def _wisniowa_meter() -> dict:
    return {
        "meter_point_id": "10000002",
        "meter_serial": "10000002",
        "zone_count": 2,
        "total_plus": 425.0,
        "total_minus": 1904.0,
        "is_prosumer": True,
        "tariff": "G12W",
    }


def _wisniowa_options() -> dict:
    """Options mirroring the real Wiśniowa G12W net-metering invoice."""
    return {
        CONF_PROSUMER_COEFFICIENT: 0.8,
        CONF_TARIFF_ENERGY_DAY: 0.7125,
        CONF_TARIFF_ENERGY_NIGHT: 0.4622,
        CONF_TARIFF_EXCISE_MWH: 5.0,
        CONF_TARIFF_TRADE_FEE: 16.18,
        CONF_TARIFF_ABONAMENT: 0.70,
        CONF_TARIFF_GRID_FIXED: 20.17,
        CONF_TARIFF_GRID_VAR_DAY: 0.4017,
        CONF_TARIFF_GRID_VAR_NIGHT: 0.0851,
        CONF_TARIFF_QUALITY: 0.0332,
        CONF_TARIFF_OZE: 0.0073,
        CONF_TARIFF_COGEN: 0.0030,
        CONF_TARIFF_CAPACITY: 24.05,
    }


def _wisniowa_hourly() -> dict:
    """Hourly series giving saldo_plus 83/342 and gross import 120/372."""

    def _zone(plus: float, overlap: float, minus: float):
        imp = {0: float(plus)}
        exp: dict = {}
        if overlap or minus:
            imp[3600] = float(overlap)
            exp[3600] = float(overlap + minus)
        return imp, exp

    imp1, exp1 = _zone(83, 37, 63)
    imp2, exp2 = _zone(342, 30, 20)
    return {
        "import_1": imp1,
        "export_1": exp1,
        "import_2": imp2,
        "export_2": exp2,
    }


class TestWisniowaServiceAcceptance:
    """End-to-end ``verify_period`` on the real Wiśniowa invoice.

    The rates come from ``entry.options`` (no fee-table injection): the
    service must pick up the product fees (handlowa 16,18 x2, abonament 0,70,
    energia 0,7125/0,4622) and charge excise on the GROSS import.
    """

    @pytest.mark.asyncio
    async def test_full_invoice_from_change_flows_and_options(self):
        from custom_components.energa_mobile import services as svc

        hass, _ = _hass_with_meter(_wisniowa_meter(), _wisniowa_options())
        # June flows BEFORE the July-1 period start; ``change`` (not ``state``)
        # must drive the FIFO warehouse: L1 752 kWh, L2 606 kWh at July 1.
        june = datetime(2026, 6, 1, tzinfo=timezone.utc)
        change_stats = {
            "id_import_1": [{"start": june, "change": 0.0}],
            "id_export_1": [{"start": june, "change": 940.0}],
            "id_import_2": [{"start": june, "change": 0.0}],
            "id_export_2": [{"start": june, "change": 757.5}],
        }
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value=_wisniowa_hourly()),
        ), patch.object(
            svc,
            "_statistic_id_for",
            side_effect=lambda hass, mid, serial, suffix: f"id_{suffix}",
        ), patch.object(svc, "get_instance") as gi:
            gi.return_value.async_add_executor_job = AsyncMock(
                return_value=change_stats
            )
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-07-01",
                    "end": "2026-08-31",
                    "meter_id": "10000002",
                    "rcem_pln": 0.0,
                },
            )

        assert res["old_system"] is True
        assert res["fee_source"] == "options"
        # FIFO warehouse >= the positive balances, fully covering the period.
        assert res["kwh"]["bank_open_1"] >= 83.0
        assert res["kwh"]["bank_open_2"] >= 342.0
        assert res["kwh"]["cover_1"] == 83.0
        assert res["kwh"]["cover_2"] == 342.0
        assert res["coverage_unknown"] is False
        # Real invoice FES/00042: 129,04 / 29,68 / 158,72.
        assert res["excise_day"] == 0.60
        assert res["excise_night"] == 1.86
        assert res["trade_fee"] == 32.36
        assert res["netto"] == 129.04
        assert res["vat"] == 29.68
        assert res["brutto"] == 158.72
        assert res["do_zaplaty"] == 158.72

    @pytest.mark.asyncio
    async def test_zero_bank_on_positive_import_flags_unknown(self):
        """A 0/0 warehouse reconstruction must not silently overcharge."""
        hass, _ = _hass_with_meter(_wisniowa_meter(), _wisniowa_options())
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value=_wisniowa_hourly()),
        ), patch(
            "custom_components.energa_mobile.services._collect_monthly_flows",
            new=AsyncMock(return_value={(2026, 7): {"import_1": 10.0}}),
        ):
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-07-01",
                    "end": "2026-08-31",
                    "meter_id": "10000002",
                    "rcem_pln": 0.0,
                },
            )
        assert res["coverage_unknown"] is True
        assert res["warnings"]
        assert res["kwh"]["cover_1"] == 0.0
        assert res["kwh"]["bank_open_1"] is None


def _agrestowa_meter() -> dict:
    return {
        "meter_point_id": "10000001",
        "meter_serial": "10000001",
        "zone_count": 2,
        "total_plus": 707.0,
        "total_minus": 435.0,
        "is_prosumer": True,
        "tariff": "G12W",
    }


def _agrestowa_options() -> dict:
    """Options mirroring the real Agrestowa G12W net-billing product."""
    return {
        CONF_PROSUMER_COEFFICIENT: 0.0,
        CONF_TARIFF_ENERGY_DAY: 0.6107,
        CONF_TARIFF_ENERGY_NIGHT: 0.3990,
        CONF_TARIFF_EXCISE_MWH: 5.0,
        CONF_TARIFF_TRADE_FEE: 0.0,
        CONF_TARIFF_ABONAMENT: 0.74,
        CONF_TARIFF_GRID_FIXED: 20.17,
        CONF_TARIFF_GRID_VAR_DAY: 0.4017,
        CONF_TARIFF_GRID_VAR_NIGHT: 0.0851,
        CONF_TARIFF_QUALITY: 0.0332,
        CONF_TARIFF_OZE: 0.0073,
        CONF_TARIFF_COGEN: 0.0030,
        CONF_TARIFF_CAPACITY: 24.05,
    }


def _agrestowa_hourly() -> dict:
    """Hourly series giving saldo_plus 398/309, overlap 38/23, export 238/197."""

    def _zone(plus: float, overlap: float, minus: float):
        imp = {0: float(plus)}
        exp: dict = {}
        if overlap or minus:
            imp[3600] = float(overlap)
            exp[3600] = float(overlap + minus)
        return imp, exp

    imp1, exp1 = _zone(398, 38, 238)
    imp2, exp2 = _zone(309, 23, 197)
    return {
        "import_1": imp1,
        "export_1": exp1,
        "import_2": imp2,
        "export_2": exp2,
    }


def _api_for(hass):
    return hass.data[DOMAIN]["entry_1"]["api"]


class TestAgrestowaServiceAcceptance:
    """End-to-end net-billing invoice with AUTOMATIC opening deposit.

    Real August 2026 invoice FES/00045: netto 628,55 / VAT 144,57 /
    brutto 773,12 / depozyt 157,59 / do zapłaty 615,53. July's deposit was
    fully consumed, so the reconstructed August opening is 0,00 and the
    result must carry no ``coverage_unknown`` warning.
    """

    @pytest.mark.asyncio
    async def test_auto_deposit_open_and_invoice_to_the_grosz(self):
        hass, _ = _hass_with_meter(_agrestowa_meter(), _agrestowa_options())
        _api_for(hass).async_fetch_official_rcem_map = AsyncMock(
            return_value={(2026, 7): 0.26288}
        )
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value=_agrestowa_hourly()),
        ), patch(
            "custom_components.energa_mobile.services._collect_monthly_flows",
            new=AsyncMock(
                return_value={
                    (2026, 7): {
                        "import_1": 204.0,
                        "import_2": 266.0,
                        "export_1": 356.0,
                        "export_2": 220.0,
                    }
                }
            ),
        ) as monthly_mock:
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "meter_id": "10000001",
                    "rcem_pln": 0.29453,
                },
            )
        monthly_mock.assert_awaited()
        assert res["old_system"] is False
        assert res["fee_source"] == "options"
        # July deposit (186,24) fully paid July's energy -> August opens at 0.
        assert res["deposit_open"] == 0.0
        assert res["deposit_generated"] == 157.59
        assert res["deposit_applied"] == 157.59
        assert res["coverage_unknown"] is False
        assert res["netto"] == 628.55
        assert res["vat"] == 144.57
        assert res["brutto"] == 773.12
        assert res["do_zaplaty"] == 615.53

    @pytest.mark.asyncio
    async def test_missing_history_flags_unknown_with_warning(self):
        hass, _ = _hass_with_meter(_agrestowa_meter(), _agrestowa_options())
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value=_agrestowa_hourly()),
        ), patch(
            "custom_components.energa_mobile.services._collect_monthly_flows",
            new=AsyncMock(return_value={}),
        ):
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "meter_id": "10000001",
                    "rcem_pln": 0.29453,
                },
            )
        assert res["deposit_open"] is None
        assert res["coverage_unknown"] is True
        assert any("historii przepływów" in w for w in res["warnings"])


class TestNetBillingAutoDeposit:
    """Automatic deposit reconstruction honours overrides and warns on RCEm."""

    @pytest.mark.asyncio
    async def test_nonzero_opening_from_history_is_used(self):
        hass, _ = _hass_with_meter(
            _prosumer_meter(), {CONF_PROSUMER_COEFFICIENT: 0.0}
        )
        _api_for(hass).async_fetch_official_rcem_map = AsyncMock(
            return_value={(2026, 6): 0.20}
        )
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(
                return_value={"import_1": {0: 10.0}, "export_1": {0: 60.0}}
            ),
        ), patch(
            "custom_components.energa_mobile.services._collect_monthly_flows",
            new=AsyncMock(
                return_value={
                    (2026, 6): {"import": 0.0, "export": 100.0},
                    (2026, 7): {"import": 10.0, "export": 0.0},
                }
            ),
        ):
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "meter_id": "1",
                    "rcem_pln": 0.20,
                },
            )
        assert res["deposit_open"] == 17.03
        assert res["coverage_unknown"] is False
        # 50 export x 0.20 x 1.23 = 12.30 generated in the period.
        assert res["deposit_generated"] == 12.30
        assert res["deposit"] == 29.33

    @pytest.mark.asyncio
    async def test_manual_override_skips_reconstruction(self):
        hass, _ = _hass_with_meter(
            _prosumer_meter(), {CONF_PROSUMER_COEFFICIENT: 0.0}
        )
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(
                return_value={"import_1": {0: 10.0}, "export_1": {0: 60.0}}
            ),
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
                    "rcem_pln": 0.20,
                    "deposit_open_pln": 16.94,
                },
            )
        monthly_mock.assert_not_awaited()
        assert res["deposit_open"] == 16.94
        assert res["coverage_unknown"] is False
        assert res["deposit"] == 29.24  # 16.94 + 12.30

    @pytest.mark.asyncio
    async def test_missing_historical_rcem_falls_back_with_warning(self):
        hass, _ = _hass_with_meter(
            _prosumer_meter(), {CONF_PROSUMER_COEFFICIENT: 0.0}
        )
        _api_for(hass).async_fetch_official_rcem_map = AsyncMock(return_value={})
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(
                return_value={"import_1": {0: 10.0}, "export_1": {0: 60.0}}
            ),
        ), patch(
            "custom_components.energa_mobile.services._collect_monthly_flows",
            new=AsyncMock(
                return_value={(2026, 7): {"import": 0.0, "export": 100.0}}
            ),
        ):
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "meter_id": "1",
                    "rcem_pln": 0.25,
                },
            )
        # 100 x 0.25 x 1.23 = 30.75 reconstructed with the fallback RCEm.
        assert res["deposit_open"] == 30.75
        assert any("historycznych cen RCEm" in w for w in res["warnings"])
        assert "2026-07" in " ".join(res["warnings"])

    @pytest.mark.asyncio
    async def test_history_falls_back_to_coordinator_monthly_cache(self):
        hass, _ = _hass_with_meter(
            _prosumer_meter(), {CONF_PROSUMER_COEFFICIENT: 0.0}
        )
        coordinator = hass.data[DOMAIN]["entry_1"]["coordinator"]
        coordinator._monthly = {
            "1": {(2026, 7): {"import": 0.0, "export": 100.0}}
        }
        _api_for(hass).async_fetch_official_rcem_map = AsyncMock(
            return_value={(2026, 7): 0.20}
        )
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(
                return_value={"import_1": {0: 10.0}, "export_1": {0: 60.0}}
            ),
        ), patch(
            "custom_components.energa_mobile.services._collect_monthly_flows",
            new=AsyncMock(return_value={}),
        ):
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "meter_id": "1",
                    "rcem_pln": 0.20,
                },
            )
        # Coordinator (API) monthly cache: 100 x 0.20 x 1.23 = 24.60 opening.
        assert res["deposit_open"] == 24.60
        assert res["coverage_unknown"] is False


class TestServiceFeeSource:
    @pytest.mark.asyncio
    async def test_defaults_are_reported_and_warned(self):
        hass, _ = _hass_with_meter(
            _prosumer_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8}
        )
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value={"import_1": {0: 100.0}}),
        ), patch(
            "custom_components.energa_mobile.services._collect_monthly_flows",
            new=AsyncMock(return_value={}),
        ):
            res = await async_verify_period_data(
                hass,
                {"start": "2026-08-01", "end": "2026-08-31", "meter_id": "1"},
            )
        assert res["fee_source"] == "defaults"
        assert any("tabeli domyślnej" in w for w in res["warnings"])

    def test_fee_source_helper_classifies(self):
        from custom_components.energa_mobile.tariff import fee_source

        assert fee_source({})[0] == "defaults"
        assert fee_source({"tariff_trade_fee": 16.18})[0] == "partial"
        assert fee_source(_wisniowa_options())[0] == "options"
