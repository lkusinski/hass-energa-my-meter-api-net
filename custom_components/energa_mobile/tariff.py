"""Tariff fee tables and full-bill math (v0.2.14, G11 table v0.3.0).

Reconstructs a Polish Energa invoice (Grupa Orlen) from meter flows:
section 1 = energy sale (day/night + trade fee), section 2 =
distribution (subscription, fixed/variable grid, quality, OZE,
cogeneration, capacity fee), VAT 23%, minus prosumer settlement
(deposit for net-billing, kWh coverage for net-metering).

Fee tables (net PLN):
- G12W_DEFAULT_FEES: two-zone tariff, defaults from G12W invoices
  (07.2026 net-billing, 05-06.2026 net-metering). Per-zone variable
  distribution prices were derived from kWh x price = line value and
  are marked for re-check against the OSD tariff PTR.
- G11_DEFAULT_FEES: single-zone tariff, exact values from the G11
  consumer invoice 1200000017/FES/XXXXX (04.02-05.04.2026, 2159 kWh):
  sale 1352.37 + distribution 919.37 = netto 2271.74 -> brutto 2794.24.

NOTE on excise: the invoice states "naliczono akcyze 10,80 zl" as an
informational footnote — the 5 PLN/MWh is already inside the energy
price, NOT added on top (2159 kWh: 1320.01 + 32.36 + 919.37 = 2271.74
to the grosz). compute_bill therefore reports excise as an INFO line
only (key "excise"), excluded from sale_total/netto.

Pure functions only (no Home Assistant imports) so they stay unit-tested.
"""

from __future__ import annotations

VAT_RATE = 0.23

# URE 2026 capacity-fee brackets for households (ryczałt, netto PLN/month).
# Source: Informacja Prezesa URE Nr 58/2025 (30.10.2025) — by ANNUAL
# consumption, not contracted power: <500 kWh -> 4.29; 500-1200 -> 10.31;
# 1200-2800 -> 17.18; >2800 -> 24.05. Our old default 24.05 was just the
# top bracket (both reference houses consume more) — for a small flat it
# would overcharge by ~20 PLN/month, hence auto-bracketing below.
CAPACITY_2026_BRACKETS = (
    (500.0, 4.29),
    (1200.0, 10.31),
    (2800.0, 17.18),
    (float("inf"), 24.05),
)


def capacity_for_annual_use(annual_kwh: float | None) -> float:
    """Monthly capacity fee (netto) from estimated annual import.

    Falls back to the top bracket when the estimate is missing/invalid —
    same value as the old hard default, never worse.
    """
    try:
        annual = float(annual_kwh)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return G12W_DEFAULT_FEES["capacity"]
    if annual <= 0:
        return G12W_DEFAULT_FEES["capacity"]
    for limit, fee in CAPACITY_2026_BRACKETS:
        if annual < limit:
            return fee
    return G12W_DEFAULT_FEES["capacity"]

# Option keys (mirrored in const.py as CONF_TARIFF_*). Kept as plain
# strings here so this module stays importable without Home Assistant.
_OPTION_KEY_MAP = {
    "energy_day": "tariff_energy_day",
    "energy_night": "tariff_energy_night",
    "excise_mwh": "tariff_excise_mwh",
    "trade_fee": "tariff_trade_fee",
    "abonament": "tariff_abonament",
    "grid_fixed": "tariff_grid_fixed",
    "grid_var_day": "tariff_grid_var_day",
    "grid_var_night": "tariff_grid_var_night",
    "quality": "tariff_quality",
    "oze": "tariff_oze",
    "cogen": "tariff_cogen",
    "capacity": "tariff_capacity",
}

# G12W defaults, PLN net (see module docstring for provenance).
# energy_* : energy sale price per kWh, day (L1) / night (L2).
# excise_mwh : excise duty per MWh of IMPORT — informational only
#   (already inside the energy price, see module docstring).
# trade_fee : monthly trade fee (handlowa); 0.0 = not present on the
#   reference net-billing invoice (override in a follow-up Options UI).
G12W_DEFAULT_FEES = {
    "energy_day": 0.6107,
    "energy_night": 0.3990,
    "excise_mwh": 5.00,
    "trade_fee": 0.0,
    "abonament": 0.74,
    "grid_fixed": 20.17,
    "grid_var_day": 0.4017,
    "grid_var_night": 0.0851,
    "quality": 0.0332,
    "oze": 0.0073,
    "cogen": 0.0030,
    "capacity": 24.05,
}

# G11 defaults, PLN net — exact values from consumer invoice
# 1200000017/FES/XXXXX (G11, meter 73000003, 04.02-05.04.2026, 2159 kWh,
# moc umowna 18 kW). Single zone: night/variable-night keys unused.
# Annual use on that invoice: 7312 kWh -> top URE capacity bracket.
G11_DEFAULT_FEES = {
    "energy_day": 0.6114,
    "energy_night": 0.0,
    "excise_mwh": 5.00,
    "trade_fee": 16.18,
    "abonament": 0.70,
    "grid_fixed": 11.77,
    "grid_var_day": 0.3485,
    "grid_var_night": 0.0,
    "quality": 0.0332,
    "oze": 0.0073,
    "cogen": 0.0030,
    "capacity": 24.05,
}

# Fee table per tariff family. Unknown tariffs fall back to G12W.
FEE_TABLES = {
    "G11": G11_DEFAULT_FEES,
    "G12W": G12W_DEFAULT_FEES,
}


def tariff_family(tariff: str | None) -> str:
    """Fee-table key for a meter tariff string ("G11" / "G12W", ...).

    G11 (single zone) has its own invoice-verified table; every other
    two-zone tariff (G12, G12W, G12AS, G12R, ...) uses the G12W table.
    Fully defensive: unknown/empty values fall back to G12W.
    """
    try:
        name = str(tariff or "").strip().upper()
    except (ValueError, TypeError):
        return "G12W"
    if name.startswith("G11"):
        return "G11"
    return "G12W"


def compute_bill(
    import_day: float,
    import_night: float,
    export_kwh: float,
    rcem: float,
    fees: dict | None = None,
    months: int = 1,
    cover_day: float = 0.0,
    cover_night: float = 0.0,
    deposit_pln: float | None = None,
) -> dict:
    """Full monthly bill from meter flows.

    Args:
        import_day/night: kWh taken from the grid per zone (period).
        export_kwh: kWh fed into the grid (period, hourly-netted sum).
        rcem: invoiced monthly market price (volume-weighted, PSE table).
        fees: fee table (defaults to G12W_DEFAULT_FEES).
        months: how many monthly fixed fees to include.
        cover_day/night: kWh covered by the virtual warehouse (old
            net-metering only; energy charge drops, excise and
            distribution stay on the FULL import).
        deposit_pln: explicit deposit to subtract (new net-billing).
            When None, computed as export_kwh*rcem*1.23.

    Returns dict with every invoice line (net PLN) plus totals and
    ``do_zaplaty`` (gross payable). ``excise`` is informational only
    (already inside the energy price — proven by the G11 invoice).
    """
    f = dict(G12W_DEFAULT_FEES)
    if fees:
        f.update(fees)
    import_day = max(0.0, float(import_day))
    import_night = max(0.0, float(import_night))
    export_kwh = max(0.0, float(export_kwh))
    months = max(1, int(months))

    # Old net-metering coverage: covered kWh are exempt from the energy
    # charge AND from variable distribution + quality fee, but OZE/cogen
    # and all fixed fees stay on the FULL import (verified on the
    # G12W-stare 05-06.2026 invoice: variable rows 0.00, OZE + kogen
    # charged, energy rows 0 kWh).
    pay_day = max(0.0, import_day - max(0.0, float(cover_day)))
    pay_night = max(0.0, import_night - max(0.0, float(cover_night)))

    sale_energy = pay_day * f["energy_day"] + pay_night * f["energy_night"]
    # Excise is NOT added: it is already inside the energy price
    # (G11 invoice 1200000017/FES/XXXXX matches to the grosz without
    # it; the "naliczono akcyze" line is informational).
    excise_info = (import_day + import_night) * f["excise_mwh"] / 1000.0
    sale_total = sale_energy + f["trade_fee"] * months

    distr_var_day = pay_day * f["grid_var_day"]
    distr_var_night = pay_night * f["grid_var_night"]
    pay_total = pay_day + pay_night
    total_kwh = import_day + import_night
    distr_quality = pay_total * f["quality"]
    distr_oze = total_kwh * f["oze"]
    distr_cogen = total_kwh * f["cogen"]
    distr_total = (
        distr_var_day
        + distr_var_night
        + distr_quality
        + distr_oze
        + distr_cogen
        + (f["abonament"] + f["grid_fixed"] + f["capacity"]) * months
    )

    netto = sale_total + distr_total
    vat = netto * VAT_RATE
    brutto = netto + vat

    if deposit_pln is None:
        deposit_pln = export_kwh * float(rcem) * 1.23
    applied = min(max(0.0, float(deposit_pln)), brutto)
    do_zaplaty = round(brutto - applied, 2)

    def _r(x: float) -> float:
        return round(x, 2)

    return {
        "sale_energy_day": _r(pay_day * f["energy_day"]),
        "sale_energy_night": _r(pay_night * f["energy_night"]),
        "excise": _r(excise_info),
        "excise_note": "informacyjnie — akcyza jest już w cenie energii (faktura G11)",
        "trade_fee": _r(f["trade_fee"] * months),
        "sale_total": _r(sale_total),
        "distr_var_day": _r(distr_var_day),
        "distr_var_night": _r(distr_var_night),
        "distr_quality": _r(distr_quality),
        "distr_oze": _r(distr_oze),
        "distr_cogen": _r(distr_cogen),
        "distr_fixed": _r((f["abonament"] + f["grid_fixed"] + f["capacity"]) * months),
        "distr_total": _r(distr_total),
        "netto": _r(netto),
        "vat": _r(vat),
        "brutto": _r(brutto),
        "deposit": _r(max(0.0, float(deposit_pln))),
        "deposit_applied": _r(applied),
        "do_zaplaty": do_zaplaty,
    }


def fees_from_options(options: dict | None, tariff: str | None = None) -> dict:
    """Build a fee table from integration Options (v0.2.14, tariffs v0.3.0).

    Reads ``tariff_*`` overrides, falls back to the per-tariff table
    (G11 vs G12W via :func:`tariff_family`). Fully defensive:
    unknown/missing/invalid values keep defaults, so a half-filled
    Options form can never break the bill sensor.
    """
    base = FEE_TABLES.get(tariff_family(tariff), G12W_DEFAULT_FEES)
    f = dict(base)
    if not options:
        return f
    # Migration (v0.3.0): the Options form used to bake G12W defaults
    # into every account on first open. A G11 meter whose overrides are
    # all identical to the G12W table was never meaningfully customized
    # — use the invoice-verified G11 table instead of stale G12W numbers.
    if tariff_family(tariff) == "G11" and _options_match_table(
        options, G12W_DEFAULT_FEES
    ):
        return dict(G11_DEFAULT_FEES)
    for fee, opt_key in _OPTION_KEY_MAP.items():
        try:
            if opt_key in options and options[opt_key] is not None:
                f[fee] = float(options[opt_key])
        except (ValueError, TypeError):
            continue
    return f


def _options_match_table(options: dict, table: dict) -> bool:
    """True when no tariff_* override deviates from the given table.

    Missing keys and invalid values count as match (they fall back to
    defaults anyway). Used for the v0.3.0 G11 migration above.
    """
    for fee, opt_key in _OPTION_KEY_MAP.items():
        if opt_key in options and options[opt_key] is not None:
            try:
                if float(options[opt_key]) != float(table[fee]):
                    return False
            except (ValueError, TypeError):
                continue
    return True


def split_cover(total_cover: float, import_day: float, import_night: float) -> tuple[float, float]:
    """Split warehouse coverage across day/night proportionally to import.

    Old net-metering only: covered kWh are exempt from the energy charge
    (and variable distribution + quality fee, see compute_bill). Coverage
    can never exceed the actual import.
    """
    try:
        total_cover = max(0.0, float(total_cover))
    except (ValueError, TypeError):
        return (0.0, 0.0)
    try:
        day = max(0.0, float(import_day))
        night = max(0.0, float(import_night))
    except (ValueError, TypeError):
        return (0.0, 0.0)
    total = day + night
    if total <= 0 or total_cover <= 0:
        return (0.0, 0.0)
    cover = min(total_cover, total)
    cover_day = round(cover * day / total, 2)
    return (cover_day, round(cover - cover_day, 2))
