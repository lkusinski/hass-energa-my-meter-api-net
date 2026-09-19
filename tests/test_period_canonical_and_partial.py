"""Faza 4 tests: canonical SQLite opening balances + mid-month period starts.

Two gaps closed in v1.9.2:

* LUKA 1 — ``verify_period`` prefers the canonical SQLite opening snapshot
  (``settlement_lot``/``market_price``) when it exists, otherwise recomputes
  from the recorder/API and persists the result. Precedence is
  ``override`` -> ``canonical`` -> ``recorder``/``api`` and the response
  carries ``opening_source``.
* LUKA 2 — a period starting mid-month includes the elapsed partial start
  month (day-accurate flows), so ``bank_open_*``/``deposit_open`` describe the
  balance at ``period_start`` and not at the 1st of the month.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energa_mobile.const import (
    CONF_PROSUMER_COEFFICIENT,
    DOMAIN,
)
from custom_components.energa_mobile.core.settlement.models import SettlementLot
from custom_components.energa_mobile.core.verification import (
    CANONICAL_OPENING_RULE,
    OPENING_SOURCE_CANONICAL,
    OPENING_SOURCE_OVERRIDE,
    OPENING_SOURCE_RECORDER,
    canonical_opening_lot_id,
    opening_deposit_from_monthly_flows,
    openings_from_canonical_lots,
)
from custom_components.energa_mobile.services import async_verify_period_data
from custom_components.energa_mobile.settlement import trailing_months
from custom_components.energa_mobile.storage.sqlite.database import CanonicalStorage
from custom_components.energa_mobile.tariff import G12W_DEFAULT_FEES

TZ = ZoneInfo("Europe/Warsaw")

_PATCH_HOURLY = "custom_components.energa_mobile.services._collect_meter_hourly"
_PATCH_MONTHLY = "custom_components.energa_mobile.services._collect_monthly_flows"


def _net_metering_meter() -> dict:
    return {
        "meter_point_id": "10000002",
        "meter_serial": "10000002",
        "zone_count": 2,
        "total_plus": 425.0,
        "total_minus": 1904.0,
        "is_prosumer": True,
        "tariff": "G12W",
    }


def _net_billing_meter() -> dict:
    return {
        "meter_point_id": "10000001",
        "meter_serial": "10000001",
        "zone_count": 2,
        "total_plus": 707.0,
        "total_minus": 435.0,
        "is_prosumer": True,
        "tariff": "G12W",
    }


def _wisniowa_hourly() -> dict:
    return {
        "import_1": {0: 83.0},
        "export_1": {},
        "import_2": {0: 342.0},
        "export_2": {},
    }


def _storage_hass(meter: dict, options: dict, *, ppe_id: str = "PPE_TEST"):
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = options
    entry.data = {"ppe_id": ppe_id}
    coordinator = SimpleNamespace(
        data=[meter],
        _verify_cache={},
        _rce_cache=None,
        _rcem_monthly={},
        _monthly={},
        async_update_listeners=MagicMock(),
    )
    storage = CanonicalStorage(":memory:")
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    hass.data = {
        DOMAIN: {
            "entry_1": {
                "coordinator": coordinator,
                "api": None,
                "storage": storage,
            }
        }
    }
    return hass, entry, coordinator, storage


class TestOpeningsFromCanonicalLots:
    def _lot(self, unit, zone, amount, when, rule=CANONICAL_OPENING_RULE):
        return SettlementLot(
            lot_id=canonical_opening_lot_id("PPE_X", unit, zone, when),
            ppe_id="PPE_X",
            unit=unit,
            zone=zone,
            original_amount=Decimal(str(amount)),
            remaining_amount=Decimal(str(amount)),
            created_at_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
            assigned_at=when,
            expires_at=when,
            rule_version=rule,
        )

    def test_lot_id_is_deterministic(self):
        assert (
            canonical_opening_lot_id("PPE_X", "kWh", "day", date(2026, 8, 1))
            == "open_PPE_X_kWh_day_2026-08-01"
        )

    def test_reads_all_zones_only_when_present(self):
        when = date(2026, 8, 1)
        lots = [
            self._lot("kWh", "day", 110.0, when),
            self._lot("kWh", "night", 60.0, when),
        ]
        out = openings_from_canonical_lots(
            lots, unit="kWh", zones=["day", "night"], reference_date=when
        )
        assert out == {"day": 110.0, "night": 60.0}
        # Missing one zone -> None (caller must fall back).
        assert (
            openings_from_canonical_lots(
                lots[:1], unit="kWh", zones=["day", "night"], reference_date=when
            )
            is None
        )

    def test_ignores_other_dates_rules_and_units(self):
        when = date(2026, 8, 1)
        lots = [
            self._lot("kWh", "total", 50.0, date(2026, 7, 1)),
            self._lot("kWh", "total", 50.0, when, rule="fifo_12m"),
            self._lot("PLN", "total", 50.0, when),
            self._lot("kWh", "total", 42.0, when),
        ]
        assert openings_from_canonical_lots(
            lots, unit="kWh", zones=["total"], reference_date=when
        ) == {"total": 42.0}


class TestOpeningDepositPartialMonth:
    def test_mid_month_start_includes_partial_start_month(self):
        # 100 kWh export in the (partial) July start month, RCEm 0.20.
        monthly = {(2026, 7): {"import_1": 0.0, "export_1": 100.0}}
        opening, detail = opening_deposit_from_monthly_flows(
            monthly,
            {(2026, 7): 0.20},
            G12W_DEFAULT_FEES,
            period_start=date(2026, 7, 15),
        )
        assert opening == 24.60
        assert detail["has_data"] is True

    def test_mid_month_start_draws_down_partial_import(self):
        monthly = {(2026, 7): {"import_1": 10.0, "export_1": 100.0}}
        opening, _ = opening_deposit_from_monthly_flows(
            monthly,
            {(2026, 7): 0.20},
            G12W_DEFAULT_FEES,
            period_start=date(2026, 7, 15),
        )
        assert opening == 17.03

    def test_first_of_month_still_ignores_start_month(self):
        monthly = {(2026, 7): {"import_1": 0.0, "export_1": 100.0}}
        opening, _ = opening_deposit_from_monthly_flows(
            monthly,
            {(2026, 7): 0.20},
            G12W_DEFAULT_FEES,
            period_start=date(2026, 7, 1),
        )
        assert opening is None


class TestCanonicalNetMetering:
    @pytest.mark.asyncio
    async def test_compute_saves_then_reads_from_canonical(self):
        hass, _, coordinator, storage = _storage_hass(
            _net_metering_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8}
        )
        monthly = {
            (2026, 7): {
                "import_1": 50.0,
                "export_1": 200.0,
                "import_2": 20.0,
                "export_2": 100.0,
            }
        }
        payload = {
            "start": "2026-08-01",
            "end": "2026-08-31",
            "meter_id": "10000002",
        }

        with patch(
            _PATCH_HOURLY, new=AsyncMock(return_value=_wisniowa_hourly())
        ), patch(_PATCH_MONTHLY, new=AsyncMock(return_value=monthly)):
            first = await async_verify_period_data(hass, payload)

        assert first["opening_source"] == OPENING_SOURCE_RECORDER
        assert first["kwh"]["bank_open_1"] == 110.0  # 200 * 0.8 - 50
        assert first["kwh"]["bank_open_2"] == 60.0  # 100 * 0.8 - 20
        lots = storage.get_settlement_lots("PPE_TEST", unit="kWh")
        assert {lot.zone for lot in lots} == {"day", "night"}
        assert all(lot.rule_version == CANONICAL_OPENING_RULE for lot in lots)

        # Clear the in-memory cache: the canonical snapshot must be preferred
        # and the recorder/API must not be consulted again.
        coordinator._verify_cache = {}
        with patch(
            _PATCH_HOURLY, new=AsyncMock(return_value=_wisniowa_hourly())
        ), patch(_PATCH_MONTHLY, new=AsyncMock(return_value=monthly)) as monthly_mock:
            second = await async_verify_period_data(hass, payload)

        monthly_mock.assert_not_awaited()
        assert second["opening_source"] == OPENING_SOURCE_CANONICAL
        assert second["kwh"]["bank_open_1"] == 110.0
        assert second["kwh"]["bank_open_2"] == 60.0

    @pytest.mark.asyncio
    async def test_override_beats_canonical_and_recorder(self):
        hass, _, coordinator, _ = _storage_hass(
            _net_metering_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8}
        )
        payload = {
            "start": "2026-08-01",
            "end": "2026-08-31",
            "meter_id": "10000002",
            "bank_open_1": 40.0,
        }
        with patch(
            _PATCH_HOURLY, new=AsyncMock(return_value=_wisniowa_hourly())
        ), patch(_PATCH_MONTHLY, new=AsyncMock(return_value={})) as monthly_mock:
            res = await async_verify_period_data(hass, payload)

        monthly_mock.assert_not_awaited()
        assert res["opening_source"] == OPENING_SOURCE_OVERRIDE
        assert res["kwh"]["bank_open_1"] == 40.0
        assert res["kwh"]["cover_1"] == 40.0


class TestCanonicalNetBilling:
    @pytest.mark.asyncio
    async def test_compute_saves_lot_and_market_prices_then_reads(self):
        hass, _, coordinator, storage = _storage_hass(
            _net_billing_meter(), {CONF_PROSUMER_COEFFICIENT: 0.0}
        )
        coordinator._rcem_monthly = {(2026, 6): 0.20, (2026, 7): 0.20}
        monthly = {
            (2026, 6): {"import_1": 0.0, "export_1": 100.0},
            (2026, 7): {"import_1": 10.0, "export_1": 0.0},
        }
        hourly = {
            "import_1": {0: 10.0},
            "export_1": {0: 60.0},
            "import_2": {},
            "export_2": {},
        }
        payload = {
            "start": "2026-08-01",
            "end": "2026-08-31",
            "meter_id": "10000001",
            "rcem_pln": 0.20,
        }

        with patch(
            _PATCH_HOURLY, new=AsyncMock(return_value=hourly)
        ), patch(_PATCH_MONTHLY, new=AsyncMock(return_value=monthly)):
            first = await async_verify_period_data(hass, payload)

        assert first["opening_source"] == OPENING_SOURCE_RECORDER
        assert first["deposit_open"] == 17.03
        lots = storage.get_settlement_lots("PPE_TEST", unit="PLN")
        assert len(lots) == 1
        assert lots[0].remaining_amount == Decimal("17.03")
        assert lots[0].rule_version == CANONICAL_OPENING_RULE
        prices = storage.get_market_prices("RCEM", year=2026)
        assert {p.applicable_month for p in prices} == {6, 7}

        coordinator._verify_cache = {}
        with patch(
            _PATCH_HOURLY, new=AsyncMock(return_value=hourly)
        ), patch(_PATCH_MONTHLY, new=AsyncMock(return_value=monthly)) as monthly_mock:
            second = await async_verify_period_data(hass, payload)

        monthly_mock.assert_not_awaited()
        assert second["opening_source"] == OPENING_SOURCE_CANONICAL
        assert second["deposit_open"] == 17.03
        assert second["coverage_unknown"] is False


class TestOpeningSourceOnSensor:
    def test_sensor_exposes_opening_source(self):
        from custom_components.energa_mobile.sensors.period import (
            EnergaPeriodVerificationSensor,
        )

        coordinator = SimpleNamespace(
            _verify_result={
                "10000002": {
                    "empty": False,
                    "do_zaplaty": 1.0,
                    "opening_source": OPENING_SOURCE_CANONICAL,
                    "deposit_open_source": None,
                }
            }
        )
        sensor = EnergaPeriodVerificationSensor(
            coordinator=coordinator,
            meter_id="10000002",
            serial="10000002",
            device_info=MagicMock(),
            entry=MagicMock(),
        )
        attrs = sensor.extra_state_attributes
        assert attrs["opening_source"] == OPENING_SOURCE_CANONICAL
        assert "deposit_open_source" in attrs


class TestMidMonthService:
    @pytest.mark.asyncio
    async def test_bank_open_reflects_partial_start_month(self):
        hass, _, _, _ = _storage_hass(
            _net_metering_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8}
        )
        monthly = {
            (2026, 7): {
                "import_1": 50.0,
                "export_1": 200.0,
                "import_2": 20.0,
                "export_2": 100.0,
            }
        }
        with patch(
            _PATCH_HOURLY, new=AsyncMock(return_value=_wisniowa_hourly())
        ), patch(_PATCH_MONTHLY, new=AsyncMock(return_value=monthly)):
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-07-15",
                    "end": "2026-07-31",
                    "meter_id": "10000002",
                },
            )
        assert res["opening_source"] == OPENING_SOURCE_RECORDER
        assert res["kwh"]["bank_open_1"] == 110.0
        assert res["kwh"]["bank_open_2"] == 60.0
        assert res["kwh"]["cover_1"] == 83.0
        assert res["coverage_unknown"] is False

    @pytest.mark.asyncio
    async def test_month_only_cache_mid_month_drops_start_month_with_warning(self):
        hass, _, coordinator, _ = _storage_hass(
            _net_metering_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8}
        )
        # Recorder empty -> the coordinator's whole-month cache is used, which
        # cannot be split at the 15th. The start month must be dropped.
        coordinator._monthly = {
            "10000002": {
                (2026, 7): {
                    "import_1": 50.0,
                    "export_1": 200.0,
                    "import_2": 20.0,
                    "export_2": 100.0,
                }
            }
        }
        with patch(
            _PATCH_HOURLY, new=AsyncMock(return_value=_wisniowa_hourly())
        ), patch(_PATCH_MONTHLY, new=AsyncMock(return_value={})):
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-07-15",
                    "end": "2026-07-31",
                    "meter_id": "10000002",
                },
            )
        # Start month dropped -> no prior history -> unknown, but warned.
        assert any("cache (API)" in w for w in res["warnings"])


class TestBankConsistency:
    @pytest.mark.asyncio
    async def test_bank_opening_matches_fifo_bank_sensor(self):
        """verify_period and the Bank sensor share the same FIFO lots.

        The Bank kWh sensor derives its level with ``_fifo_bank_from_monthly``
        (FIFO 12-month over the monthly flows); the verify_period opening at
        the same reference month must produce the same per-zone balance.
        """
        from custom_components.energa_mobile.sensors.bank import (
            _fifo_bank_from_monthly,
        )

        months = trailing_months(date.today(), 13)[-4:-1]  # 3 months before now
        vals = [(10.0, 100.0, 0.0, 50.0), (20.0, 120.0, 5.0, 60.0), (30.0, 80.0, 10.0, 40.0)]
        monthly = {}
        for (y, m), (i1, e1, i2, e2) in zip(months, vals):
            monthly[(y, m)] = {
                "import_1": i1,
                "export_1": e1,
                "import_2": i2,
                "export_2": e2,
            }

        bank_total, detail = _fifo_bank_from_monthly(monthly, 0.8, has_zones=True)
        assert bank_total == pytest.approx(
            detail["bank_kwh_l1"] + detail["bank_kwh_l2"]
        )

        hass, _, _, _ = _storage_hass(
            _net_metering_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8}
        )
        today = date.today()
        with patch(
            _PATCH_HOURLY, new=AsyncMock(return_value=_wisniowa_hourly())
        ), patch(_PATCH_MONTHLY, new=AsyncMock(return_value=monthly)):
            res = await async_verify_period_data(
                hass,
                {
                    "start": today.isoformat(),
                    "end": (today + timedelta(days=5)).isoformat(),
                    "meter_id": "10000002",
                },
            )
        # The verify_period opening is exactly the Bank sensor's FIFO level.
        assert res["opening_source"] == OPENING_SOURCE_RECORDER
        assert res["kwh"]["bank_open_1"] == detail["bank_kwh_l1"]
        assert res["kwh"]["bank_open_2"] == detail["bank_kwh_l2"]
        # Sanity: the service's own result reflects the same cover.
        assert res["kwh"]["cover_1"] == min(detail["bank_kwh_l1"], 83.0)
