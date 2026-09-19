"""Period verification result sensor for Energa My Meter (Faza 2).

Renders the latest ``energa_mobile.verify_period`` result computed by the
``Przelicz okres rozliczeniowy`` button. The state is the amount payable (PLN) for the
chosen period; the full invoice breakdown lives in the attributes. No
``state_class``/``device_class`` energy is set, so this entity can never
pollute the Home Assistant Energy Dashboard statistics.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

_LOGGER = logging.getLogger(__name__)

# Invoice result keys copied verbatim into the sensor attributes.
_BREAKDOWN_KEYS = (
    "sale_energy_day",
    "sale_energy_night",
    "sale_total",
    "sale_gross",
    "excise",
    "excise_day",
    "excise_night",
    "excise_added",
    "excise_note",
    "trade_fee",
    "distr_var_day",
    "distr_var_night",
    "distr_quality",
    "distr_oze",
    "distr_cogen",
    "distr_abonament",
    "distr_grid_fixed",
    "distr_capacity",
    "distr_fixed",
    "distr_total",
    "distr_gross",
    "netto",
    "vat",
    "brutto",
    "deposit",
    "deposit_generated",
    "deposit_open",
    "deposit_applied",
    "deposit_close",
    "do_zaplaty",
    "old_system",
    "system",
    "tariff",
    "prosumer_coefficient",
    "kwh_source",
    "fee_source",
    "months",
    "coverage_unknown",
    "warnings",
)

# Full ``kwh`` block keys produced by ``build_period_invoice`` (Faza 3). The
# sensor copies the whole ``kwh`` dict verbatim anyway; keeping the expected set
# here documents the contract and lets ``_missing_breakdown_keys`` flag drift.
_KWH_KEYS = (
    "saldo_plus_1",
    "saldo_plus_2",
    "saldo_minus_1",
    "saldo_minus_2",
    "gross_1",
    "gross_2",
    "gross_import_1",
    "gross_import_2",
    "overlap_1",
    "overlap_2",
    "cover_1",
    "cover_2",
    "bank_open_1",
    "bank_open_2",
    "bank_close_1",
    "bank_close_2",
)

# Warehouse keys that only exist for the old net-metering system.
_NET_METERING_KWH_KEYS = frozenset(
    {"bank_open_1", "bank_open_2", "bank_close_1", "bank_close_2"}
)


def _missing_breakdown_keys(result: dict) -> list[str]:
    """Invoice keys ``build_period_invoice`` promises but the result lacks.

    Diagnostic helper (also asserted by tests): it makes a silent omission of a
    line item — exactly the ``fee_source`` bug fixed in v1.9.0-beta.6 — visible
    in the entity attributes and the log instead of hiding in an absent key.

    The net-metering warehouse keys (``bank_open_*``/``bank_close_*``) are only
    promised for the old (net-metering) system: a net-billing result has no
    warehouse, so flagging them produced a spurious "missing keys" warning on
    the agrestowa lab (2026-09-18). Placeholder results ("calculating") and
    empty-period replies carry no breakdown by design and are skipped too.
    """
    if result.get("status") == "calculating" or result.get("empty"):
        return []
    missing = [key for key in _BREAKDOWN_KEYS if key not in result]
    kwh = result.get("kwh")
    if isinstance(kwh, dict):
        is_net_billing = (
            result.get("old_system") is False
            or str(result.get("system") or "") == "net_billing"
        )
        for key in _KWH_KEYS:
            if is_net_billing and key in _NET_METERING_KWH_KEYS:
                continue
            if key not in kwh:
                missing.append(key)
    return missing


class EnergaPeriodVerificationSensor(CoordinatorEntity, SensorEntity):
    """Latest arbitrary-period invoice verification result."""

    _attr_has_entity_name = True
    _attr_name = "Okres: rozliczenie"
    _attr_translation_key = "period_verification"
    _attr_icon = "mdi:receipt-text-check-outline"
    _attr_native_unit_of_measurement = "PLN"
    # Diagnostic entity: Home Assistant rejects EntityCategory.CONFIG on a
    # sensor ("cannot be added as the entity category is set to config"), so
    # the result lives in the Diagnostic section — still outside "Sensors".
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Deliberately no state_class / energy device_class: this is a computed
    # period result, never an Energy Dashboard source (Faza 2 requirement).
    _attr_state_class = None

    def __init__(
        self,
        coordinator,
        meter_id: str,
        serial: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._meter_id = str(meter_id)
        self._serial = str(serial or meter_id)
        self._entry = entry
        self._attr_unique_id = f"energa_{self._meter_id}_period_verification"
        self.entity_id = (
            f"sensor.energa_{self._serial}_weryfikacja_rachunku".lower()
        )
        self._attr_device_info = device_info

    def _result(self) -> dict | None:
        store = getattr(self.coordinator, "_verify_result", None)
        if not isinstance(store, dict):
            return None
        result = store.get(self._meter_id)
        if result is None:
            result = store.get(self._serial)
        return result if isinstance(result, dict) else None

    @property
    def native_value(self):
        """Payable amount for the period, or ``None`` when unavailable."""
        result = self._result()
        if (
            not result
            or result.get("empty")
            or result.get("status") == "calculating"
        ):
            return None
        try:
            return round(float(result.get("do_zaplaty")), 2)
        except (ValueError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> dict:
        result = self._result()
        if not result:
            return {"status": "no_result"}
        status = result.get("status")
        if not status:
            status = "empty" if result.get("empty") else "ok"
        attrs: dict = {
            "status": status,
            "period_start": result.get("period_start"),
            "period_end": result.get("period_end"),
            "source": result.get("source"),
            "cached": bool(result.get("cached", False)),
            "rcem": result.get("rcem"),
            "kwh": result.get("kwh"),
            "error": result.get("error"),
        }
        for key in _BREAKDOWN_KEYS:
            if key in result:
                attrs[key] = result.get(key)
        # Always expose the source explicitly (even when the key is absent) so
        # the attribute is never silently ``None`` for a rendered result.
        attrs.setdefault("fee_source", result.get("fee_source"))
        # v1.9.2-beta.3: product preset provenance (auto/explicit/inferred).
        attrs.setdefault("tariff_product", result.get("tariff_product"))
        attrs.setdefault("product_source", result.get("product_source"))
        # v1.9.2-beta.2: opening-balance provenance (override/canonical/recorder/api).
        attrs.setdefault("opening_source", result.get("opening_source"))
        attrs.setdefault("deposit_open_source", result.get("deposit_open_source"))
        missing = _missing_breakdown_keys(result)
        if missing:
            _LOGGER.warning(
                "Energa: sensor wyniku okresu bez kluczy %s "
                "(wynik usługi może być niepełny)",
                missing,
            )
            attrs["missing_breakdown_keys"] = missing
        return attrs

    @property
    def available(self) -> bool:
        """A result exists (even an empty one reports status in attributes)."""
        return self._result() is not None
