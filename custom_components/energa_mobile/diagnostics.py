"""Diagnostics support for Energa My Meter integration.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdział 10 & 14.
Provides sanitized diagnostics with strict PII masking (redacting passwords, tokens, PESEL, addresses).
"""

from __future__ import annotations

import os
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .ha.alerts import ProsumerAlertManager

TO_REDACT = {
    "password",
    "token",
    "access_token",
    "refresh_token",
    "pesel",
    "street",
    "address",
    "phone",
    "email",
    # v1.9.2 (P1.1): the Energa login is the account e-mail; it used to
    # pass through diagnostics unmasked despite the "strict PII" docstring.
    "username",
    "user",
    "login",
    "client_secret",
}

# Keys whose value is shown partially (a***@d***) instead of fully hidden,
# so a support engineer can still tell which account a dump belongs to
# without exposing the address.
_PARTIAL_MASK_KEYS = {"username", "user", "login", "email"}


def _mask_email_like(value: Any) -> str:
    """Mask an e-mail/login to ``a***@d***`` (or ``a***`` when no domain)."""
    text = str(value or "")
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"{local[:1] or '*'}***@{domain[:1] or '*'}***"
    return f"{text[:1] or '*'}***"


def _redacted_value(key_lower: str, value: Any) -> str:
    """Return the display value for a sensitive key."""
    if any(target in key_lower for target in _PARTIAL_MASK_KEYS):
        return _mask_email_like(value)
    return "**REDACTED**"


def redact_sensitive_data(data: Any) -> Any:
    """Recursively redact sensitive keys and PII from diagnostic structures."""
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            k_lower = str(key).lower()
            if any(target in k_lower for target in TO_REDACT):
                redacted[key] = _redacted_value(k_lower, value)
            elif isinstance(value, (dict, list)):
                redacted[key] = redact_sensitive_data(value)
            else:
                redacted[key] = value
        return redacted
    elif isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return sanitized diagnostics for a config entry."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    storage = entry_data.get("storage")

    diag: dict[str, Any] = {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "domain": entry.domain,
            "version": getattr(entry, "version", 1),
            "data": redact_sensitive_data(dict(entry.data)),
            "options": redact_sensitive_data(dict(entry.options)),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success if coordinator else None,
            "has_data": bool(coordinator and coordinator.data),
            "meters_count": len(coordinator.data) if coordinator and coordinator.data else 0,
        },
        "storage": None,
        "alerts": [],
    }

    if storage:
        def _get_storage_stats() -> dict[str, Any]:
            stats: dict[str, Any] = {
                "schema_version": storage.get_schema_version(),
                "readings_count": storage.get_readings_count(),
                "db_size_bytes": os.path.getsize(storage.db_path) if os.path.exists(storage.db_path) else 0,
            }
            try:
                cur = storage._connection.cursor()
                # Reading date bounds
                bounds = cur.execute(
                    "SELECT MIN(interval_start_utc), MAX(interval_start_utc) FROM interval_reading"
                ).fetchone()
                stats["earliest_reading_utc"] = bounds[0] if bounds else None
                stats["latest_reading_utc"] = bounds[1] if bounds else None

                # Table counts
                stats["market_prices_count"] = cur.execute("SELECT count(*) FROM market_price").fetchone()[0]
                stats["settlement_lots_count"] = cur.execute("SELECT count(*) FROM settlement_lot").fetchone()[0]
                stats["reconciliations_count"] = cur.execute("SELECT count(*) FROM invoice_reconciliation").fetchone()[0]
                stats["checkpoints_count"] = cur.execute("SELECT count(*) FROM import_checkpoint").fetchone()[0]
            except Exception as err:
                stats["error"] = str(err)

            return stats

        diag["storage"] = await hass.async_add_executor_job(_get_storage_stats)

        # Collect active alerts
        ppe_id = entry.data.get("ppe_id")
        if ppe_id:
            alert_mgr = ProsumerAlertManager(storage)
            raw_alerts = alert_mgr.get_all_alerts(ppe_id)
            diag["alerts"] = [
                {
                    "alert_type": a.alert_type,
                    "severity": a.severity,
                    "title": a.title,
                    "message": a.message,
                    "details": a.details,
                }
                for a in raw_alerts
            ]

    return diag
