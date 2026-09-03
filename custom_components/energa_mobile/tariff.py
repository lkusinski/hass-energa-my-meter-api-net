"""Tariff fee tables and full-bill math (v0.2.14).

Reconstructs a Polish Energa invoice (Grupa Orlen) from meter flows:
section 1 = energy sale (day/night + excise + trade fee), section 2 =
distribution (subscription, fixed/variable grid, quality, OZE,
cogeneration, capacity fee), VAT 23%, minus prosumer settlement
(deposit for net-billing, kWh coverage for net-metering).

Defaults come from G12W invoices (07.2026 net-billing, 05-06.2026
net-metering). Per-zone variable distribution prices were derived from
kWh x price = line value and are marked for re-check against the OSD
tariff PTR (see docs/WIZJA.md open questions).

Pure functions only (no Home Assistant imports) so they stay unit-tested.
"""

from __future__ import annotations

VAT_RATE = 0.23

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
# excise_mwh : excise duty per MWh of IMPORT (paid even when energy
#   itself is covered by the virtual warehouse).
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
    ``do_zaplaty`` (gross payable).
    """
    f = dict(G12W_DEFAULT_FEES)
    if fees:
        f.update(fees)
    import_day = max(0.0, float(import_day))
    import_night = max(0.0, float(import_night))
    export_kwh = max(0.0, float(export_kwh))
    months = max(1, int(months))

    # Old net-metering coverage: covered kWh are exempt from the energy
    # charge AND from variable distribution + quality fee, but excise,
    # OZE/cogen and all fixed fees stay on the FULL import (verified on
    # the G12W-stare 05-06.2026 invoice: variable rows 0.00, excise +
    # OZE + kogen charged, energy rows 0 kWh).
    pay_day = max(0.0, import_day - max(0.0, float(cover_day)))
    pay_night = max(0.0, import_night - max(0.0, float(cover_night)))

    sale_energy = pay_day * f["energy_day"] + pay_night * f["energy_night"]
    excise = (import_day + import_night) * f["excise_mwh"] / 1000.0
    sale_total = sale_energy + excise + f["trade_fee"] * months

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
        "excise": _r(excise),
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


def fees_from_options(options: dict | None) -> dict:
    """Build a fee table from integration Options (v0.2.14).

    Reads ``tariff_*`` overrides, falls back to G12W_DEFAULT_FEES.
    Fully defensive: unknown/missing/invalid values keep defaults, so a
    half-filled Options form can never break the bill sensor.
    """
    f = dict(G12W_DEFAULT_FEES)
    if not options:
        return f
    for fee, opt_key in _OPTION_KEY_MAP.items():
        try:
            if opt_key in options and options[opt_key] is not None:
                f[fee] = float(options[opt_key])
        except (ValueError, TypeError):
            continue
    return f


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
