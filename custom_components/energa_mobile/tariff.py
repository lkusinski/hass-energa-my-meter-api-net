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
- G11_DEFAULT_FEES: single-zone tariff, exact values from a G11
  consumer invoice (02-04.2026, 2159 kWh, no PV):
  sale 1352.37 + distribution 919.37 = netto 2271.74 -> brutto 2794.24.

NOTE on excise: the invoice states "naliczono akcyze 10,80 zl" as an
informational footnote — the 5 PLN/MWh is already inside the energy
price, NOT added on top (2159 kWh: 1320.01 + 32.36 + 919.37 = 2271.74
to the grosz). compute_bill therefore reports excise as an INFO line
only (key "excise"), excluded from sale_total/netto.

Pure functions only (no Home Assistant imports) so they stay unit-tested.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

VAT_RATE = 0.23
_HALF_UP = ROUND_HALF_UP


def _r2(x: float) -> float:
    """Round to grosze using ROUND_HALF_UP (invoice line convention).

    Energa bills every line rounded to grosze and sums the ROUNDED lines
    (verified: August 2026 net-billing akcyza 0,023 MWh -> 0,115 -> 0,12).
    Python's built-in ``round`` is banker's rounding, hence Decimal.
    """
    return float(Decimal(str(float(x))).quantize(Decimal("0.01"), rounding=_HALF_UP))


def hourly_saldo(
    import_hours: dict, export_hours: dict
) -> tuple[float, float, float, float]:
    """Per-hour netting of gross import/export for ONE metering zone.

    The Energa invoice settles each hour separately (``BP`` = bilansowanie
    prosumentów): import and export are netted hour-by-hour, so the energy
    and variable-distribution base is the sum of hourly POSITIVE balances
    ("salda dodatnie"), and the deposit base is the sum of NEGATIVE ones.

    Args:
        import_hours/export_hours: {hour_epoch: kWh} gross hourly series
            (missing hours count as 0).

    Returns:
        (saldo_plus, saldo_minus, gross_import, gross_export) in kWh.
    """
    plus = minus = g_imp = g_exp = 0.0
    for ts in set(import_hours) | set(export_hours):
        try:
            imp = float(import_hours.get(ts, 0.0) or 0.0)
            exp = float(export_hours.get(ts, 0.0) or 0.0)
        except (ValueError, TypeError):
            continue
        g_imp += imp
        g_exp += exp
        net = imp - exp
        if net > 0.0:
            plus += net
        elif net < 0.0:
            minus += -net
    return plus, minus, g_imp, g_exp


def bill_saldos(hourly: dict) -> dict:
    """Hourly-netted invoice bases for a (possibly two-zone) meter.

    Args:
        hourly: {"import_1": {ts: kWh}, "export_1": {...},
                 "import_2": {...}, "export_2": {...}} — one-zone meters
                 may provide only the ``_1`` keys.

    Returns dict with (kWh, rounded to 3 dp):
        saldo_plus_1/2   energy + variable distribution + quality base
        saldo_minus_1/2  export credited to the deposit
        gross_1/2        gross meter import (excise base helper)
        overlap_1/2      gross import - saldo plus (EXCISE base)
        total_plus       saldo_plus_1 + saldo_plus_2 (OZE/cogen base)
    """
    out: dict[str, float] = {}
    for zone in (1, 2):
        imp = hourly.get(f"import_{zone}") or {}
        exp = hourly.get(f"export_{zone}") or {}
        if not imp and not exp:
            continue
        plus, minus, g_imp, g_exp = hourly_saldo(imp, exp)
        out[f"saldo_plus_{zone}"] = round(plus, 3)
        out[f"saldo_minus_{zone}"] = round(minus, 3)
        out[f"gross_{zone}"] = round(g_imp, 3)
        out[f"overlap_{zone}"] = round(max(0.0, g_imp - plus), 3)
    out["total_plus"] = round(
        out.get("saldo_plus_1", 0.0) + out.get("saldo_plus_2", 0.0), 3
    )
    return out


def mtd_invoice_bases(mtd: dict, has_zones: bool, old_system: bool) -> dict:
    """Pick the invoice bases from a coordinator MTD flow dict.

    NEW net-billing is settled hour by hour by the OSD, so when the
    coordinator has written hourly-netted salda keys we invoice on:
        saldo_plus_1/2  -> energy + variable distribution + quality base
        saldo_minus_1/2 -> export credited to the deposit
        overlap_1/2     -> excise base ("nakładka", gross import - salda plus)
    Otherwise (old net-metering, second-zone-less or no hourly data yet) we
    fall back to the gross meter flows with excise kept informational.

    Returns dict:
        import_day/import_night/export  kWh for compute_bill
        excise_day/excise_night         kWh for compute_bill
        add_excise                      bool
        export_day/export_night         per-zone credited export (display)
        source                          "salda_hourly" | "gross_meter"
    """
    mtd = mtd or {}
    has_salda = "saldo_plus_1" in mtd or "saldo_plus_2" in mtd
    if has_salda:
        # Hourly-netted bases, billed as WHOLE kWh per zone (Agrestowa
        # 08.2026: 398,46 -> 398; 434,893 -> 435).
        if has_zones:
            plus_d = float(mtd.get("saldo_plus_1", 0.0))
            plus_n = float(mtd.get("saldo_plus_2", 0.0))
            minus = float(mtd.get("saldo_minus_1", 0.0)) + float(
                mtd.get("saldo_minus_2", 0.0)
            )
            over_d = float(mtd.get("overlap_1", 0.0))
            over_n = float(mtd.get("overlap_2", 0.0))
            gross_d = float(mtd.get("gross_1", mtd.get("import_1", 0.0)))
            gross_n = float(mtd.get("gross_2", mtd.get("import_2", 0.0)))
            exp_d = float(mtd.get("saldo_minus_1", 0.0))
            exp_n = float(mtd.get("saldo_minus_2", 0.0))
        else:
            plus_d = float(mtd.get("saldo_plus_1", 0.0))
            plus_n = 0.0
            minus = float(mtd.get("saldo_minus_1", 0.0))
            over_d = float(mtd.get("overlap_1", 0.0))
            over_n = 0.0
            gross_d = float(mtd.get("gross_1", mtd.get("import", 0.0)))
            gross_n = 0.0
            exp_d = exp_n = 0.0
        # Excise is a real netto line only for PROSUMERS (a plain consumer
        # has it inside the energy price — Warzywna FES/00017). Base:
        #   net-billing  -> "nakładka" (gross import - salda plus)
        #   net-metering -> gross import (Wiśniowa FES/00042: 0,120+0,372 MWh)
        is_prosumer = minus > 0.0
        exc_d, exc_n = (gross_d, gross_n) if old_system else (over_d, over_n)
        return {
            "import_day": float(round(plus_d)),
            "import_night": float(round(plus_n)),
            "export": float(round(minus)),
            "excise_day": float(round(exc_d)),
            "excise_night": float(round(exc_n)),
            "add_excise": bool(is_prosumer),
            "export_day": float(round(exp_d)) if has_zones else 0.0,
            "export_night": float(round(exp_n)) if has_zones else 0.0,
            "source": "salda_hourly",
        }
    if has_zones:
        imp_d = float(mtd.get("import_1", 0))
        imp_n = float(mtd.get("import_2", 0))
        exp = float(mtd.get("export_1", 0)) + float(mtd.get("export_2", 0))
        if not exp:
            exp = float(mtd.get("export", 0))
        exp_d = float(mtd.get("export_1", 0))
        exp_n = float(mtd.get("export_2", 0))
    else:
        imp_d = float(mtd.get("import", 0))
        imp_n = 0.0
        exp = float(mtd.get("export", 0))
        exp_d = exp_n = 0.0
    return {
        "import_day": imp_d,
        "import_night": imp_n,
        "export": exp,
        "excise_day": 0.0,
        "excise_night": 0.0,
        "add_excise": False,
        "export_day": exp_d,
        "export_night": exp_n,
        "source": "gross_meter",
    }

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

# G11 defaults, PLN net — exact values from a G11 consumer invoice
# (02-04.2026, 2159 kWh, single zone, no PV). Night/variable-night
# keys unused. Annual use on that invoice: 7312 kWh (top URE bracket).
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
    deposit_generated: float | None = None,
    excise_day: float = 0.0,
    excise_night: float = 0.0,
    add_excise: bool = False,
) -> dict:
    """Full monthly bill from meter flows.

    Args:
        import_day/night: kWh per zone (period). For net-billing pass the
            HOURLY-NETTED "salda dodatnie" (see :func:`bill_saldos`); for
            net-metering the gross meter import.
        export_kwh: kWh fed into the grid (period). For net-billing pass
            the hourly-netted "salda ujemne".
        rcem: invoiced monthly market price (volume-weighted, PSE table).
        fees: fee table (defaults to G12W_DEFAULT_FEES).
        months: how many monthly fixed fees to include.
        cover_day/night: kWh covered by the virtual warehouse (old
            net-metering only; energy charge drops, excise and
            distribution stay on the FULL import).
        deposit_pln: explicit TOTAL available deposit to subtract (new
            net-billing), i.e. opening balance + this period's generation.
            When None, computed as export_kwh*rcem*1.23.
        deposit_generated: this period's freshly generated deposit
            (export_kwh*rcem*1.23) for the breakdown. When None it is derived
            from ``export_kwh`` and ``rcem``; callers that pass an explicit
            total in ``deposit_pln`` (opening + generated) should pass it too
            so ``deposit_open`` is reported correctly.
        excise_day/night: kWh on which excise is charged — the "nakładka"
            (gross import - salda dodatnie) for net-billing. Informational
            when ``add_excise`` is False.
        add_excise: net-billing only — excise (5 PLN/MWh) is a REAL,
            separately invoiced line added to the net total (verified on
            Agrestowa 07-08.2026: 0,35 / 0,31 PLN). For G11 consumers and
            old net-metering the 5 PLN/MWh is already inside the energy
            price, so it stays informational.

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
    # charge AND from variable distribution + quality fee, but OZE/cogen
    # and all fixed fees stay on the FULL import (verified on the
    # G12W-stare 05-06.2026 invoice: variable rows 0.00, OZE + kogen
    # charged, energy rows 0 kWh).
    pay_day = max(0.0, import_day - max(0.0, float(cover_day)))
    pay_night = max(0.0, import_night - max(0.0, float(cover_night)))

    excise_rate = f["excise_mwh"] / 1000.0  # PLN per kWh
    if add_excise:
        line_excise_day = _r2(max(0.0, float(excise_day)) * excise_rate)
        line_excise_night = _r2(max(0.0, float(excise_night)) * excise_rate)
        excise_net = _r2(line_excise_day + line_excise_night)
    else:
        # Informational only (inside the energy price) — gross import.
        line_excise_day = line_excise_night = 0.0
        excise_net = 0.0
        excise_info = _r2((import_day + import_night) * excise_rate)

    # Sale lines, each rounded to grosze, then summed (invoice convention).
    line_energy_day = _r2(pay_day * f["energy_day"])
    line_energy_night = _r2(pay_night * f["energy_night"])
    trade = _r2(f["trade_fee"] * months)
    sale_total = _r2(line_energy_day + line_energy_night + trade + excise_net)

    # Distribution lines.
    line_var_day = _r2(pay_day * f["grid_var_day"])
    line_var_night = _r2(pay_night * f["grid_var_night"])
    pay_total = pay_day + pay_night
    total_kwh = import_day + import_night
    line_quality = _r2(pay_total * f["quality"])
    line_oze = _r2(total_kwh * f["oze"])
    line_cogen = _r2(total_kwh * f["cogen"])
    # Fixed monthly lines, each rounded to grosze (the invoice prints them
    # separately: abonament / sieciowa stała / mocowa). ``distr_fixed`` is
    # kept as their sum for backward compatibility.
    line_abonament = _r2(f["abonament"] * months)
    line_grid_fixed = _r2(f["grid_fixed"] * months)
    line_capacity = _r2(f["capacity"] * months)
    line_fixed = _r2(line_abonament + line_grid_fixed + line_capacity)
    distr_total = _r2(
        line_var_day + line_var_night + line_quality + line_oze + line_cogen + line_fixed
    )

    netto = _r2(sale_total + distr_total)
    vat = _r2(netto * VAT_RATE)
    brutto = _r2(netto + vat)

    # Deposit (new net-billing). Ustawa o OZE art. 4 ust. 11 + Agrestowa
    # 07-08.2026: the deposit is capped at ENERGY SALE gross (energy +
    # excise when excise is a real line) — never trade fee, never
    # distribution/grid fees.
    if deposit_generated is None:
        deposit_generated = _r2(export_kwh * float(rcem) * 1.23)
    else:
        deposit_generated = _r2(max(0.0, float(deposit_generated)))
    if deposit_pln is None:
        deposit_pln = deposit_generated
    deposit = _r2(max(0.0, float(deposit_pln)))
    # Opening balance = total available minus what this period generated.
    deposit_open = _r2(max(0.0, deposit - deposit_generated))
    cap_gross = _r2((line_energy_day + line_energy_night + excise_net) * (1.0 + VAT_RATE))
    applied = _r2(min(deposit, cap_gross))
    deposit_close = _r2(max(0.0, deposit - applied))
    do_zaplaty = _r2(brutto - applied)

    out = {
        "sale_energy_day": line_energy_day,
        "sale_energy_night": line_energy_night,
        "excise": excise_info if not add_excise else excise_net,
        "excise_day": line_excise_day,
        "excise_night": line_excise_night,
        "excise_added": bool(add_excise),
        "excise_note": (
            "doliczana do netto (net-billing)"
            if add_excise
            else "informacyjnie — akcyza jest już w cenie energii (G11/net-metering)"
        ),
        "trade_fee": trade,
        "sale_total": sale_total,
        "sale_gross": _r2((line_energy_day + line_energy_night + excise_net) * (1.0 + VAT_RATE)),
        "distr_var_day": line_var_day,
        "distr_var_night": line_var_night,
        "distr_quality": line_quality,
        "distr_oze": line_oze,
        "distr_cogen": line_cogen,
        "distr_abonament": line_abonament,
        "distr_grid_fixed": line_grid_fixed,
        "distr_capacity": line_capacity,
        "distr_fixed": line_fixed,
        "distr_total": distr_total,
        "distr_gross": _r2(distr_total * (1.0 + VAT_RATE)),
        "netto": netto,
        "vat": vat,
        "brutto": brutto,
        "deposit": deposit,
        "deposit_generated": deposit_generated,
        "deposit_open": deposit_open,
        "deposit_applied": applied,
        "deposit_close": deposit_close,
        "do_zaplaty": do_zaplaty,
    }
    return out


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
