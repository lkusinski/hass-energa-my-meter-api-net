"""Tests for the period completeness sensor + recalculate button gate (v1.9.0-beta.5)."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energa_mobile.const import (
    CONF_VERIFY_PERIOD_END,
    CONF_VERIFY_PERIOD_START,
    DOMAIN,
)
from custom_components.energa_mobile.core.completeness import (
    STATE_COMPLETE,
    STATE_INCOMPLETE,
    STATE_UNKNOWN,
    evaluate_completeness,
    registers_for_meter,
    unknown_result,
)
from custom_components.energa_mobile.services import (
    async_compute_period_completeness,
    async_verify_period_data,
    period_completeness_status,
)

UTC = timezone.utc
WARSAW = ZoneInfo("Europe/Warsaw")


def _day_series(*days):
    """Build a series keyed by noon UTC datetimes for the given ISO days."""
    out = {}
    for iso in days:
        y, m, d = (int(part) for part in iso.split("-"))
        out[datetime(y, m, d, 12, tzinfo=UTC)] = 1.0
    return out


class TestEvaluateCompleteness:
    def test_complete_range(self):
        result = evaluate_completeness(
            period_start="2026-08-03",
            period_end="2026-08-07",
            hourly_by_zone={
                "import_1": _day_series(
                    "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"
                )
            },
            registers=["import_1"],
            source_checked="recorder",
            tz=UTC,
        )
        assert result["state"] == STATE_COMPLETE
        assert result["expected_days"] == 5
        assert result["available_days"] == 5
        assert result["missing_days"] == []
        assert result["completeness_pct"] == 100.0
        assert result["source_checked"] == "recorder"

    def test_gap_is_incomplete(self):
        result = evaluate_completeness(
            period_start="2026-08-03",
            period_end="2026-08-07",
            hourly_by_zone={
                "import_1": _day_series(
                    "2026-08-03", "2026-08-04", "2026-08-06", "2026-08-07"
                )
            },
            registers=["import_1"],
            source_checked="recorder",
            tz=UTC,
        )
        assert result["state"] == STATE_INCOMPLETE
        assert result["missing_days"] == ["2026-08-05"]
        assert result["available_days"] == 4
        assert result["completeness_pct"] == 80.0

    def test_zero_readings_still_count_as_covered(self):
        result = evaluate_completeness(
            period_start="2026-08-03",
            period_end="2026-08-04",
            hourly_by_zone={
                "import_1": {
                    datetime(2026, 8, 3, 1, tzinfo=UTC): 0.0,
                    datetime(2026, 8, 4, 1, tzinfo=UTC): 0.0,
                }
            },
            registers=["import_1"],
            source_checked="api",
            tz=UTC,
        )
        assert result["state"] == STATE_COMPLETE
        assert result["available_days"] == 2

    def test_weekend_without_readings_is_not_missing(self):
        # 2026-08-01 = Saturday, 2026-08-02 = Sunday; no readings at all.
        result = evaluate_completeness(
            period_start="2026-08-01",
            period_end="2026-08-02",
            hourly_by_zone={},
            registers=["import_1"],
            source_checked="recorder",
            tz=UTC,
        )
        assert result["state"] == STATE_COMPLETE
        assert result["missing_days"] == []

    def test_missing_dates_is_unknown(self):
        result = unknown_result(period_start=None, period_end="2026-08-31")
        assert result["state"] == STATE_UNKNOWN
        assert result["expected_days"] == 0
        assert result["available_days"] == 0
        assert result["completeness_pct"] == 0.0

    def test_invalid_range_is_unknown(self):
        result = evaluate_completeness(
            period_start="2026-08-31",
            period_end="2026-08-01",
            hourly_by_zone={},
            registers=["import_1"],
            tz=UTC,
        )
        assert result["state"] == STATE_UNKNOWN

    def test_export_gap_does_not_block_when_import_covers_day(self):
        result = evaluate_completeness(
            period_start="2026-08-03",
            period_end="2026-08-04",
            hourly_by_zone={
                "import_1": _day_series("2026-08-03", "2026-08-04"),
                # export only on Monday; Sunday has no export (genuine zero)
                "export_1": _day_series("2026-08-03"),
            },
            registers=["import_1", "export_1"],
            source_checked="recorder",
            tz=UTC,
        )
        assert result["state"] == STATE_COMPLETE
        assert result["registers"]["export_1"]["available_days"] == 1
        assert result["registers"]["export_1"]["has_data"] is True

    def test_dst_transition_buckets_by_local_date(self):
        # DST starts 2026-03-29 in Europe/Warsaw; keys are UTC datetimes but
        # must bucket to the correct local days.
        result = evaluate_completeness(
            period_start="2026-03-28",
            period_end="2026-03-30",
            hourly_by_zone={
                "import_1": {
                    datetime(2026, 3, 28, 6, tzinfo=UTC): 1.0,
                    datetime(2026, 3, 29, 6, tzinfo=UTC): 1.0,
                    datetime(2026, 3, 30, 6, tzinfo=UTC): 1.0,
                }
            },
            registers=["import_1"],
            source_checked="recorder",
            tz=WARSAW,
        )
        assert result["expected_days"] == 3
        assert result["state"] == STATE_COMPLETE


class TestRegistersForMeter:
    def test_single_zone_consumer(self):
        assert registers_for_meter({"zone_count": 1}) == ["import_1"]

    def test_two_zone_consumer(self):
        assert registers_for_meter({"zone_count": 2}) == ["import_1", "import_2"]

    def test_two_zone_prosumer_adds_export(self):
        meter = {
            "zone_count": 2,
            "total_minus": 10.0,
            "meterObjects": [{"obis": "1-0:2.8.1*255"}],
        }
        registers = registers_for_meter(meter)
        assert "import_1" in registers and "import_2" in registers
        assert "export_1" in registers and "export_2" in registers


def _entry(options):
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = options
    return entry


class TestComputePeriodCompleteness:
    @pytest.mark.asyncio
    async def test_unknown_without_dates(self):
        result = await async_compute_period_completeness(
            MagicMock(), _entry({}), {"meter_point_id": "1", "zone_count": 1}
        )
        assert result["state"] == STATE_UNKNOWN

    @pytest.mark.asyncio
    async def test_recorder_used_first(self):
        entry = _entry(
            {
                CONF_VERIFY_PERIOD_START: "2026-08-03",
                CONF_VERIFY_PERIOD_END: "2026-08-04",
            }
        )
        meter = {"meter_point_id": "1", "meter_serial": "S1", "zone_count": 1}
        hourly = {"import_1": _day_series("2026-08-03", "2026-08-04")}
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value=hourly),
        ), patch(
            "custom_components.energa_mobile.services._collect_meter_hourly_api",
            new=AsyncMock(return_value={}),
        ) as api_mock:
            result = await async_compute_period_completeness(MagicMock(), entry, meter)

        assert result["state"] == STATE_COMPLETE
        assert result["source_checked"] == "recorder"
        api_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_fallback_when_recorder_empty(self):
        entry = _entry(
            {
                CONF_VERIFY_PERIOD_START: "2026-08-03",
                CONF_VERIFY_PERIOD_END: "2026-08-04",
            }
        )
        meter = {"meter_point_id": "1", "meter_serial": "S1", "zone_count": 1}
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry_1": {"api": object()}}}
        hourly = {"import_1": _day_series("2026-08-03", "2026-08-04")}
        with patch(
            "custom_components.energa_mobile.services._collect_meter_hourly",
            new=AsyncMock(return_value={}),
        ), patch(
            "custom_components.energa_mobile.services._collect_meter_hourly_api",
            new=AsyncMock(return_value=hourly),
        ):
            result = await async_compute_period_completeness(hass, entry, meter)

        assert result["state"] == STATE_COMPLETE
        assert result["source_checked"] == "api"


class TestCompletenessSensor:
    def _sensor(self, coordinator=None, options=None):
        from custom_components.energa_mobile.sensors.completeness import (
            EnergaPeriodCompletenessSensor,
        )

        coordinator = coordinator or SimpleNamespace(
            _period_completeness={}, async_update_listeners=MagicMock()
        )
        entry = _entry(
            options
            or {
                CONF_VERIFY_PERIOD_START: "2026-08-03",
                CONF_VERIFY_PERIOD_END: "2026-08-04",
            }
        )
        sensor = EnergaPeriodCompletenessSensor(
            coordinator=coordinator,
            meter={"meter_point_id": "1", "meter_serial": "S1", "zone_count": 1},
            meter_id="1",
            serial="S1",
            device_info=MagicMock(),
            entry=entry,
        )
        sensor.hass = MagicMock()
        return sensor

    def test_initial_state_is_unknown(self):
        sensor = self._sensor()
        assert sensor.native_value == STATE_UNKNOWN
        assert sensor.available is True
        assert sensor.entity_id == "sensor.energa_s1_okres_kompletnosc"
        assert sensor._attr_unique_id == "energa_1_period_completeness"
        assert sensor._attr_name == "Okres: kompletność danych"

    @pytest.mark.asyncio
    async def test_refresh_publishes_state_and_attributes(self):
        coordinator = SimpleNamespace(
            _period_completeness={}, async_update_listeners=MagicMock()
        )
        sensor = self._sensor(coordinator=coordinator)
        result = {
            "state": STATE_INCOMPLETE,
            "period_start": "2026-08-03",
            "period_end": "2026-08-04",
            "expected_days": 2,
            "available_days": 1,
            "missing_days": ["2026-08-04"],
            "completeness_pct": 50.0,
            "source_checked": "recorder",
            "checked_at": "2026-09-18T00:00:00+00:00",
        }
        with patch(
            "custom_components.energa_mobile.services.async_compute_period_completeness",
            new=AsyncMock(return_value=result),
        ):
            await sensor.async_refresh()

        assert sensor.native_value == STATE_INCOMPLETE
        attrs = sensor.extra_state_attributes
        assert attrs["missing_days"] == ["2026-08-04"]
        assert attrs["completeness_pct"] == 50.0
        assert attrs["source_checked"] == "recorder"
        assert coordinator._period_completeness["1"]["state"] == STATE_INCOMPLETE
        coordinator.async_update_listeners.assert_called_once()


class TestCompletenessThreadSafety:
    """HA 2026.9: state writes / task creation must stay on the event loop."""

    def _sensor(self, coordinator, options):
        from custom_components.energa_mobile.sensors.completeness import (
            EnergaPeriodCompletenessSensor,
        )

        sensor = EnergaPeriodCompletenessSensor(
            coordinator=coordinator,
            meter={"meter_point_id": "1", "meter_serial": "S1", "zone_count": 1},
            meter_id="1",
            serial="S1",
            device_info=MagicMock(),
            entry=_entry(options),
        )
        sensor.hass = SimpleNamespace(loop=None)
        return sensor

    @pytest.mark.asyncio
    async def test_worker_thread_state_write_hops_to_loop(self):
        import threading

        coordinator = SimpleNamespace(
            _period_completeness={}, async_update_listeners=MagicMock()
        )
        sensor = self._sensor(
            coordinator,
            {
                CONF_VERIFY_PERIOD_START: "2026-08-03",
                CONF_VERIFY_PERIOD_END: "2026-08-04",
            },
        )
        loop = asyncio.get_running_loop()
        sensor.hass = SimpleNamespace(loop=loop)
        loop_thread = threading.get_ident()
        written: list[int] = []
        sensor.async_write_ha_state = lambda: written.append(threading.get_ident())

        # Called from a worker thread: must be re-scheduled on the loop.
        await asyncio.to_thread(sensor._safe_write_state)
        await asyncio.sleep(0)

        assert written == [loop_thread]

    @pytest.mark.asyncio
    async def test_date_change_publishes_unknown_and_reschedules(self):
        coordinator = SimpleNamespace(
            _period_completeness={}, async_update_listeners=MagicMock()
        )
        sensor = self._sensor(
            coordinator,
            {
                CONF_VERIFY_PERIOD_START: "2026-08-03",
                CONF_VERIFY_PERIOD_END: "2026-08-04",
            },
        )
        previous = {
            "state": STATE_COMPLETE,
            "period_start": "2026-07-01",
            "period_end": "2026-07-31",
        }
        sensor._state = STATE_COMPLETE
        sensor._attrs = previous
        coordinator._period_completeness["1"] = dict(previous)

        cancelled: list[bool] = []

        class _FakeTask:
            def cancel(self):
                cancelled.append(True)

        sensor._refresh_task = _FakeTask()
        scheduled: list[bool] = []
        sensor._schedule_refresh = lambda: scheduled.append(True)

        sensor._handle_period_options_updated("entry_1")

        # Previous (stale) check cancelled; fresh unknown published at once.
        assert cancelled == [True]
        assert sensor.native_value == STATE_UNKNOWN
        assert coordinator._period_completeness["1"]["state"] == STATE_UNKNOWN
        assert coordinator._period_completeness["1"]["period_start"] == "2026-08-03"
        assert scheduled == [True]

    def test_date_change_for_other_entry_is_ignored(self):
        coordinator = SimpleNamespace(
            _period_completeness={}, async_update_listeners=MagicMock()
        )
        sensor = self._sensor(coordinator, {})
        scheduled: list[bool] = []
        sensor._schedule_refresh = lambda: scheduled.append(True)
        sensor._handle_period_options_updated("other_entry")
        assert scheduled == []

    @pytest.mark.asyncio
    async def test_stale_refresh_does_not_overwrite_newer(self):
        coordinator = SimpleNamespace(
            _period_completeness={}, async_update_listeners=MagicMock()
        )
        sensor = self._sensor(
            coordinator,
            {
                CONF_VERIFY_PERIOD_START: "2026-08-03",
                CONF_VERIFY_PERIOD_END: "2026-08-04",
            },
        )
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = {"n": 0}

        async def _fake_compute(hass, entry, meter):
            calls["n"] += 1
            if calls["n"] == 1:
                first_started.set()
                await release_first.wait()
                return {
                    "state": STATE_COMPLETE,
                    "period_start": "2026-07-01",
                    "period_end": "2026-07-31",
                }
            return {
                "state": STATE_INCOMPLETE,
                "period_start": "2026-08-03",
                "period_end": "2026-08-04",
            }

        with patch(
            "custom_components.energa_mobile.services.async_compute_period_completeness",
            new=_fake_compute,
        ):
            older = asyncio.create_task(sensor.async_refresh())
            await first_started.wait()
            newer = asyncio.create_task(sensor.async_refresh())
            await newer
            assert sensor.native_value == STATE_INCOMPLETE
            release_first.set()
            await older

        # The slow older verdict must not clobber the newer one.
        assert sensor.native_value == STATE_INCOMPLETE


class TestButtonGateIntegration:
    def _button(self, options, coordinator):
        from custom_components.energa_mobile.button import EnergaVerifyPeriodButton

        hass = MagicMock()
        hass.data = {DOMAIN: {"entry_1": {"coordinator": coordinator}}}
        entry = _entry(options)
        meter = {"meter_point_id": "1", "meter_serial": "S1", "zone_count": 1}
        return EnergaVerifyPeriodButton(hass=hass, entry=entry, meter=meter)

    def test_button_available_only_when_complete(self):
        options = {
            CONF_VERIFY_PERIOD_START: "2026-08-03",
            CONF_VERIFY_PERIOD_END: "2026-08-04",
        }
        coordinator = SimpleNamespace(
            _period_completeness={
                "1": {
                    "state": STATE_COMPLETE,
                    "period_start": "2026-08-03",
                    "period_end": "2026-08-04",
                }
            },
            async_update_listeners=MagicMock(),
        )
        assert self._button(options, coordinator).available is True
        coordinator._period_completeness["1"]["state"] = STATE_INCOMPLETE
        assert self._button(options, coordinator).available is False
        # A verdict for a different window must count as stale -> unavailable.
        coordinator._period_completeness["1"] = {
            "state": STATE_COMPLETE,
            "period_start": "2026-07-01",
            "period_end": "2026-07-31",
        }
        assert self._button(options, coordinator).available is False

    @pytest.mark.asyncio
    async def test_button_worker_thread_state_write_hops_to_loop(self):
        import threading

        options = {
            CONF_VERIFY_PERIOD_START: "2026-08-03",
            CONF_VERIFY_PERIOD_END: "2026-08-04",
        }
        coordinator = SimpleNamespace(
            _period_completeness={"1": {"state": STATE_COMPLETE}},
            async_update_listeners=MagicMock(),
        )
        button = self._button(options, coordinator)
        loop = asyncio.get_running_loop()
        button.hass = SimpleNamespace(loop=loop)
        loop_thread = threading.get_ident()
        written: list[int] = []
        button.async_write_ha_state = lambda: written.append(threading.get_ident())

        await asyncio.to_thread(button._safe_write_state)
        await asyncio.sleep(0)

        assert written == [loop_thread]

    def test_status_helper_reads_cache(self):
        coordinator = SimpleNamespace(
            _period_completeness={"1": {"state": STATE_INCOMPLETE}}
        )
        assert period_completeness_status(coordinator, "1") == STATE_INCOMPLETE
        assert period_completeness_status(coordinator, "missing") == STATE_UNKNOWN
        assert period_completeness_status(None, "1") == STATE_UNKNOWN


class TestServiceGate:
    def _hass(self, coordinator):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "entry_1"
        entry.options = {}
        hass.config_entries.async_entries = MagicMock(return_value=[entry])
        hass.data = {DOMAIN: {"entry_1": {"coordinator": coordinator, "api": MagicMock()}}}
        return hass

    @pytest.mark.asyncio
    async def test_service_refuses_incomplete_period(self):
        coordinator = SimpleNamespace(
            _period_completeness={
                "1": {
                    "state": STATE_INCOMPLETE,
                    "expected_days": 2,
                    "available_days": 1,
                    "period_start": "2026-08-03",
                    "period_end": "2026-08-04",
                }
            },
            _verify_cache={},
        )
        hass = self._hass(coordinator)
        result = await async_verify_period_data(
            hass,
            {
                "start": "2026-08-03",
                "end": "2026-08-04",
                "entry_id": "entry_1",
                "meter_id": "1",
            },
        )
        assert result["empty"] is True
        assert result["error"] == "period_incomplete"
        assert "nie są kompletne" in result["warning"]

    @pytest.mark.asyncio
    async def test_service_refuses_unknown_or_not_yet_computed_period(self):
        # Feature active (store exists) but no verdict for this meter yet.
        coordinator = SimpleNamespace(_period_completeness={}, _verify_cache={})
        hass = self._hass(coordinator)
        result = await async_verify_period_data(
            hass,
            {
                "start": "2026-08-03",
                "end": "2026-08-04",
                "entry_id": "entry_1",
                "meter_id": "1",
            },
        )
        assert result["empty"] is True
        assert result["error"] == "period_incomplete"
        assert "nie został jeszcze ustalony" in result["warning"]

    @pytest.mark.asyncio
    async def test_service_refuses_stale_period_verdict(self):
        coordinator = SimpleNamespace(
            _period_completeness={
                "1": {
                    "state": STATE_COMPLETE,
                    "period_start": "2026-07-01",
                    "period_end": "2026-07-31",
                }
            },
            _verify_cache={},
        )
        hass = self._hass(coordinator)
        result = await async_verify_period_data(
            hass,
            {
                "start": "2026-08-03",
                "end": "2026-08-04",
                "entry_id": "entry_1",
                "meter_id": "1",
            },
        )
        assert result["error"] == "period_incomplete"

    @pytest.mark.asyncio
    async def test_service_proceeds_without_completeness_infrastructure(self):
        # No store at all (feature unavailable) -> do not gate.
        coordinator = SimpleNamespace(_verify_cache={})
        hass = self._hass(coordinator)
        with patch(
            "custom_components.energa_mobile.services._active_meters_for_period",
            new=AsyncMock(return_value=[]),
        ):
            result = await async_verify_period_data(
                hass,
                {
                    "start": "2026-08-03",
                    "end": "2026-08-04",
                    "entry_id": "entry_1",
                    "meter_id": "1",
                },
            )
        assert result.get("error") != "period_incomplete"


class TestDateChangeDoesNotReload:
    @pytest.mark.asyncio
    async def test_only_dates_changed_no_reload(self):
        from custom_components.energa_mobile import _async_options_updated

        coord = SimpleNamespace(async_update_listeners=MagicMock())
        entry = MagicMock()
        entry.entry_id = "entry_1"
        entry.options = {
            CONF_VERIFY_PERIOD_START: "2026-08-02",
            CONF_VERIFY_PERIOD_END: "2026-08-04",
        }
        hass = MagicMock()
        hass.data = {
            DOMAIN: {
                "entry_1": {
                    "coordinator": coord,
                    "_options_snapshot": {
                        CONF_VERIFY_PERIOD_START: "2026-08-03",
                        CONF_VERIFY_PERIOD_END: "2026-08-04",
                    },
                    "api": MagicMock(),
                }
            }
        }
        await _async_options_updated(hass, entry)

        hass.config_entries.async_reload.assert_not_called()
        coord.async_update_listeners.assert_called_once()
