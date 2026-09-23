"""Tests for 1.9.0-beta.8: consumer label, duplicate-domain guard, fast setup.

Covers the three defects fixed in beta.8:
  A) sensor platform setup must not trigger a second coordinator refresh
     (``update_before_add=False``) nor re-await the API when the coordinator
     already has data — the root cause of HA's "taking over 10 seconds".
  B) a one-way consumer must be labelled ``consumer`` and must not emit
     deposit warnings; prosumer behaviour is unchanged.
  C) any other custom_components folder declaring our domain must be reported
     via Repairs + persistent notification, and cleared when it disappears.
"""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.energa_mobile as init_mod
from custom_components.energa_mobile import sensor as sensor_mod
from custom_components.energa_mobile.const import DOMAIN
from custom_components.energa_mobile.core.verification import build_period_invoice
from custom_components.energa_mobile.sensors.bill import EnergaBillForecastSensor
from custom_components.energa_mobile.settlement import (
    scan_for_domain_duplicates,
    settlement_system_label,
    settlement_system_name,
)


# ---------------------------------------------------------------------------
# B) settlement enum + consumer invoice
# ---------------------------------------------------------------------------
class TestSettlementSystemName:
    def test_consumer(self):
        assert settlement_system_name(False, False) == "consumer"
        assert settlement_system_label(False, False) == "konsument (jednokierunkowy)"

    def test_net_metering_prosumer_unchanged(self):
        assert settlement_system_name(True, True) == "net_metering"
        assert settlement_system_label(True, True) == "stare net-metering (magazyn kWh)"

    def test_net_billing_prosumer_unchanged(self):
        assert settlement_system_name(True, False) == "net_billing"
        assert settlement_system_label(True, False) == "nowe net-billing (depozyt PLN)"


class TestConsumerPeriodInvoice:
    """A one-way consumer must be ``consumer`` with no deposit warning."""

    FEES = {
        "energy_day": 0.605286,
        "energy_night": 0.0,
        "excise_mwh": 5.0,
        "trade_fee": 20.32,
        "abonament": 0.74,
        "grid_fixed": 11.77,
        "grid_var_day": 0.3485,
        "grid_var_night": 0.0,
        "quality": 0.0332,
        "oze": 0.0073,
        "cogen": 0.0030,
        "capacity": 24.05,
    }

    def _res(self):
        return build_period_invoice(
            {"import_1": {0: 120.0}},
            fees=self.FEES,
            rcem=0.29453,
            months=1,
            old_system=False,
            is_prosumer=False,
        )

    def test_system_is_consumer(self):
        res = self._res()
        assert res["system"] == "consumer"
        assert res["old_system"] is False

    def test_no_deposit_warning_and_zero_deposit(self):
        res = self._res()
        assert res["warnings"] == []
        assert res["coverage_unknown"] is False
        assert res["deposit"] == 0.0
        assert res["deposit_generated"] == 0.0
        assert res["deposit_open"] == 0.0

    def test_kwh_is_import_only(self):
        res = self._res()
        assert res["kwh"]["saldo_plus_1"] == 120.0
        assert res["kwh"]["saldo_minus_1"] == 0.0
        assert res["kwh"]["cover_1"] == 0.0

    def test_prosumer_default_still_net_billing_and_warns(self):
        """Default (is_prosumer omitted) preserves the legacy prosumer path."""
        res = build_period_invoice(
            {"import_1": {0: 120.0}}, fees=self.FEES, rcem=0.29453,
            months=1, old_system=False,
        )
        assert res["system"] == "net_billing"
        assert any("depozytu" in w for w in res["warnings"])


class TestBillSensorConsumerLabel:
    """The bill sensor attributes must not lie about a plain consumer."""

    def _sensor(self, meter):
        coordinator = MagicMock()
        coordinator.data = [meter]
        coordinator._mtd = {
            str(meter["meter_point_id"]): {"import": 100.0, "export": 0.0}
        }
        coordinator._meter_totals = {
            str(meter["meter_point_id"]): {"import": 1000.0, "export": 0.0}
        }
        coordinator._rce_cache = 0.29453
        coordinator._rce_source = "manual"
        coordinator._rolling_365 = {}
        coordinator._profile_forecast_cache = {}
        coordinator.storage = None
        entry = MagicMock()
        entry.options = {"rce_auto_fetch": True, "prosumer_coefficient": 0.0}
        entry.data = {}
        return EnergaBillForecastSensor(
            coordinator=coordinator,
            meter_id=str(meter["meter_point_id"]),
            device_info=MagicMock(),
            entry=entry,
            serial=str(meter["meter_serial"]),
        )

    def test_consumer_gets_consumer_label(self):
        meter = {
            "meter_point_id": 30910550,
            "meter_serial": "30910550",
            "tariff": "G11",
            "total_plus": 1000.0,
            "total_minus": 0.0,
        }
        sensor = self._sensor(meter)
        assert sensor._is_consumer() is True
        _ = sensor.native_value  # evaluate attributes
        attrs = sensor._attr_extra_state_attributes
        assert attrs["system"] == "konsument (jednokierunkowy)"
        assert attrs["settlement_type"] == "consumer"
        assert "hourly_netting_note" not in attrs
        assert "note" not in attrs

    def test_prosumer_label_unchanged(self):
        meter = {
            "meter_point_id": 11685328,
            "meter_serial": "11685328",
            "tariff": "G12W",
            "total_plus": 1000.0,
            "total_minus": 500.0,
        }
        sensor = self._sensor(meter)
        assert sensor._is_consumer() is False
        _ = sensor.native_value
        attrs = sensor._attr_extra_state_attributes
        assert attrs["system"] == "nowe net-billing (depozyt PLN)"
        assert attrs["settlement_type"] == "net_billing_rcem"


# ---------------------------------------------------------------------------
# C) duplicate-domain detection
# ---------------------------------------------------------------------------
def _write_manifest(folder, payload, raw=None):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "manifest.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestScanForDomainDuplicates:
    def test_finds_backup_copy_of_our_domain(self, tmp_path):
        _write_manifest(
            tmp_path / "energa_mobile",
            {"domain": "energa_mobile", "name": "Live", "version": "1.9.0-beta.8"},
        )
        _write_manifest(
            tmp_path / "energa_mobile.prebak",
            {"domain": "energa_mobile", "name": "Backup", "version": "1.9.0-beta.2"},
        )
        hits = scan_for_domain_duplicates(str(tmp_path), "energa_mobile")
        assert len(hits) == 1
        assert hits[0]["path"].endswith("energa_mobile.prebak")
        assert hits[0]["name"] == "Backup"

    def test_ignores_other_domains_and_live_dir(self, tmp_path):
        _write_manifest(tmp_path / "energa_mobile", {"domain": "energa_mobile"})
        _write_manifest(tmp_path / "solis", {"domain": "solis"})
        _write_manifest(tmp_path / "hacs", {"domain": "hacs"})
        assert scan_for_domain_duplicates(str(tmp_path), "energa_mobile") == []

    def test_no_hits_on_clean_dir(self, tmp_path):
        _write_manifest(tmp_path / "energa_mobile", {"domain": "energa_mobile"})
        assert scan_for_domain_duplicates(str(tmp_path), "energa_mobile") == []

    def test_broken_json_and_missing_dir_are_skipped(self, tmp_path):
        _write_manifest(tmp_path / "bad", None, raw="{nope")
        assert scan_for_domain_duplicates(str(tmp_path / "missing"), "energa_mobile") == []
        assert scan_for_domain_duplicates(str(tmp_path), "energa_mobile") == []


class TestDetectDuplicateDomain:
    @pytest.mark.asyncio
    async def test_hit_creates_issue_and_notification(self):
        hass = MagicMock()
        hits = [{"path": "/c/energa_mobile.prebak", "name": "Backup", "domain": "energa_mobile"}]
        hass.async_add_executor_job = AsyncMock(return_value=hits)

        with patch.object(init_mod.ir, "async_create_issue") as create_issue, patch.object(
            init_mod.persistent_notification, "async_create"
        ) as create_note:
            await init_mod._async_detect_duplicate_domain(hass)

        create_issue.assert_called_once()
        args, kwargs = create_issue.call_args
        assert args[2] == init_mod.DUPLICATE_DOMAIN_ISSUE_ID
        assert kwargs["translation_key"] == init_mod.DUPLICATE_DOMAIN_ISSUE_ID
        assert "energa_mobile.prebak" in kwargs["translation_placeholders"]["paths"]
        create_note.assert_called_once()
        assert create_note.call_args.kwargs["notification_id"] == init_mod.DUPLICATE_DOMAIN_ISSUE_ID

    @pytest.mark.asyncio
    async def test_no_hit_deletes_issue_and_dismisses(self):
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(return_value=[])

        with patch.object(init_mod.ir, "async_delete_issue") as del_issue, patch.object(
            init_mod.persistent_notification, "async_dismiss"
        ) as dismiss:
            await init_mod._async_detect_duplicate_domain(hass)

        del_issue.assert_called_once_with(
            hass, init_mod.DOMAIN, init_mod.DUPLICATE_DOMAIN_ISSUE_ID
        )
        dismiss.assert_called_once_with(hass, init_mod.DUPLICATE_DOMAIN_ISSUE_ID)

    @pytest.mark.asyncio
    async def test_hit_lists_every_duplicate_path(self):
        hass = MagicMock()
        hits = [
            {"path": "/c/energa_mobile.prebak", "name": "B", "domain": "energa_mobile"},
            {"path": "/c/energa_mobile.dup_test", "name": "D", "domain": "energa_mobile"},
        ]
        hass.async_add_executor_job = AsyncMock(return_value=hits)
        with patch.object(init_mod.ir, "async_create_issue") as create_issue, patch.object(
            init_mod.persistent_notification, "async_create"
        ):
            await init_mod._async_detect_duplicate_domain(hass)
        paths = create_issue.call_args.kwargs["translation_placeholders"]["paths"]
        assert "energa_mobile.prebak" in paths
        assert "energa_mobile.dup_test" in paths


# ---------------------------------------------------------------------------
# A) fast platform setup
# ---------------------------------------------------------------------------
def _make_hass(coordinator, api):
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.options = {}
    entry.data = {}
    # Swallow the background coroutine the platform schedules (we only assert
    # setup behaviour; letting it dangle raises a RuntimeWarning at gc time).
    entry.async_create_background_task.side_effect = (
        lambda _hass, coro, *args, **kwargs: coro.close()
    )
    hass = MagicMock()
    hass.data = {DOMAIN: {entry.entry_id: {"api": api, "coordinator": coordinator}}}
    return hass, entry


class TestFastPlatformSetup:
    @pytest.mark.asyncio
    async def test_uses_coordinator_data_and_no_update_before_add(self):
        meter = {
            "meter_point_id": 30910550,
            "meter_serial": "30910550",
            "tariff": "G11",
            "total_plus": 1000.0,
            "total_minus": 0.0,
        }
        coordinator = MagicMock()
        coordinator.data = [meter]
        api = MagicMock()
        api.async_get_data = AsyncMock()
        hass, entry = _make_hass(coordinator, api)
        add_entities = MagicMock()

        ent_registry = MagicMock()
        ent_registry.entities = {}
        dev_registry = MagicMock()
        dev_registry.async_entries_for_config_entry.return_value = []

        integration = MagicMock()
        integration.version = "1.9.0-beta.8"

        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        with patch.object(
            sensor_mod, "async_get_integration", AsyncMock(return_value=integration)
        ), patch.object(sensor_mod.dr, "async_get", return_value=dev_registry), patch.object(
            er_mod, "async_get", return_value=ent_registry
        ):
            await sensor_mod.async_setup_entry(hass, entry, add_entities)

        api.async_get_data.assert_not_awaited()
        add_entities.assert_called_once()
        assert add_entities.call_args.kwargs["update_before_add"] is False

    @pytest.mark.asyncio
    async def test_falls_back_to_api_when_coordinator_empty(self):
        meter = {
            "meter_point_id": 30910550,
            "meter_serial": "30910550",
            "tariff": "G11",
            "total_plus": 1000.0,
            "total_minus": 0.0,
        }
        coordinator = MagicMock()
        coordinator.data = None
        api = MagicMock()
        api.async_get_data = AsyncMock(return_value=[meter])
        hass, entry = _make_hass(coordinator, api)
        add_entities = MagicMock()

        ent_registry = MagicMock()
        ent_registry.entities = {}
        dev_registry = MagicMock()
        dev_registry.async_entries_for_config_entry.return_value = []
        integration = MagicMock()
        integration.version = "1.9.0-beta.8"

        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        with patch.object(
            sensor_mod, "async_get_integration", AsyncMock(return_value=integration)
        ), patch.object(sensor_mod.dr, "async_get", return_value=dev_registry), patch.object(
            er_mod, "async_get", return_value=ent_registry
        ):
            await sensor_mod.async_setup_entry(hass, entry, add_entities)

        api.async_get_data.assert_awaited_once_with(force_refresh=False)
        add_entities.assert_called_once()
