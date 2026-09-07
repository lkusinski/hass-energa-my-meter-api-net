"""Tests for Home Assistant diagnostics and PII redaction.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 10 & 14.
Kryteria akceptacji:
- Brak sekretów, tokenów, PESEL i danych wrażliwych w diagnostyce.
- Kompletność drzewa diagnostycznego (entry, coordinator, storage, alerts).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from custom_components.energa_mobile.diagnostics import (
    async_get_config_entry_diagnostics,
    redact_sensitive_data,
)
from custom_components.energa_mobile.const import DOMAIN


def test_redact_sensitive_data():
    """Verify recursive redaction of sensitive PII keys."""
    raw = {
        "username": "user123",
        "password": "SuperSecretPassword123!",
        "token": "bearer_abc_xyz",
        "pesel": "90010112345",
        "nested": {
            "street_address": "ul. Kwiatowa 5",
            "phone_number": "+48 500 600 700",
            "safe_param": 42,
        },
        "items": [
            {"email": "test@example.com", "tariff": "G11"},
            {"code": "XYZ"},
        ],
    }

    redacted = redact_sensitive_data(raw)

    assert redacted["username"] == "user123"
    assert redacted["password"] == "**REDACTED**"
    assert redacted["token"] == "**REDACTED**"
    assert redacted["pesel"] == "**REDACTED**"
    assert redacted["nested"]["street_address"] == "**REDACTED**"
    assert redacted["nested"]["phone_number"] == "**REDACTED**"
    assert redacted["nested"]["safe_param"] == 42
    assert redacted["items"][0]["email"] == "**REDACTED**"
    assert redacted["items"][0]["tariff"] == "G11"
    assert redacted["items"][1]["code"] == "XYZ"


@pytest.mark.asyncio
async def test_async_get_config_entry_diagnostics():
    """Verify diagnostic generation with mocked Home Assistant components."""
    hass_mock = MagicMock()
    hass_mock.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    entry_mock = MagicMock()
    entry_mock.entry_id = "test_entry_456"
    entry_mock.title = "Dom Wiśniowa"
    entry_mock.domain = DOMAIN
    entry_mock.version = 1
    entry_mock.data = {
        "username": "user@test.pl",
        "password": "SECRET_PASSWORD",
        "ppe_id": "PL_TEST_001",
    }
    entry_mock.options = {
        "tariff": "G11",
        "secret_token": "TOK123",
    }

    coordinator_mock = MagicMock()
    coordinator_mock.last_update_success = True
    coordinator_mock.data = [{"meter_serial": "123456"}]

    storage_mock = MagicMock()
    storage_mock.get_schema_version.return_value = 2
    storage_mock.get_readings_count.return_value = 1450
    storage_mock.get_latest_reading_time.return_value = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
    storage_mock.get_settlement_lots.return_value = []
    storage_mock.db_path = "/tmp/test.db"
    storage_mock._connection = MagicMock()
    cur_mock = MagicMock()
    storage_mock._connection.cursor.return_value = cur_mock
    cur_mock.execute.return_value.fetchone.side_effect = [
        ("2026-08-01 00:00:00", "2026-09-07 12:00:00"),  # bounds
        (10,),  # market_prices
        (5,),   # settlement_lots
        (1,),   # reconciliations
        (2,),   # checkpoints
    ]

    hass_mock.data = {
        DOMAIN: {
            "test_entry_456": {
                "coordinator": coordinator_mock,
                "storage": storage_mock,
            }
        }
    }

    diag = await async_get_config_entry_diagnostics(hass_mock, entry_mock)

    assert diag["entry"]["title"] == "Dom Wiśniowa"
    assert diag["entry"]["data"]["password"] == "**REDACTED**"
    assert diag["entry"]["options"]["secret_token"] == "**REDACTED**"
    assert diag["coordinator"]["last_update_success"] is True
    assert diag["coordinator"]["meters_count"] == 1
    assert diag["storage"]["schema_version"] == 2
    assert diag["storage"]["readings_count"] == 1450
