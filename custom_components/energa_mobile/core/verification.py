"""Pure invoice reconstruction for an arbitrary period (Faza 1).

Rebuilds a seller invoice (Grupa Orlen/Energa) for a user-supplied period
from hourly recorder statistics. No Home Assistant imports here, so the
whole salda -> invoice pipeline stays unit-testable against real invoices.

Pipeline (matches the live bill sensors):

    hourly_by_zone  ->  tariff.bill_saldos
                    ->  tariff.mtd_invoice_bases(old_system=...)
                    ->  tariff.compute_bill

Faza 1 scope / deliberate limitations:
- Data source is the HA recorder hourly statistics of the integration's
  ``*_stats`` energy sensors; there is no Energa API fallback yet (Faza 2).
- RCEm is supplied by the caller (coordinator cache / Options / last known);
  per-month historical PSE tables are Faza 3.
- No opening deposit balance (net-billing) and no opening warehouse level
  (net-metering), so ``do_zaplaty`` for a partial period is approximate
  (Faza 3). Old net-metering coverage can be passed via ``cover_day`` /
  ``cover_night`` once that state exists.
- Fee tables are the current ones; historical tariff tables are not yet
  versioned (Faza 4). Pass ``fees`` explicitly to pin a table.
"""

from __future__ import annotations

from ..tariff import bill_saldos, compute_bill, mtd_invoice_bases

# kWh bases surfaced in the response (all JSON-serialisable floats).
PERIOD_KWH_KEYS = (
    "saldo_plus_1",
    "saldo_plus_2",
    "saldo_minus_1",
    "saldo_minus_2",
    "gross_1",
    "gross_2",
    "overlap_1",
    "overlap_2",
)


def has_two_zones(hourly_by_zone: dict) -> bool:
    """True when the hourly series carries a second metering zone."""
    hourly = hourly_by_zone or {}
    return bool(hourly.get("import_2") or hourly.get("export_2"))


def build_period_invoice(
    hourly_by_zone: dict,
    *,
    fees: dict,
    rcem: float,
    months: int = 1,
    old_system: bool = False,
    deposit_open_pln: float | None = None,
    cover_day: float = 0.0,
    cover_night: float = 0.0,
) -> dict:
    """Build the full invoice breakdown for one meter and one period.

    Args:
        hourly_by_zone: ``{"import_1": {epoch_hour: kWh},
            "export_1": {...}, "import_2": {...}, "export_2": {...}}``
            (single-zone meters only provide the ``_1`` keys).
        fees: fee table (see :data:`tariff.G12W_DEFAULT_FEES`).
        rcem: market price used to value exported energy (PLN/kWh).
        months: number of monthly fixed fees to charge.
        old_system: ``True`` for net-metering (coefficient >= 0.7); excise is
            then charged on the gross import and no PLN deposit is applied.
        deposit_open_pln: explicit opening deposit balance (net-billing).
            ``None`` values the current-period export at ``rcem``.
        cover_day/cover_night: warehouse coverage (net-metering only).

    Returns:
        Invoice dict (net PLN lines) with the hourly ``kwh`` bases plus
        ``rcem``, ``months`` and ``old_system``. ``compute_bill`` output is
        preserved; every value is JSON-serialisable.
    """
    hourly = hourly_by_zone or {}
    saldos = bill_saldos(hourly)
    has_zones = has_two_zones(hourly)

    bases = mtd_invoice_bases(saldos, has_zones=has_zones, old_system=old_system)

    # Old net-metering never settles through a PLN deposit: the warehouse
    # covers the salda plus (cover_day/cover_night) instead.
    if old_system and deposit_open_pln is None:
        deposit_pln: float | None = 0.0
    else:
        deposit_pln = deposit_open_pln

    res = compute_bill(
        import_day=bases["import_day"],
        import_night=bases["import_night"],
        export_kwh=bases["export"],
        rcem=rcem,
        fees=fees,
        months=months,
        cover_day=cover_day,
        cover_night=cover_night,
        deposit_pln=deposit_pln,
        excise_day=bases["excise_day"],
        excise_night=bases["excise_night"],
        add_excise=bases["add_excise"],
    )

    out = dict(res)
    out["kwh"] = {key: float(saldos.get(key, 0.0)) for key in PERIOD_KWH_KEYS}
    out["rcem"] = float(rcem or 0.0)
    out["months"] = max(1, int(months))
    out["old_system"] = bool(old_system)
    out["kwh_source"] = bases["source"]
    return out


__all__ = ["PERIOD_KWH_KEYS", "build_period_invoice", "has_two_zones"]
