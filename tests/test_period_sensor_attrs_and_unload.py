"""Regression tests for v1.9.0-beta.6.

Two defects from the user review:

1. ``sensor.*_weryfikacja_rachunku`` copied only ``_BREAKDOWN_KEYS`` and that
   tuple was missing ``fee_source`` (the service returned ``options`` /
   ``partial`` / ``defaults`` but the attribute read ``None``). The sensor now
   exposes ``fee_source`` and every ``build_period_invoice`` key (including
   ``system``, ``coverage_unknown``, ``warnings`` and the full ``kwh`` block
   with ``cover_1/2`` and ``bank_open/close_1/2``).

2. ``ha core stop`` / unload logged
   ``ERROR ... Setup of config entry ... cancelled`` plus a traceback from
   ``_async_update_profile_forecasts``. The coordinator now tracks that task and
   cancels it from ``async_unload_entry`` (and ``async_shutdown``).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.energa_mobile import _async_cancel_coordinator_tasks


def _sensor(result):
    from custom_components.energa_mobile.sensors.period import (
        EnergaPeriodVerificationSensor,
    )

    coordinator = SimpleNamespace(
        _verify_result={"10000001": result} if result else {}
    )
    return EnergaPeriodVerificationSensor(
        coordinator=coordinator,
        meter_id="10000001",
        serial="10000001",
        device_info=MagicMock(),
        entry=MagicMock(),
    )


class TestFeeSourceExposedOnSensor:
    def test_fee_source_attribute_is_present(self):
        sensor = _sensor(
            {
                "empty": False,
                "do_zaplaty": 158.72,
                "fee_source": "options",
                "kwh": {},
            }
        )
        assert sensor.extra_state_attributes["fee_source"] == "options"

    def test_full_faza3_breakdown_matches_build_period_invoice(self):
        """Every key of ``build_period_invoice`` reaches the attributes."""
        from custom_components.energa_mobile.core.verification import (
            build_period_invoice,
        )

        hourly = {"import_1": {0: 100.0}, "export_1": {0: 120.0}}
        result = build_period_invoice(
            hourly,
            fees={},
            rcem=0.3,
            old_system=True,
            bank_open_1=50.0,
            bank_open_2=10.0,
        )
        result.update(
            {
                "empty": False,
                "fee_source": "options",
                "status": "ok",
                "source": "energa_api",
            }
        )
        attrs = _sensor(result).extra_state_attributes

        # Previously missing on the sensor:
        assert attrs["fee_source"] == "options"
        assert attrs["system"] == "net_metering"
        assert attrs["coverage_unknown"] is False
        assert attrs["warnings"] == []
        # Full kwh block, incl. warehouse cover/bank state.
        for key in (
            "cover_1",
            "cover_2",
            "bank_open_1",
            "bank_open_2",
            "bank_close_1",
            "bank_close_2",
        ):
            assert key in attrs["kwh"], f"kwh.{key} missing"
        assert attrs["kwh"]["cover_1"] == 0.0
        assert attrs["kwh"]["bank_open_1"] == 50.0
        assert attrs["kwh"]["bank_open_2"] == 10.0
        assert attrs["kwh"]["bank_close_1"] == 146.0
        assert "missing_breakdown_keys" not in attrs

    def test_missing_keys_are_reported_not_silent(self):
        sensor = _sensor(
            {
                "empty": False,
                "do_zaplaty": 1.0,
                "fee_source": "defaults",
                "kwh": {"cover_1": 1.0},
            }
        )
        attrs = sensor.extra_state_attributes
        assert "missing_breakdown_keys" in attrs
        assert "system" in attrs["missing_breakdown_keys"]


class TestProfileForecastTaskCancellation:
    @pytest.mark.asyncio
    async def test_shutdown_cancels_pending_task_without_error(self):
        from custom_components.energa_mobile.coordinator import EnergaCoordinator

        coordinator = EnergaCoordinator.__new__(EnergaCoordinator)
        started = asyncio.Event()

        async def _never_finishes():
            started.set()
            await asyncio.sleep(3600)

        coordinator._profile_forecast_task = asyncio.get_running_loop().create_task(
            _never_finishes()
        )
        await started.wait()

        await coordinator.async_shutdown()

        assert coordinator._profile_forecast_task is None
        # The child task completed via cancellation (no pending task leak).
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_cancel_helper_swallows_missing_coordinator(self):
        await _async_cancel_coordinator_tasks(None, "entry_1")
        await _async_cancel_coordinator_tasks(SimpleNamespace(), "entry_1")

    @pytest.mark.asyncio
    async def test_cancel_helper_never_raises_on_bad_shutdown(self):
        class _Boom:
            async def async_shutdown(self):
                raise RuntimeError("boom")

        await _async_cancel_coordinator_tasks(_Boom(), "entry_1")

    @pytest.mark.asyncio
    async def test_running_refresh_is_cancelled_and_reraises(self):
        """Cancelling the coordinator task stops the child and propagates."""
        from custom_components.energa_mobile.coordinator import EnergaCoordinator

        coordinator = EnergaCoordinator.__new__(EnergaCoordinator)
        child_cancelled = asyncio.Event()

        async def _slow_profile(active_meters):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                child_cancelled.set()
                raise

        coordinator._async_update_profile_forecasts = _slow_profile
        coordinator._profile_forecast_task = None

        parent = asyncio.get_running_loop().create_task(
            coordinator._async_refresh_profile_forecasts([])
        )
        # Let the child task start.
        for _ in range(5):
            await asyncio.sleep(0)

        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent

        assert child_cancelled.is_set()
        assert coordinator._profile_forecast_task is None

    @pytest.mark.asyncio
    async def test_shutdown_await_is_safe_when_already_done(self):
        from custom_components.energa_mobile.coordinator import EnergaCoordinator

        coordinator = EnergaCoordinator.__new__(EnergaCoordinator)

        async def _quick():
            return None

        task = asyncio.get_running_loop().create_task(_quick())
        await task
        coordinator._profile_forecast_task = task
        # Already finished -> no cancellation, no raise.
        await coordinator.async_shutdown()
