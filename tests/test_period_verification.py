"""Tests for Faza 2 period verification (pure helpers, API range, UI glue)."""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.entity import EntityCategory

from custom_components.energa_mobile import (
    _async_options_updated,
    _only_period_dates_changed,
)
from custom_components.energa_mobile.const import (
    CONF_PROSUMER_COEFFICIENT,
    CONF_VERIFY_PERIOD_END,
    CONF_VERIFY_PERIOD_START,
    DOMAIN,
)
from custom_components.energa_mobile.core.verification import (
    SOURCE_ENERGA_API,
    SOURCE_RECORDER,
    choose_period_source,
    format_period_date,
    parse_period_date,
    period_is_historical,
)
from custom_components.energa_mobile.services import (
    _async_verify_period,
    async_verify_period_data,
)

TZ = ZoneInfo("Europe/Warsaw")


class TestDateHelpers:
    def test_parse_accepts_date_datetime_and_iso(self):
        assert parse_period_date("2026-08-01") == date(2026, 8, 1)
        assert parse_period_date("2026-08-01T12:34:56+02:00") == date(2026, 8, 1)
        assert parse_period_date(date(2026, 8, 1)) == date(2026, 8, 1)
        assert parse_period_date(datetime(2026, 8, 1, 5, 0)) == date(2026, 8, 1)

    def test_parse_rejects_garbage_without_raising(self):
        assert parse_period_date(None) is None
        assert parse_period_date("") is None
        assert parse_period_date("not-a-date") is None
        assert parse_period_date("2026-13-99") is None

    def test_format_normalises(self):
        assert format_period_date(datetime(2026, 8, 1, 9, 0)) == "2026-08-01"
        assert format_period_date("bad") is None

    def test_not_historical_when_ends_today(self):
        today = date(2026, 9, 17)
        assert period_is_historical("2026-08-01", "2026-08-31", today=today) is True
        assert period_is_historical("2026-09-01", "2026-09-17", today=today) is False
        assert period_is_historical("2026-09-01", "2026-09-30", today=today) is False
        assert period_is_historical("bad", "2026-08-31", today=today) is False


class TestChoosePeriodSource:
    def test_api_when_historical(self):
        assert (
            choose_period_source(
                api_available=True, historical=True, recorder_empty=False
            )
            == SOURCE_ENERGA_API
        )

    def test_api_when_recorder_empty(self):
        assert (
            choose_period_source(
                api_available=True, historical=False, recorder_empty=True
            )
            == SOURCE_ENERGA_API
        )

    def test_recorder_when_no_api(self):
        assert (
            choose_period_source(
                api_available=False, historical=True, recorder_empty=True
            )
            == SOURCE_RECORDER
        )

    def test_recorder_when_current_and_recorder_has_data(self):
        assert (
            choose_period_source(
                api_available=True, historical=False, recorder_empty=False
            )
            == SOURCE_RECORDER
        )


class TestApiHourlyRange:
    @pytest.mark.asyncio
    async def test_two_zone_mapping(self, api):
        api._meters_data = [
            {
                "meter_point_id": "m1",
                "zone_count": 2,
                "obis_plus": "1-0:1.8.0*255",
                "obis_minus": "1-0:2.8.0*255",
                "is_prosumer": True,
                "total_minus": 5.0,
            }
        ]

        async def fake_hourly(meter_point_id, day, include_timestamps=False):
            ts = int(
                datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp() * 1000
            )
            return {
                "import": [(1.0, ts)],
                "import_1": [(0.4, ts)],
                "import_2": [(0.6, ts)],
                "export": [(0.1, ts)],
                "export_1": [(0.04, ts)],
                "export_2": [(0.06, ts)],
            }

        api.async_get_history_hourly = fake_hourly
        with patch("asyncio.sleep", new=AsyncMock()):
            out = await api.async_get_hourly_range(
                "m1", date(2026, 8, 1), date(2026, 8, 3)
            )

        assert set(out) == {"import_1", "import_2", "export_1", "export_2"}
        assert sum(out["import_1"].values()) == pytest.approx(0.8)
        assert sum(out["import_2"].values()) == pytest.approx(1.2)
        assert len(out["import_1"]) == 2

    @pytest.mark.asyncio
    async def test_single_zone_consumer_has_no_export(self, api):
        api._meters_data = [
            {
                "meter_point_id": "m2",
                "zone_count": 1,
                "obis_plus": "1-0:1.8.0*255",
                "obis_minus": "1-0:2.8.0*255",
                "is_prosumer": False,
                "total_minus": 0.0,
            }
        ]

        async def fake_hourly(meter_point_id, day, include_timestamps=False):
            return {"import": [(2.0, 3600)], "export": [(0.0, 3600)]}

        api.async_get_history_hourly = fake_hourly
        with patch("asyncio.sleep", new=AsyncMock()):
            out = await api.async_get_hourly_range(
                "m2", date(2026, 8, 1), date(2026, 8, 2)
            )

        assert set(out) == {"import_1", "import_2", "export_1", "export_2"}
        assert sum(out["import_1"].values()) == pytest.approx(2.0)
        assert out["import_2"] == {}
        assert out["export_1"] == {}
        assert out["export_2"] == {}

    @pytest.mark.asyncio
    async def test_maps_zones_hours_and_multiple_days(self, api):
        plus = "1-0:1.8.0*255"
        minus = "1-0:2.8.0*255"
        api._meters_data = [
            {
                "meter_point_id": "m1",
                "zone_count": 2,
                "obis_plus": plus,
                "obis_minus": minus,
                "is_prosumer": True,
                "total_minus": 5.0,
            }
        ]
        calls = []

        async def fake_chart(
            meter_id, obis, timestamp, zone_index=None, include_timestamps=False
        ):
            calls.append(obis)
            assert include_timestamps is True
            base = int(timestamp) // 1000
            if obis == plus:
                value = {None: 3.0, 0: 1.0, 1: 2.0}[zone_index]
            else:
                value = {None: 0.3, 0: 0.1, 1: 0.2}[zone_index]
            return [
                (value, base * 1000),
                (value + 0.5, (base + 3600) * 1000),
            ]

        api._fetch_chart = fake_chart
        day1 = int(datetime(2026, 8, 1, tzinfo=TZ).timestamp())
        day2 = int(datetime(2026, 8, 2, tzinfo=TZ).timestamp())
        with patch("asyncio.sleep", new=AsyncMock()):
            out = await api.async_get_hourly_range(
                "m1", date(2026, 8, 1), date(2026, 8, 3)
            )

        assert set(out) == {"import_1", "import_2", "export_1", "export_2"}
        assert out["import_1"][day1] == pytest.approx(1.0)
        assert out["import_1"][day1 + 3600] == pytest.approx(1.5)
        assert out["import_1"][day2] == pytest.approx(1.0)
        assert len(out["import_1"]) == 4
        assert out["import_2"][day1] == pytest.approx(2.0)
        assert out["export_1"][day1] == pytest.approx(0.1)
        assert out["export_2"][day1] == pytest.approx(0.2)
        # "import" (total) is fetched too but only per-zone keys are stored.
        assert plus in calls and minus in calls

    @pytest.mark.asyncio
    async def test_parses_raw_api_get_tm_string_and_zones(self, api):
        plus = "1-0:1.8.0*255"
        minus = "1-0:2.8.0*255"
        api._meters_data = [
            {
                "meter_point_id": "m1",
                "zone_count": 2,
                "obis_plus": plus,
                "obis_minus": minus,
                "is_prosumer": True,
                "total_minus": 5.0,
            }
        ]

        async def fake_api_get(path, params=None):
            ts = int(params["mainChartDate"])
            zones = [1.0, 2.0, None] if params["meterObject"] == plus else [0.1, 0.2, None]
            return {
                "response": {
                    "mainChart": [
                        {"tm": str(ts), "zones": zones},
                        {"tm": str(ts + 3600000), "zones": zones},
                    ]
                }
            }

        api._api_get = fake_api_get
        day1 = int(datetime(2026, 8, 1, tzinfo=TZ).timestamp())
        out = await api.async_get_hourly_range(
            "m1", date(2026, 8, 1), date(2026, 8, 2)
        )

        assert out["import_1"][day1] == pytest.approx(1.0)
        assert out["import_2"][day1] == pytest.approx(2.0)
        assert out["export_1"][day1] == pytest.approx(0.1)
        assert out["export_2"][day1] == pytest.approx(0.2)
        assert out["import_1"][day1 + 3600] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_skips_failing_day_and_returns_the_rest(self, api):
        api._meters_data = [
            {
                "meter_point_id": "m1",
                "zone_count": 2,
                "obis_plus": "1-0:1.8.0*255",
                "obis_minus": "1-0:2.8.0*255",
                "is_prosumer": True,
                "total_minus": 5.0,
            }
        ]
        bad_day = date(2026, 8, 2)

        async def fake_hourly(meter_point_id, day, include_timestamps=False):
            if day == bad_day:
                raise RuntimeError("boom")
            ts = int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp())
            return {
                "import_1": [(1.0, ts * 1000)],
                "import_2": [(2.0, ts * 1000)],
                "export_1": [(0.1, ts * 1000)],
                "export_2": [(0.2, ts * 1000)],
            }

        api.async_get_history_hourly = fake_hourly
        with patch("asyncio.sleep", new=AsyncMock()):
            out = await api.async_get_hourly_range(
                "m1", date(2026, 8, 1), date(2026, 8, 4)
            )

        assert set(out) == {"import_1", "import_2", "export_1", "export_2"}
        # 3 requested days, middle one fails -> two days survive, no raise.
        assert len(out["import_1"]) == 2
        assert len(out["import_2"]) == 2
        assert len(out["export_1"]) == 2
        assert len(out["export_2"]) == 2

    @pytest.mark.asyncio
    async def test_empty_meters_data_triggers_refresh(self, api):
        api._meters_data = []
        meter = {
            "meter_point_id": "m1",
            "zone_count": 2,
            "obis_plus": "1-0:1.8.0*255",
            "obis_minus": "1-0:2.8.0*255",
            "is_prosumer": True,
            "total_minus": 5.0,
        }

        async def refresh():
            api._meters_data = [meter]
            return [meter]

        async def fake_chart(
            meter_id, obis, timestamp, zone_index=None, include_timestamps=False
        ):
            base = int(timestamp) // 1000
            return [(1.0, base * 1000)]

        api.async_get_data = AsyncMock(side_effect=refresh)
        api._fetch_chart = fake_chart
        out = await api.async_get_hourly_range(
            "m1", date(2026, 8, 1), date(2026, 8, 2)
        )

        api.async_get_data.assert_awaited_once()
        assert sum(out["import_1"].values()) > 0
        assert sum(out["import_2"].values()) > 0

    @pytest.mark.asyncio
    async def test_unknown_meter_returns_empty(self, api):
        api._meters_data = []
        api.async_get_data = AsyncMock(return_value=[])
        with patch("asyncio.sleep", new=AsyncMock()):
            out = await api.async_get_hourly_range(
                "ghost", date(2026, 8, 1), date(2026, 8, 3)
            )
        assert out == {}
        api.async_get_data.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bad_window_returns_empty(self, api):
        assert await api.async_get_hourly_range("m1", date(2026, 8, 2), date(2026, 8, 1)) == {}


def _coordinator():
    meter = {
        "meter_point_id": "1",
        "meter_serial": "S1",
        "zone_count": 1,
        "total_plus": 10.0,
        "tariff": "G12W",
    }
    return SimpleNamespace(data=[meter], _verify_cache={}, _rce_cache=None)


def _service_hass(coordinator, api):
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = {}
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    hass.data = {DOMAIN: {"entry_1": {"coordinator": coordinator, "api": api}}}
    return hass


def _call(data):
    call = MagicMock()
    call.data = data
    return call


class TestServiceSourceSelection:
    @pytest.mark.asyncio
    async def test_api_used_for_historical_meter_period(self):
        api = MagicMock()
        api.async_get_hourly_range = AsyncMock(return_value={"import_1": {0: 100.0}})
        coordinator = _coordinator()
        hass = _service_hass(coordinator, api)

        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value={}),
        ):
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "entry_id": "entry_1",
                    "meter_id": "1",
                },
            )

        assert res["source"] == SOURCE_ENERGA_API
        assert res["cached"] is False
        assert res["kwh"]["saldo_plus_1"] == 100.0

    @pytest.mark.asyncio
    async def test_falls_back_to_recorder_when_api_empty(self):
        api = MagicMock()
        api.async_get_hourly_range = AsyncMock(return_value={})
        coordinator = _coordinator()
        hass = _service_hass(coordinator, api)

        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value={"import_1": {0: 50.0}}),
        ):
            res = await async_verify_period_data(
                hass,
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "entry_id": "entry_1",
                    "meter_id": "1",
                },
            )

        assert res["source"] == SOURCE_RECORDER
        assert res["kwh"]["saldo_plus_1"] == 50.0

    @pytest.mark.asyncio
    async def test_second_call_is_served_from_cache(self):
        api = MagicMock()
        api.async_get_hourly_range = AsyncMock(return_value={"import_1": {0: 100.0}})
        coordinator = _coordinator()
        hass = _service_hass(coordinator, api)
        payload = {
            "start": "2026-08-01",
            "end": "2026-08-31",
            "entry_id": "entry_1",
            "meter_id": "1",
        }

        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value={}),
        ):
            first = await async_verify_period_data(hass, payload)
            second = await async_verify_period_data(hass, payload)

        assert first["cached"] is False
        assert second["cached"] is True
        assert second["do_zaplaty"] == first["do_zaplaty"]

    @pytest.mark.asyncio
    async def test_without_meter_id_recorder_is_kept(self):
        api = MagicMock()
        api.async_get_hourly_range = AsyncMock(return_value={"import_1": {0: 100.0}})
        coordinator = _coordinator()
        hass = _service_hass(coordinator, api)

        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value={"import_1": {0: 50.0}}),
        ):
            res = await _async_verify_period(
                hass, _call({"start": "2026-08-01", "end": "2026-08-31"})
            )

        assert res["source"] == SOURCE_RECORDER
        api.async_get_hourly_range.assert_not_called()


class TestPeriodDateEntity:
    def _entity(self, options):
        from custom_components.energa_mobile.date import EnergaPeriodDate

        entry = MagicMock()
        entry.options = options
        meter = {"meter_point_id": "10000001", "meter_serial": "10000001"}
        entity = EnergaPeriodDate(entry, meter, "start", MagicMock())
        entity.hass = MagicMock()
        return entry, entity

    def test_reads_option(self):
        _, entity = self._entity({CONF_VERIFY_PERIOD_START: "2026-08-01"})
        assert entity.native_value == date(2026, 8, 1)
        assert entity.entity_id == "date.energa_10000001_okres_start"
        assert entity._attr_unique_id == "energa_10000001_period_start"
        assert entity._attr_name == "Okres Start"
        assert entity._attr_entity_category == EntityCategory.CONFIG

    def test_end_entity_name_and_category(self):
        from custom_components.energa_mobile.date import EnergaPeriodDate

        entry = MagicMock()
        entry.options = {}
        meter = {"meter_point_id": "10000001", "meter_serial": "10000001"}
        entity = EnergaPeriodDate(entry, meter, "end", MagicMock())
        assert entity._attr_name == "Okres Koniec"
        assert entity.entity_id == "date.energa_10000001_okres_koniec"
        assert entity._attr_entity_category == EntityCategory.CONFIG
        assert entity._attr_icon == "mdi:calendar-range"

    def test_unset_returns_none(self):
        _, entity = self._entity({})
        assert entity.native_value is None

    @pytest.mark.asyncio
    async def test_set_value_persists_iso_string(self):
        entry, entity = self._entity({})
        with patch.object(entity, "async_write_ha_state", create=True):
            await entity.async_set_value(date(2026, 9, 1))
        called = entity.hass.config_entries.async_update_entry
        assert called.called
        assert (
            called.call_args.kwargs["options"][CONF_VERIFY_PERIOD_START]
            == "2026-09-01"
        )

    @pytest.mark.asyncio
    async def test_invalid_value_is_ignored(self):
        entry, entity = self._entity({})
        with patch.object(entity, "async_write_ha_state", create=True):
            await entity.async_set_value("garbage")
        entity.hass.config_entries.async_update_entry.assert_not_called()
        assert entry.options == {}


class TestVerifyPeriodButton:
    def _button(self, options):
        from custom_components.energa_mobile.button import EnergaVerifyPeriodButton

        entry = MagicMock()
        entry.entry_id = "entry_1"
        entry.options = options
        meter = {"meter_point_id": "10000001", "meter_serial": "10000001"}
        button = EnergaVerifyPeriodButton(hass=MagicMock(), entry=entry, meter=meter)
        return entry, button

    def test_unavailable_without_both_dates(self):
        _, button = self._button({CONF_VERIFY_PERIOD_START: "2026-08-01"})
        assert button.available is False
        _, button2 = self._button(
            {
                CONF_VERIFY_PERIOD_START: "2026-08-01",
                CONF_VERIFY_PERIOD_END: "2026-08-31",
            }
        )
        assert button2.available is True
        assert button2.entity_id == "button.energa_10000001_przelicz_okres"
        assert button2._attr_name == "Przelicz okres rozliczeniowy"
        assert button2._attr_entity_category == EntityCategory.CONFIG

    @pytest.mark.asyncio
    async def test_press_schedules_background_task(self):
        entry, button = self._button(
            {
                CONF_VERIFY_PERIOD_START: "2026-08-01",
                CONF_VERIFY_PERIOD_END: "2026-08-31",
            }
        )
        await button.async_press()
        entry.async_create_background_task.assert_called_once()
        args, kwargs = entry.async_create_background_task.call_args
        args[1].close()  # the coroutine is executed by HA in production
        assert kwargs["name"] == "energa_verify_period_10000001"

    @pytest.mark.asyncio
    async def test_press_without_dates_does_nothing(self):
        entry, button = self._button({})
        await button.async_press()
        entry.async_create_background_task.assert_not_called()

    def test_result_message_includes_full_summary(self):
        _, button = self._button({})
        msg = button._result_message(
            {
                "empty": False,
                "period_start": "2026-07-01",
                "period_end": "2026-09-01",
                "netto": 129.04,
                "vat": 29.68,
                "brutto": 158.72,
                "do_zaplaty": 158.72,
                "old_system": True,
                "kwh": {
                    "cover_1": 83.0,
                    "cover_2": 342.0,
                    "bank_open_1": 500.0,
                    "bank_open_2": 500.0,
                },
                "coverage_unknown": False,
                "source": "energa_api",
            }
        )
        assert "129.04 PLN" in msg
        assert "29.68 PLN" in msg
        assert "158.72 PLN" in msg
        assert "83 kWh" in msg and "342 kWh" in msg
        assert "energa_api" in msg

    def test_result_message_warns_on_unknown_coverage(self):
        _, button = self._button({})
        msg = button._result_message(
            {
                "empty": False,
                "period_start": "2026-07-01",
                "period_end": "2026-09-01",
                "netto": 300.0,
                "vat": 69.0,
                "brutto": 369.0,
                "do_zaplaty": 369.0,
                "old_system": True,
                "coverage_unknown": True,
                "warnings": ["Brak historii magazynu."],
                "source": "energa_api",
            }
        )
        assert "Uwaga" in msg
        assert "Brak historii magazynu." in msg

    def test_result_message_net_billing_shows_deposit(self):
        _, button = self._button({})
        msg = button._result_message(
            {
                "empty": False,
                "netto": 628.55,
                "vat": 144.57,
                "brutto": 773.12,
                "do_zaplaty": 615.53,
                "deposit_applied": 157.59,
                "old_system": False,
                "coverage_unknown": False,
                "source": "energa_api",
            }
        )
        assert "Depozyt" in msg
        assert "157.59 PLN" in msg
        assert "615.53 PLN" in msg

    @pytest.mark.asyncio
    async def test_run_verification_stores_result_and_notifies(self):
        entry, button = self._button(
            {
                CONF_VERIFY_PERIOD_START: "2026-08-01",
                CONF_VERIFY_PERIOD_END: "2026-08-31",
            }
        )
        snapshots = []
        coordinator = SimpleNamespace(_verify_result={})
        coordinator.async_update_listeners = MagicMock(
            side_effect=lambda: snapshots.append(
                dict(coordinator._verify_result.get("10000001") or {})
            )
        )
        button.hass.data = {DOMAIN: {"entry_1": {"coordinator": coordinator}}}
        result = {
            "empty": False,
            "netto": 100.0,
            "brutto": 123.0,
            "do_zaplaty": 23.0,
            "period_start": "2026-08-01T00:00:00+02:00",
            "period_end": "2026-09-01T00:00:00+02:00",
            "source": SOURCE_ENERGA_API,
        }
        with patch(
            "custom_components.energa_mobile.services.async_verify_period_data",
            new=AsyncMock(return_value=result),
        ) as service_mock:
            await button._run_verification(
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "entry_id": "entry_1",
                    "meter_id": "10000001",
                }
            )
        service_mock.assert_awaited_once()
        # First publish is the immediate "calculating" state, second the result.
        assert snapshots[0]["status"] == "calculating"
        assert snapshots[0]["period_start"] == "2026-08-01"
        assert snapshots[-1]["do_zaplaty"] == 23.0
        assert snapshots[-1]["status"] == "ok"
        assert coordinator._verify_result["10000001"]["do_zaplaty"] == 23.0
        assert button._verify_running is False

    @pytest.mark.asyncio
    async def test_double_press_does_not_start_two_tasks(self):
        entry, button = self._button(
            {
                CONF_VERIFY_PERIOD_START: "2026-08-01",
                CONF_VERIFY_PERIOD_END: "2026-08-31",
            }
        )
        coordinator = SimpleNamespace(_verify_result={}, async_update_listeners=MagicMock())
        button.hass.data = {DOMAIN: {"entry_1": {"coordinator": coordinator}}}
        await button.async_press()
        await button.async_press()
        entry.async_create_background_task.assert_called_once()
        # The scheduled coroutine is never awaited by the mock; close it to
        # avoid a "coroutine was never awaited" warning.
        args, _ = entry.async_create_background_task.call_args
        args[1].close()
        assert button._verify_running is True

    @pytest.mark.asyncio
    async def test_empty_period_sets_empty_status(self):
        entry, button = self._button(
            {
                CONF_VERIFY_PERIOD_START: "2026-08-01",
                CONF_VERIFY_PERIOD_END: "2026-08-31",
            }
        )
        coordinator = SimpleNamespace(_verify_result={}, async_update_listeners=MagicMock())
        button.hass.data = {DOMAIN: {"entry_1": {"coordinator": coordinator}}}
        result = {
            "empty": True,
            "error": "no_data",
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "source": SOURCE_RECORDER,
        }
        with patch(
            "custom_components.energa_mobile.services.async_verify_period_data",
            new=AsyncMock(return_value=result),
        ):
            await button._run_verification(
                {"start": "2026-08-01", "end": "2026-08-31",
                 "entry_id": "entry_1", "meter_id": "10000001"}
            )
        assert coordinator._verify_result["10000001"]["status"] == "empty"
        assert button._verify_running is False

    @pytest.mark.asyncio
    async def test_verification_exception_sets_error_status(self):
        entry, button = self._button(
            {
                CONF_VERIFY_PERIOD_START: "2026-08-01",
                CONF_VERIFY_PERIOD_END: "2026-08-31",
            }
        )
        coordinator = SimpleNamespace(_verify_result={}, async_update_listeners=MagicMock())
        button.hass.data = {DOMAIN: {"entry_1": {"coordinator": coordinator}}}
        with patch(
            "custom_components.energa_mobile.services.async_verify_period_data",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            await button._run_verification(
                {"start": "2026-08-01", "end": "2026-08-31",
                 "entry_id": "entry_1", "meter_id": "10000001"}
            )
        assert coordinator._verify_result["10000001"]["status"] == "error"
        assert coordinator._verify_result["10000001"]["empty"] is True
        assert button._verify_running is False


class TestVerificationSensor:
    def _sensor(self, result):
        from custom_components.energa_mobile.sensors.period import (
            EnergaPeriodVerificationSensor,
        )

        coordinator = SimpleNamespace(_verify_result={"10000001": result} if result else {})
        sensor = EnergaPeriodVerificationSensor(
            coordinator=coordinator,
            meter_id="10000001",
            serial="10000001",
            device_info=MagicMock(),
            entry=MagicMock(),
        )
        return sensor

    def test_state_is_do_zaplaty(self):
        sensor = self._sensor(
            {
                "empty": False,
                "do_zaplaty": 615.53,
                "netto": 628.55,
                "brutto": 773.12,
                "kwh": {"saldo_plus_1": 398.0},
                "rcem": 0.29453,
                "source": SOURCE_ENERGA_API,
                "cached": False,
                "period_start": "2026-08-01T00:00:00+02:00",
                "period_end": "2026-09-01T00:00:00+02:00",
            }
        )
        assert sensor.native_value == 615.53
        assert sensor.available is True
        attrs = sensor.extra_state_attributes
        assert attrs["source"] == SOURCE_ENERGA_API
        assert attrs["kwh"]["saldo_plus_1"] == 398.0
        assert attrs["netto"] == 628.55
        assert sensor._attr_state_class is None
        assert sensor._attr_name == "Okres: rozliczenie"
        assert sensor._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_no_result_is_unavailable(self):
        sensor = self._sensor(None)
        assert sensor.native_value is None
        assert sensor.available is False
        assert sensor.extra_state_attributes == {"status": "no_result"}

    def test_empty_result_is_unknown(self):
        sensor = self._sensor({"empty": True, "error": "no_data"})
        assert sensor.native_value is None
        assert sensor.extra_state_attributes["status"] == "empty"

    def test_full_breakdown_attributes_include_new_lines(self):
        sensor = self._sensor(
            {
                "empty": False,
                "do_zaplaty": 158.72,
                "netto": 129.04,
                "vat": 29.68,
                "brutto": 158.72,
                "deposit_applied": 0.0,
                "deposit_generated": 0.0,
                "deposit_open": 0.0,
                "deposit_close": 0.0,
                "distr_abonament": 1.40,
                "distr_grid_fixed": 40.34,
                "distr_capacity": 48.10,
                "coverage_unknown": False,
                "warnings": [],
                "old_system": True,
                "system": "net_metering",
                "prosumer_coefficient": 0.8,
                "kwh": {"cover_1": 83.0, "bank_open_1": 500.0},
                "source": SOURCE_ENERGA_API,
                "period_start": "2026-07-01",
                "period_end": "2026-09-01",
            }
        )
        attrs = sensor.extra_state_attributes
        assert attrs["distr_abonament"] == 1.40
        assert attrs["distr_grid_fixed"] == 40.34
        assert attrs["distr_capacity"] == 48.10
        assert attrs["deposit_open"] == 0.0
        assert attrs["deposit_generated"] == 0.0
        assert attrs["deposit_close"] == 0.0
        assert attrs["coverage_unknown"] is False
        assert attrs["warnings"] == []
        assert attrs["system"] == "net_metering"
        assert attrs["prosumer_coefficient"] == 0.8
        assert attrs["kwh"]["cover_1"] == 83.0
        assert attrs["kwh"]["bank_open_1"] == 500.0

    def test_calculating_result_reports_progress(self):
        sensor = self._sensor(
            {
                "status": "calculating",
                "empty": False,
                "period_start": "2026-08-01",
                "period_end": "2026-08-31",
            }
        )
        assert sensor.native_value is None
        assert sensor.available is True
        attrs = sensor.extra_state_attributes
        assert attrs["status"] == "calculating"
        assert attrs["period_start"] == "2026-08-01"
        assert attrs["period_end"] == "2026-08-31"


class TestOnlyPeriodDatesChanged:
    def test_only_start_changed(self):
        old = {CONF_VERIFY_PERIOD_START: "2026-08-01"}
        new = {CONF_VERIFY_PERIOD_START: "2026-08-02"}
        assert _only_period_dates_changed(old, new) is True

    def test_both_dates_changed(self):
        old = {
            CONF_VERIFY_PERIOD_START: "2026-08-01",
            CONF_VERIFY_PERIOD_END: "2026-08-31",
        }
        new = {
            CONF_VERIFY_PERIOD_START: "2026-09-01",
            CONF_VERIFY_PERIOD_END: "2026-09-30",
        }
        assert _only_period_dates_changed(old, new) is True

    def test_price_change_requires_reload(self):
        old = {CONF_PROSUMER_COEFFICIENT: 0.8}
        new = {CONF_PROSUMER_COEFFICIENT: 0.7}
        assert _only_period_dates_changed(old, new) is False

    def test_mixed_change_requires_reload(self):
        old = {CONF_VERIFY_PERIOD_START: "2026-08-01", CONF_PROSUMER_COEFFICIENT: 0.8}
        new = {CONF_VERIFY_PERIOD_START: "2026-08-02", CONF_PROSUMER_COEFFICIENT: 0.7}
        assert _only_period_dates_changed(old, new) is False

    def test_missing_snapshot_requires_reload(self):
        assert _only_period_dates_changed(None, {CONF_VERIFY_PERIOD_START: "x"}) is False


class TestOptionsUpdatedListener:
    @pytest.mark.asyncio
    async def test_period_date_edit_does_not_reload(self):
        coordinator = SimpleNamespace(async_update_listeners=MagicMock())
        entry = MagicMock()
        entry.entry_id = "entry_1"
        entry.options = {CONF_VERIFY_PERIOD_START: "2026-08-02"}
        hass = MagicMock()
        hass.data = {
            DOMAIN: {
                "entry_1": {
                    "coordinator": coordinator,
                    "_options_snapshot": {CONF_VERIFY_PERIOD_START: "2026-08-01"},
                }
            }
        }
        await _async_options_updated(hass, entry)
        hass.config_entries.async_reload.assert_not_called()
        coordinator.async_update_listeners.assert_called_once()
        assert hass.data[DOMAIN]["entry_1"]["_options_snapshot"] == {
            CONF_VERIFY_PERIOD_START: "2026-08-02"
        }

    @pytest.mark.asyncio
    async def test_non_period_option_change_reloads(self):
        coordinator = SimpleNamespace(async_update_listeners=MagicMock())
        entry = MagicMock()
        entry.entry_id = "entry_1"
        entry.options = {CONF_PROSUMER_COEFFICIENT: 0.7}
        hass = MagicMock()
        hass.config_entries.async_reload = AsyncMock()
        hass.data = {
            DOMAIN: {
                "entry_1": {
                    "coordinator": coordinator,
                    "_options_snapshot": {CONF_PROSUMER_COEFFICIENT: 0.8},
                }
            }
        }
        await _async_options_updated(hass, entry)
        hass.config_entries.async_reload.assert_awaited_once_with("entry_1")
        coordinator.async_update_listeners.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_snapshot_reloads(self):
        entry = MagicMock()
        entry.entry_id = "entry_1"
        entry.options = {CONF_VERIFY_PERIOD_START: "2026-08-02"}
        hass = MagicMock()
        hass.config_entries.async_reload = AsyncMock()
        hass.data = {DOMAIN: {"entry_1": {"coordinator": SimpleNamespace()}}}
        await _async_options_updated(hass, entry)
        hass.config_entries.async_reload.assert_awaited_once_with("entry_1")


class TestPeriodDatePersistenceAfterRestart:
    @pytest.mark.asyncio
    async def test_value_survives_recreation(self):
        """The persisted option is read back by a freshly created entity."""
        from custom_components.energa_mobile.date import EnergaPeriodDate

        store = {}
        entry = MagicMock()
        entry.options = store

        def _update(_entry, *, options):
            store.clear()
            store.update(options)

        entry.options = store
        meter = {"meter_point_id": "10000001", "meter_serial": "10000001"}
        entity = EnergaPeriodDate(entry, meter, "start", MagicMock())
        entity.hass = MagicMock()
        entity.hass.config_entries.async_update_entry.side_effect = _update
        with patch.object(entity, "async_write_ha_state", create=True):
            await entity.async_set_value(date(2026, 9, 1))

        # Simulate an HA restart: a brand-new entity over the stored options.
        entry2 = MagicMock()
        entry2.options = dict(store)
        fresh = EnergaPeriodDate(entry2, meter, "start", MagicMock())
        assert fresh.native_value == date(2026, 9, 1)
