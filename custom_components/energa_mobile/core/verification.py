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
  ``*_stats`` energy sensors or the Energa API (Faza 2, service layer).
- RCEm is supplied by the caller (coordinator cache / Options / last known);
  per-month historical PSE tables are Faza 4.
- Faza 3 opening balances are supported through ``bank_open_1/2``
  (net-metering warehouse, reconstructed by the service from prior monthly
  flows with :func:`opening_bank_from_monthly_flows`) and
  ``deposit_open_pln`` (net-billing). When they are unknown the result is
  still returned with ``coverage_unknown=True`` and a warning instead of
  pretending invoice parity.
- Fee tables are the current ones; historical tariff tables are not yet
  versioned (Faza 4). Pass ``fees`` explicitly to pin a table.
"""

from __future__ import annotations

from datetime import date, datetime

from ..settlement import fifo_kwh_bank
from ..tariff import bill_saldos, compute_bill, mtd_invoice_bases

# Data sources returned by the verify_period service (Faza 2).
SOURCE_ENERGA_API = "energa_api"
SOURCE_RECORDER = "recorder_hourly"

# kWh bases surfaced in the response (all JSON-serialisable floats).
# ``gross_1/2`` are kept for backward compatibility; ``gross_import_1/2``
# is the invoice-aligned name. ``cover_1/2`` is the warehouse coverage.
PERIOD_KWH_KEYS = (
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
)

# Default opust coefficient when the caller does not provide one.
DEFAULT_COEFFICIENT = 0.8


def has_two_zones(hourly_by_zone: dict) -> bool:
    """True when the hourly series carries a second metering zone."""
    hourly = hourly_by_zone or {}
    return bool(hourly.get("import_2") or hourly.get("export_2"))


def parse_period_date(value) -> date | None:
    """Parse a date/datetime/ISO string into a ``date`` (or ``None``).

    Accepts ``date``/``datetime`` objects and strings starting with
    ``YYYY-MM-DD`` (an ISO datetime suffix is ignored). Garbage returns
    ``None`` instead of raising so UI/service code stays defensive.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
        except (ValueError, TypeError):
            return None
    try:
        return date.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def format_period_date(value) -> str | None:
    """Normalise any accepted date value to ``YYYY-MM-DD`` (or ``None``)."""
    parsed = parse_period_date(value)
    return parsed.isoformat() if parsed is not None else None


def period_is_historical(start, end, *, today: date | None = None) -> bool:
    """True when the period ended before ``today`` (safe to serve from API).

    ``end`` is inclusive here; a period ending today is *not* historical
    because the current day may still be incomplete.
    """
    start_date = parse_period_date(start)
    end_date = parse_period_date(end)
    if start_date is None or end_date is None:
        return False
    ref = today if today is not None else date.today()
    return end_date < ref


def choose_period_source(
    *,
    api_available: bool,
    historical: bool,
    recorder_empty: bool,
) -> str:
    """Pick the preferred data source for one verification call.

    The Energa API is preferred when a concrete meter + entry are known and
    the period is historical, or whenever the recorder has no data at all
    (e.g. HA was installed after the period). Otherwise the recorder series
    is used. Callers still fall back if the API returns nothing.
    """
    if api_available and (historical or recorder_empty):
        return SOURCE_ENERGA_API
    return SOURCE_RECORDER


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
    bank_open_1: float | None = None,
    bank_open_2: float | None = None,
    prosumer_coefficient: float | None = None,
    tariff: str | None = None,
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
        deposit_open_pln: explicit **opening** deposit balance (net-billing).
            ``None`` means it could not be reconstructed → ``deposit_open`` is
            reported as ``null`` and ``coverage_unknown`` is set.
        cover_day/cover_night: explicit warehouse coverage (net-metering
            only). When ``bank_open_1/2`` are given the coverage is computed
            as ``min(bank_open, salda dodatnie)`` and the explicit values act
            as a lower bound.
        bank_open_1/bank_open_2: warehouse level at the start of the period
            (net-metering). ``None`` = unknown (no history); if both are
            ``None`` and no explicit ``cover_*`` is given the result carries
            ``coverage_unknown=True`` and a warning, but is still returned.
        prosumer_coefficient: opust used to refresh the warehouse (display).
        tariff: tariff string recorded in the result.

    Returns:
        Invoice dict (net PLN lines) with the hourly ``kwh`` bases plus
        ``rcem``, ``months``, ``old_system``, the full invoice line items
        (sale / excise / distribution / deposit), ``coverage_unknown`` and
        ``warnings``. ``compute_bill`` output is preserved; every value is
        JSON-serialisable.
    """
    hourly = hourly_by_zone or {}
    saldos = bill_saldos(hourly)
    has_zones = has_two_zones(hourly)

    bases = mtd_invoice_bases(saldos, has_zones=has_zones, old_system=old_system)

    import_day = float(bases["import_day"])
    import_night = float(bases["import_night"]) if has_zones else 0.0

    try:
        coefficient = float(
            prosumer_coefficient
            if prosumer_coefficient is not None
            else DEFAULT_COEFFICIENT
        )
    except (ValueError, TypeError):
        coefficient = DEFAULT_COEFFICIENT

    warnings: list[str] = []
    coverage_unknown = False

    # --- Opening warehouse (net-metering) -------------------------------
    bank_known = bank_open_1 is not None or bank_open_2 is not None
    bank_1 = max(0.0, float(bank_open_1)) if bank_open_1 is not None else 0.0
    bank_2 = max(0.0, float(bank_open_2)) if bank_open_2 is not None else 0.0
    explicit_cover = max(0.0, float(cover_day or 0.0)) > 0.0 or max(
        0.0, float(cover_night or 0.0)
    ) > 0.0

    if old_system:
        cover_day = max(0.0, float(cover_day or 0.0))
        cover_night = max(0.0, float(cover_night or 0.0))
        if bank_known:
            # Warehouse covers up to the hourly-netted positive balance.
            cover_day = max(cover_day, min(bank_1, import_day))
            cover_night = max(cover_night, min(bank_2, import_night))
        elif explicit_cover:
            coverage_unknown = False
        else:
            coverage_unknown = True
            warnings.append(
                "Brak historii magazynu (net-metering): nie odtworzono stanu "
                "banku na początek okresu — pokrycie z magazynu przyjęto 0 kWh, "
                "a kwota do zapłaty może być zawyżona."
            )
        deposit_generated = 0.0
        deposit_open_out: float | None = 0.0
        deposit_total = 0.0
    else:
        cover_day = max(0.0, float(cover_day or 0.0))
        cover_night = max(0.0, float(cover_night or 0.0))

    # --- Opening deposit (net-billing) ----------------------------------
    if not old_system:
        if deposit_open_pln is None:
            deposit_open_calc = 0.0
            deposit_open_out = None
            coverage_unknown = True
            warnings.append(
                "Brak salda początkowego depozytu (net-billing): nie odtworzono "
                "wolnych środków z poprzednich okresów — kwota do zapłaty może "
                "się różnić od faktury."
            )
        else:
            deposit_open_calc = max(0.0, float(deposit_open_pln))
            deposit_open_out = deposit_open_calc
        deposit_generated = _round2(
            float(bases["export"]) * float(rcem or 0.0) * 1.23
        )
        deposit_total = _round2(deposit_open_calc + deposit_generated)

    res = compute_bill(
        import_day=bases["import_day"],
        import_night=bases["import_night"],
        export_kwh=bases["export"],
        rcem=rcem,
        fees=fees,
        months=months,
        cover_day=cover_day,
        cover_night=cover_night,
        deposit_pln=deposit_total,
        deposit_generated=deposit_generated,
        excise_day=bases["excise_day"],
        excise_night=bases["excise_night"],
        add_excise=bases["add_excise"],
    )

    kwh = {key: float(saldos.get(key, 0.0)) for key in PERIOD_KWH_KEYS}
    # Invoice-aligned aliases and warehouse coverage/bank state.
    kwh["gross_import_1"] = float(saldos.get("gross_1", 0.0))
    kwh["gross_import_2"] = float(saldos.get("gross_2", 0.0))
    kwh["cover_1"] = round(cover_day, 3)
    kwh["cover_2"] = round(cover_night, 3)
    if old_system:
        gross_export_1 = _sum_series(hourly.get("export_1"))
        gross_export_2 = _sum_series(hourly.get("export_2"))
        kwh["bank_open_1"] = bank_1 if bank_known else None
        kwh["bank_open_2"] = bank_2 if bank_known else None
        kwh["bank_close_1"] = (
            round(max(0.0, bank_1 + gross_export_1 * coefficient - cover_day), 3)
            if bank_known
            else None
        )
        kwh["bank_close_2"] = (
            round(max(0.0, bank_2 + gross_export_2 * coefficient - cover_night), 3)
            if bank_known
            else None
        )

    out = dict(res)
    out["kwh"] = kwh
    out["rcem"] = float(rcem or 0.0)
    out["months"] = max(1, int(months))
    out["old_system"] = bool(old_system)
    out["system"] = "net_metering" if old_system else "net_billing"
    out["tariff"] = tariff
    out["prosumer_coefficient"] = coefficient
    out["kwh_source"] = bases["source"]
    out["coverage_unknown"] = bool(coverage_unknown)
    out["warnings"] = list(warnings)
    # Explicit opening deposit (None = unknown) overrides compute_bill's
    # derived value (which assumes 0 when it cannot know the opening).
    out["deposit_open"] = deposit_open_out
    return out


def _round2(value: float) -> float:
    """Round to grosze with the invoice ROUND_HALF_UP convention."""
    from decimal import ROUND_HALF_UP, Decimal

    return float(
        Decimal(str(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def _sum_series(series) -> float:
    """Sum a ``{ts: kWh}`` series defensively (non-numeric -> 0)."""
    total = 0.0
    for value in (series or {}).values():
        try:
            total += float(value)
        except (ValueError, TypeError):
            continue
    return total


def opening_bank_from_monthly_flows(
    monthly_flows: dict,
    coefficient: float,
    *,
    has_zones: bool = False,
    period_start=None,
    initial_1: float = 0.0,
    initial_2: float = 0.0,
) -> tuple[float | None, float | None, dict]:
    """Warehouse level at the start of a period from prior monthly flows.

    Replays the Energa net-metering FIFO 12-month rule over the monthly
    import/export flows that precede ``period_start``. The reference month is
    the period-start month, so a mid-month start includes the flows already
    elapsed in that month (the month's expiry index is unchanged).

    Args:
        monthly_flows: ``{(year, month): {"import_1"/"export_1"/...}}``
            (single-zone flows may use ``import``/``export``).
        coefficient: opust (0.8 / 0.7).
        has_zones: use the per-zone suffixes when True.
        period_start: date/datetime of the period start.
        initial_1/initial_2: explicit starting balances added to the FIFO
            result (e.g. from a known invoice cut-off).

    Returns:
        ``(bank_1, bank_2, detail)``. Both banks are ``None`` when there is
        no usable history at all (so the caller reports ``coverage_unknown``).
    """
    ref = period_start
    if isinstance(ref, datetime):
        ref = ref.date()
    if not isinstance(ref, date):
        ref = date.today()

    def _zone_flows(imp_key: str, exp_key: str) -> list:
        flows: list = []
        # No sort here: fifo_kwh_bank sorts by (year, month) internally;
        # unsorted/mixed keys must not raise.
        for key in list(monthly_flows or {}):
            try:
                year, month = int(key[0]), int(key[1])
            except (TypeError, ValueError, IndexError):
                continue
            row = monthly_flows.get(key) or {}
            try:
                imp = max(0.0, float(row.get(imp_key, 0.0) or 0.0))
            except (ValueError, TypeError):
                imp = 0.0
            try:
                exp = max(0.0, float(row.get(exp_key, 0.0) or 0.0))
            except (ValueError, TypeError):
                exp = 0.0
            flows.append((year, month, imp, exp))
        return flows

    if has_zones:
        flows_1 = _zone_flows("import_1", "export_1")
        flows_2 = _zone_flows("import_2", "export_2")
    else:
        flows_1 = _zone_flows("import", "export")
        flows_2 = []

    has_data = any(
        imp > 0 or exp > 0 for _y, _m, imp, exp in (flows_1 + flows_2)
    )
    if not has_data:
        return (None, None, {"months_used": 0, "has_data": False})

    try:
        coeff = float(coefficient)
    except (ValueError, TypeError):
        coeff = DEFAULT_COEFFICIENT

    bank_1, detail_1 = fifo_kwh_bank(flows_1, coeff, today=ref)
    bank_2, detail_2 = fifo_kwh_bank(flows_2, coeff, today=ref)
    bank_1 = round(max(0.0, bank_1) + max(0.0, float(initial_1 or 0.0)), 3)
    bank_2 = round(max(0.0, bank_2) + max(0.0, float(initial_2 or 0.0)), 3)
    detail = {
        "bank_kwh_l1": bank_1,
        "bank_kwh_l2": bank_2,
        "months_used": max(
            detail_1.get("months_used", 0), detail_2.get("months_used", 0)
        ),
        "expired_kwh": round(
            detail_1.get("expired_kwh", 0.0) + detail_2.get("expired_kwh", 0.0), 2
        ),
        "uncovered_kwh": round(
            detail_1.get("uncovered_kwh", 0.0) + detail_2.get("uncovered_kwh", 0.0), 2
        ),
        "has_data": True,
    }
    return (bank_1, bank_2, detail)


def deposit_ledger_close(entries, *, initial: float = 0.0) -> float:
    """Replay a net-billing deposit ledger oldest-first.

    Each entry is ``(generated_pln, use_cap_pln)`` for one settled period:
    the balance grows by ``generated_pln`` and is then drawn down by at most
    ``use_cap_pln`` (the period's eligible energy + excise gross). The
    remainder rolls over to the next period.

    Pure and defensive: invalid entries are skipped, the result never goes
    negative. Used to reconstruct the opening deposit of a period when the
    caller can supply per-period generated amounts and caps.
    """
    try:
        balance = max(0.0, float(initial))
    except (ValueError, TypeError):
        balance = 0.0
    for entry in entries or []:
        try:
            generated = max(0.0, float(entry[0]))
            cap = max(0.0, float(entry[1]))
        except (ValueError, TypeError, IndexError, KeyError):
            continue
        balance += generated
        balance -= min(balance, cap)
    return round(balance, 2)


__all__ = [
    "PERIOD_KWH_KEYS",
    "SOURCE_ENERGA_API",
    "SOURCE_RECORDER",
    "build_period_invoice",
    "choose_period_source",
    "deposit_ledger_close",
    "format_period_date",
    "has_two_zones",
    "opening_bank_from_monthly_flows",
    "parse_period_date",
    "period_is_historical",
]
