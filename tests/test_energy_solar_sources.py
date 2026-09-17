"""Tests for native Energy panel PV source detection (defensive helper).

The helper must never raise: an unconfigured Energy dashboard, a missing
manager or unexpected data all degrade to an empty result.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.energa_mobile.core.energy_sources import (
    async_energy_solar_sources,
    async_has_energy_solar,
)

_GET_MANAGER = "homeassistant.components.energy.data.async_get_manager"


def _manager_with(sources):
    manager = MagicMock()
    manager.data = {"energy_sources": sources}
    return manager


def _patch_manager(manager):
    return patch(_GET_MANAGER, AsyncMock(return_value=manager))


class TestAsyncEnergySolarSources:
    async def test_missing_manager_returns_empty(self):
        with _patch_manager(None):
            assert await async_energy_solar_sources(MagicMock()) == []

    async def test_manager_error_returns_empty(self):
        with patch(_GET_MANAGER, AsyncMock(side_effect=RuntimeError("no energy"))):
            assert await async_energy_solar_sources(MagicMock()) == []

    async def test_non_dict_data_returns_empty(self):
        manager = MagicMock()
        manager.data = None
        with _patch_manager(manager):
            assert await async_energy_solar_sources(MagicMock()) == []

    async def test_solar_source_detected(self):
        manager = _manager_with(
            [
                {"type": "grid"},
                {"type": "solar", "stat_energy_from": "sensor.pv_total"},
            ]
        )
        with _patch_manager(manager):
            sources = await async_energy_solar_sources(MagicMock())
        assert len(sources) == 1
        assert sources[0]["stat_energy_from"] == "sensor.pv_total"

    async def test_grid_and_battery_only_returns_empty(self):
        manager = _manager_with(
            [{"type": "grid"}, {"type": "battery"}, {"type": "gas"}]
        )
        with _patch_manager(manager):
            assert await async_energy_solar_sources(MagicMock()) == []


class TestAsyncHasEnergySolar:
    async def test_true_when_solar_present(self):
        with _patch_manager(_manager_with([{"type": "solar"}])):
            assert await async_has_energy_solar(MagicMock()) is True

    async def test_false_when_no_solar(self):
        with _patch_manager(_manager_with([{"type": "grid"}])):
            assert await async_has_energy_solar(MagicMock()) is False

    async def test_false_when_manager_missing(self):
        with _patch_manager(None):
            assert await async_has_energy_solar(MagicMock()) is False
