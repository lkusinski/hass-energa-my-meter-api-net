"""Unit tests for service registration and dispatch in services.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.energa_mobile.services as _services
from custom_components.energa_mobile.const import DOMAIN
from custom_components.energa_mobile.services import (
    _async_verify_period,
    _get_entry_and_api,
    async_register_services,
    async_unregister_services,
)


def _verify_hass(meter: dict | None = None):
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = {}
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    hass.data = {
        DOMAIN: {
            "entry_1": {
                "coordinator": MagicMock(
                    data=[
                        meter
                        or {
                            "meter_point_id": "1",
                            "meter_serial": "S1",
                            "zone_count": 1,
                            "total_plus": 10.0,
                            "tariff": "G12W",
                        }
                    ]
                ),
                "api": MagicMock(),
            }
        }
    }
    call = MagicMock()
    call.data = {"start": "2026-08-01", "end": "2026-08-31"}
    return hass, call


class TestVerifyPeriodService:
    @pytest.mark.asyncio
    async def test_empty_window_returns_empty_response(self):
        hass, call = _verify_hass()
        with patch.object(
            _services, "_collect_meter_hourly", new=AsyncMock(return_value={})
        ):
            res = await _async_verify_period(hass, call)

        assert res["empty"] is True
        assert res["source"] == "recorder_hourly"
        assert res["meters"][0]["error"] == "no_data"
        assert res["netto"] == 0.0
        assert res["do_zaplaty"] == 0.0
        assert res["period_start"] == "2026-08-01T00:00:00+02:00"

    @pytest.mark.asyncio
    async def test_non_empty_window_returns_flat_breakdown(self):
        hass, call = _verify_hass()
        with patch.object(
            _services,
            "_collect_meter_hourly",
            new=AsyncMock(return_value={"import_1": {0: 100.0}}),
        ):
            res = await _async_verify_period(hass, call)

        assert res["empty"] is False
        assert res["source"] == "recorder_hourly"
        assert res["kwh"]["saldo_plus_1"] == 100.0
        assert res["meter_serial"] == "S1"
        assert "netto" in res and "brutto" in res
        assert isinstance(res["netto"], float)

    @pytest.mark.asyncio
    async def test_invalid_period_returns_error_without_raising(self):
        hass, call = _verify_hass()
        call.data = {"start": "not-a-date", "end": "2026-08-31"}
        res = await _async_verify_period(hass, call)
        assert res["empty"] is True
        assert res["error"] == "invalid_period"


@pytest.mark.asyncio
async def test_service_registration_and_unregistration():
    """Verify services are registered and properly removed upon unload."""
    hass = MagicMock()
    registered_services = {}

    def mock_has_service(domain, name):
        return (domain, name) in registered_services

    def mock_register(domain, name, handler, schema=None, **kwargs):
        registered_services[(domain, name)] = handler

    def mock_remove(domain, name):
        registered_services.pop((domain, name), None)

    hass.services.has_service = mock_has_service
    hass.services.async_register = mock_register
    hass.services.async_remove = mock_remove

    # 1. Register services
    await async_register_services(hass)
    assert (DOMAIN, "fetch_history") in registered_services
    assert (DOMAIN, "generate_dashboard") in registered_services
    assert (DOMAIN, "reconcile_invoice") in registered_services
    assert (DOMAIN, "verify_period") in registered_services
    assert (DOMAIN, "clear_period") in registered_services

    # 2. Idempotent registration
    await async_register_services(hass)
    assert len(registered_services) == 5

    # 3. Unregister services
    await async_unregister_services(hass)
    assert (DOMAIN, "fetch_history") not in registered_services
    assert (DOMAIN, "generate_dashboard") not in registered_services
    assert (DOMAIN, "reconcile_invoice") not in registered_services
    assert (DOMAIN, "verify_period") not in registered_services
    assert (DOMAIN, "clear_period") not in registered_services


@pytest.mark.asyncio
async def test_clear_period_removes_saved_dates():
    """The clear_period action drops verify_period_start/end from options."""
    from custom_components.energa_mobile.const import (
        CONF_VERIFY_PERIOD_END,
        CONF_VERIFY_PERIOD_START,
    )

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = {
        CONF_VERIFY_PERIOD_START: "2026-08-01",
        CONF_VERIFY_PERIOD_END: "2026-08-31",
        "other_option": 1,
    }
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    updated: dict = {}
    hass.config_entries.async_update_entry = MagicMock(
        side_effect=lambda e, options=None: updated.update(options or {})
    )

    registered_services = {}
    hass.services.has_service = lambda domain, name: False
    hass.services.async_register = (
        lambda domain, name, handler, schema=None, **kw: registered_services.__setitem__(
            name, handler
        )
    )

    await async_register_services(hass)
    await registered_services["clear_period"](MagicMock(data={"entry_id": "entry_1"}))

    assert CONF_VERIFY_PERIOD_START not in updated
    assert CONF_VERIFY_PERIOD_END not in updated
    assert updated.get("other_option") == 1


def test_get_entry_and_api():
    """Test resolution of entry and API for service invocations."""
    hass = MagicMock()
    entry1 = MagicMock()
    entry1.entry_id = "entry_1"
    api1 = MagicMock()

    hass.config_entries.async_entries = MagicMock(return_value=[entry1])
    hass.data = {DOMAIN: {"entry_1": {"api": api1}}}

    # Single entry resolution
    entry, api = _get_entry_and_api(hass)
    assert entry == entry1
    assert api == api1

    # Multiple entries with meter_id match
    entry2 = MagicMock()
    entry2.entry_id = "entry_2"
    api2 = MagicMock()
    coord2 = MagicMock()
    coord2.data = [{"meter_point_id": "999", "meter_serial": "SERIAL_999"}]

    hass.config_entries.async_entries = MagicMock(return_value=[entry1, entry2])
    hass.data[DOMAIN]["entry_2"] = {"api": api2, "coordinator": coord2}

    matched_entry, matched_api = _get_entry_and_api(hass, meter_id="SERIAL_999")
    assert matched_entry == entry2
    assert matched_api == api2
