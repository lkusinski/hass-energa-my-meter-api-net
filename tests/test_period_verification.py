"""Tests for Faza 2 period verification (pure helpers, API range, UI glue)."""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.entity import EntityCategory

from custom_components.energa_mobile.const import (
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

    @pytest.mark.asyncio
    async def test_run_verification_stores_result_and_notifies(self):
        entry, button = self._button(
            {
                CONF_VERIFY_PERIOD_START: "2026-08-01",
                CONF_VERIFY_PERIOD_END: "2026-08-31",
            }
        )
        coordinator = SimpleNamespace(_verify_result={}, async_update_listeners=MagicMock())
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
        ):
            await button._run_verification(
                {
                    "start": "2026-08-01",
                    "end": "2026-08-31",
                    "entry_id": "entry_1",
                    "meter_id": "10000001",
                }
            )
        assert coordinator._verify_result["10000001"]["do_zaplaty"] == 23.0
        coordinator.async_update_listeners.assert_called_once()


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
        assert sensor._attr_entity_category == EntityCategory.CONFIG

    def test_no_result_is_unavailable(self):
        sensor = self._sensor(None)
        assert sensor.native_value is None
        assert sensor.available is False
        assert sensor.extra_state_attributes == {"status": "no_result"}

    def test_empty_result_is_unknown(self):
        sensor = self._sensor({"empty": True, "error": "no_data"})
        assert sensor.native_value is None
        assert sensor.extra_state_attributes["status"] == "empty"
