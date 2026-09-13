"""Unit tests for service registration and dispatch in services.py."""

from unittest.mock import MagicMock

import pytest

from custom_components.energa_mobile.const import DOMAIN
from custom_components.energa_mobile.services import (
    _get_entry_and_api,
    async_register_services,
    async_unregister_services,
)


@pytest.mark.asyncio
async def test_service_registration_and_unregistration():
    """Verify services are registered and properly removed upon unload."""
    hass = MagicMock()
    registered_services = {}

    def mock_has_service(domain, name):
        return (domain, name) in registered_services

    def mock_register(domain, name, handler, schema=None):
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

    # 2. Idempotent registration
    await async_register_services(hass)
    assert len(registered_services) == 3

    # 3. Unregister services
    await async_unregister_services(hass)
    assert (DOMAIN, "fetch_history") not in registered_services
    assert (DOMAIN, "generate_dashboard") not in registered_services
    assert (DOMAIN, "reconcile_invoice") not in registered_services


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
