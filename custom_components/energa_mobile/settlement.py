"""Settlement helpers for Energa My Meter (v0.2.11).

Legal background (verified 2026-09-04):
- Old net-metering (opusty 0.8/0.7): energy introduced to the grid can be
  collected within 12 months from introduction (counted from the last day of
  the introduction month), oldest energy first (FIFO).
  Sources: energa.pl/dom/strefa-prosumenta/net-metering,
  enerad.pl/net-metering-system-opustow
- New net-billing: deposit valid 12 months from assignment (assigned in the
  next calendar month, x1.23 multiplier), oldest funds first (FIFO), refund
  of unused funds capped at 20% (RCEm) / 30% (RCE since 01.02.2025).
  Sources: energa.pl/dom/strefa-prosumenta/net-billing,
  gov.pl 27.12.2024 (Dz.U. 1847), pse.pl/oire RCEm table.

IMPORTANT: a plain calendar reset (Jan 1 / every month) would NOT comply —
both systems are rolling 12-month FIFO windows. This module implements the
FIFO-compatible helpers: settlement anniversary math, rolling-window bank
formulas and month-to-date forecast math.

Pure functions only (no Home Assistant imports) so they can be unit-tested.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime

# Polish month names as used by PSE RCEm table
PSE_MONTHS = {
    "styczeń": 1,
    "luty": 2,
    "marzec": 3,
    "kwiecień": 4,
    "maj": 5,
    "czerwiec": 6,
    "lipiec": 7,
    "sierpień": 8,
    "wrzesień": 9,
    "październik": 10,
    "listopad": 11,
    "grudzień": 12,
}

# Day of month on which PSE publishes RCEm for the previous month
PSE_RCEM_PUBLICATION_DAY = 11


def parse_settlement_date(value: str | None) -> date | None:
    """Parse YYYY-MM-DD settlement anniversary. None when empty/invalid."""
    if not value or not str(value).strip():
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def next_settlement_date(settlement: date, today: date) -> date:
    """Next anniversary of a yearly settlement date (handles Feb 29)."""
    year = today.year
    while True:
        try:
            candidate = settlement.replace(year=year)
        except ValueError:
            candidate = date(year, 2, 28)  # Feb 29 -> Feb 28
        if candidate >= today:
            return candidate
        year += 1


def days_to_settlement(settlement_str: str | None, today: date | None = None) -> int | None:
    """Days from today to next settlement anniversary. None when not set."""
    base = parse_settlement_date(settlement_str)
    if base is None:
        return None
    today = today or date.today()
    return (next_settlement_date(base, today) - today).days


def target_rcem_month(today: date | None = None) -> tuple[int, int]:
    """(year, month) of the latest PUBLISHED RCEm.

    PSE publishes RCEm ~11th of the following month, so before the 11th the
    latest published month is month-2, otherwise month-1.
    """
    today = today or date.today()
    m = today.month - 1
    y = today.year
    if today.day < PSE_RCEM_PUBLICATION_DAY:
        m -= 1
    if m < 1:
        m += 12
        y -= 1
    return (y, m)


def parse_official_rcem_table(html: str) -> list[tuple[int, int, float]]:
    """Parse official RCEm values from PSE RCEm page HTML.

    Returns [(year, month, price_pln_per_kwh), ...]. Month/year is derived
    from the publication date (RCEm of month M is published ~11th of M+1).
    Prices on the page are in PLN/MWh and converted to PLN/kWh.
    """
    pattern = re.compile(
        r"<b>(styczeń|luty|marzec|kwiecień|maj|czerwiec|lipiec|sierpień"
        r"|wrzesień|październik|listopad|grudzień)</b>"
        r"[\s\S]*?RCEm&nbsp;[\s\S]*?"
        r'<td align="right">([\d\s]+,\d+)</td>\s*'
        r'<td align="center">(\d{2})\.(\d{2})\.(\d{4})</td>'
    )
    out: list[tuple[int, int, float]] = []
    for match in pattern.finditer(html):
        _month_name, raw_val, _pd, pm, py = match.groups()
        try:
            val_mwh = float(raw_val.replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        pub_month, pub_year = int(pm), int(py)
        if pub_month > 1:
            out.append((pub_year, pub_month - 1, round(val_mwh / 1000, 5)))
        else:
            out.append((pub_year - 1, 12, round(val_mwh / 1000, 5)))
    return out


def latest_official_rcem(
    html: str, today: date | None = None
) -> tuple[int, int, float] | None:
    """Latest RCEm published on/before today from PSE page HTML."""
    today = today or date.today()
    rows = parse_official_rcem_table(html)
    # Publication date of RCEm(M) is ~11th of M+1; keep rows published already
    valid = []
    for year, month in [(r[0], r[1]) for r in rows]:
        pub_y, pub_m = (year, month + 1) if month < 12 else (year + 1, 1)
        pub_days = calendar.monthrange(pub_y, pub_m)[1]
        pub_day = min(PSE_RCEM_PUBLICATION_DAY, pub_days)
        if date(pub_y, pub_m, pub_day) <= today:
            valid.append(next(r for r in rows if r[0] == year and r[1] == month))
    if not valid:
        return None
    return max(valid)


def rolling_kwh_bank(
    export_365d: float, import_365d: float, coefficient: float
) -> float:
    """Old-system bank from last-365-day flows (FIFO expiry by construction).

    Energy older than 12 months expires, so only the trailing 365 days count:
    bank = max(0, export_365 * coeff - import_365).
    """
    return round(max(0.0, export_365d * coefficient - import_365d), 2)


def fifo_kwh_bank(
    monthly_flows, coefficient: float, today=None
) -> tuple:
    """Old-system warehouse from monthly flows with real FIFO expiry.

    Rules (Energa net-metering, verified 2026-09-04): energy introduced in
    month M (export x coefficient) can be collected until the END of month
    M+12; each month's import consumes the OLDEST live energy first.
    Uncovered import is lost (it was paid), expired leftovers vanish.

    Args:
        monthly_flows: iterable of (year, month, import_kwh, export_kwh).
        coefficient: prosumer factor (0.8 / 0.7).
        today: reference date (default: today).

    Returns (bank_kwh, detail) where detail holds expired_kwh,
    uncovered_kwh, deposits_kwh (total credited in the live window)
    and months_used. Pure function, unit-tested.
    """
    from collections import defaultdict
    from datetime import date as _date

    today = today or _date.today()
    try:
        coeff = float(coefficient)
    except (ValueError, TypeError):
        coeff = 0.8
    agg: dict = defaultdict(lambda: [0.0, 0.0])
    for row in monthly_flows or []:
        try:
            y, m, imp, exp = row
            agg[(int(y), int(m))][0] += max(0.0, float(imp))
            agg[(int(y), int(m))][1] += max(0.0, float(exp))
        except (ValueError, TypeError):
            continue
    detail = {"expired_kwh": 0.0, "uncovered_kwh": 0.0, "months_used": 0,
              "deposits_kwh": 0.0}
    if not agg:
        return (0.0, detail)
    cur_idx = today.year * 12 + today.month
    buckets: list = []  # [expiry_month_idx, balance_kwh]
    expired = 0.0
    uncovered = 0.0
    deposited = 0.0
    used = 0
    for (y, m) in sorted(agg):
        idx = y * 12 + m
        if idx > cur_idx:
            break  # future data ignored
        # Expire first, then introduce (readability over cleverness)
        live: list = []
        for exp_i, bal in buckets:
            if exp_i < idx:
                expired += bal
            else:
                live.append([exp_i, bal])
        buckets = live
        intro = agg[(y, m)][1] * coeff
        if intro > 0:
            buckets.append([idx + 12, intro])
            deposited += intro
        need = agg[(y, m)][0]
        if need > 0 or intro > 0:
            used += 1
        for b in buckets:
            if need <= 0:
                break
            take = min(b[1], need)
            b[1] -= take
            need -= take
        uncovered += max(0.0, need)
        buckets = [b for b in buckets if b[1] > 1e-9]
    bank = 0.0
    for exp_i, bal in buckets:
        if exp_i < cur_idx:
            expired += bal
        else:
            bank += bal
    detail.update({
        "expired_kwh": round(expired, 2),
        "uncovered_kwh": round(uncovered, 2),
        "months_used": used,
        "deposits_kwh": round(deposited, 2),
    })
    return (round(bank, 2), detail)


def anchor_flow_series(cums, base: float = 0.0) -> list:
    """Anchor a cumulative flow series on an existing sum (v0.3.4).

    Reimports must CONTINUE from already-imported totals: restarting at
    0 makes the recorder see a meter reset and the battery bars collapse
    (e.g. G12W prosumer: 5889 kWh -> 0.0 overnight). First point keeps
    state 0.0 (anchor, no spike); every next state is cum - prev.
    Pure function, unit-tested.
    """
    try:
        base_f = max(0.0, float(base))
    except (ValueError, TypeError):
        base_f = 0.0
    out: list = []
    prev = base_f
    for c in cums or []:
        try:
            cum = round(base_f + max(0.0, float(c)), 2)
        except (ValueError, TypeError):
            cum = round(prev, 2)
        out.append((cum, round(cum - prev, 3)))
        prev = cum
    return out


def reset_aware_delta(sums) -> float:
    """True flow from a recorder `sum` column with mid-window resets (v0.3.5).

    `last - first` goes hugely negative when a statistics reimport restarts
    a series at 0 inside the window (seen live: a September panel
    reimport wrote the month-boundary row with sum 0.0, poisoning the whole
    previous month bucket to -5509 kWh and cutting the FIFO bank by 492 kWh).
    Summing only the positive segments instead measures the real flow:
    monotonic series give exactly `last - first`, a mid-window reset adds
    the pre-reset flow and the post-reset regrow, and a reset ON the last
    row counts just the flow before it. Pure function, unit-tested.
    """
    try:
        vals = [float(s) for s in (sums or []) if s is not None]
    except (ValueError, TypeError):
        return 0.0
    if len(vals) < 2:
        return 0.0
    total = 0.0
    for prev, cur in zip(vals, vals[1:]):
        total += max(0.0, cur - prev)
    return round(total, 3)


def bucket_flows(series, max_hourly=None) -> list:
    """Group hourly flow points by timestamp into (import, export) slots.

    Args:
        series: iterable of (points, slot) where each point is
            {"dt": datetime, "value": kWh} and slot is 0 (import)
            or 1 (export). Same hour from several series adds up
            (DST fallback duplicates included).
        max_hourly: optional per-hour spike guard (kWh); higher
            values are skipped entirely.

    Returns ordered list of (dt, (import_kwh, export_kwh)).
    Pure function, unit-tested.
    """
    by_hour: dict = {}
    for points, idx in series or []:
        try:
            slot_idx = int(idx)
        except (ValueError, TypeError):
            continue
        if slot_idx not in (0, 1):
            continue
        for p in points or []:
            try:
                dt = p["dt"]
                v = float(p["value"])
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            if v < 0:
                continue
            if max_hourly is not None:
                try:
                    if v > float(max_hourly):
                        continue
                except (ValueError, TypeError):
                    pass
            slot = by_hour.setdefault(dt, [0.0, 0.0])
            slot[slot_idx] += max(0.0, v)
    return [(dt, (v[0], v[1])) for dt, v in sorted(by_hour.items())]


def warehouse_level_pct(bank_kwh: float | None, deposits_kwh: float | None) -> float | None:
    """Poziom magazynu w % (stary net-metering, v0.3.0).

    bank vs suma wkładów (export×coeff) z żywego okna 12 m-cy:
    100% = nic nie odebrane / nic nie wygasło, 0% = magazyn pusty.
    None, gdy brak historii (tryb baseline) — nie zgadujemy.
    """
    try:
        deposits = float(deposits_kwh)  # type: ignore[arg-type]
        bank = float(bank_kwh)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
    if deposits <= 0:
        return None
    return round(max(0.0, min(1.0, bank / deposits)) * 100.0, 1)


def is_export_prosumer(meter: dict | None) -> bool:
    """True when the meter can actually export (prosumer, v0.2.15).

    `obis_minus` alone is NOT enough: consumer meters (e.g. G11 without PV)
    may still report export OBIS codes with zero readings, which used to
    spawn a useless `0.0` Bank, misleading `Bank Ładowanie/Rozładowanie`
    flows and a `Bilans Prosumencki` that is trivially `-import`.
    Require either the seller flag (`type: Wytwórca`) or a non-zero
    export total.
    """
    if not meter:
        return False
    if meter.get("is_prosumer"):
        return True
    for key in (
        "total_minus",
        "total_minus_1",
        "total_minus_2",
        "export",
        "export_1",
        "export_2",
    ):
        try:
            if float(meter.get(key) or 0) > 0:
                return True
        except (ValueError, TypeError):
            continue
    return False


def orphan_bank_uids(
    meter_id: str, serial: str, is_prosumer: bool, coefficient: float | None
) -> set:
    """Unique IDs of stale prosumer entities to remove (v0.2.15+).

    - Consumer meters: the whole prosumer set (Bilans, banks, flows,
      RCEm, forecast, export live/stats/costs/prices).
    - Prosumer meters: only the bank of the INACTIVE settlement system
      (e.g. Bank kWh left over after switching to net-billing, or vice
      versa when baselines were configured under a previous coefficient).
    Pure helper so the rule stays unit-tested; sensor.py applies it.
    """
    mid, ser = str(meter_id), str(serial or meter_id)
    if not is_prosumer:
        # NOTE: bill_forecast intentionally NOT here — since v0.2.17
        # consumers get a plain import-bill forecast too.
        return {
            f"energa_{mid}_prosumer_balance",
            f"energa_{mid}_bank_kwh",
            f"energa_{mid}_bank_pln",
            f"energa_{mid}_bank_charge",
            f"energa_{mid}_bank_discharge",
            f"energa_{mid}_rcem_auto",
            f"energa_{mid}_daily_produkcja_live",
            f"energa_{mid}_export_stats",
            f"energa_{mid}_export_1_stats",
            f"energa_{mid}_export_2_stats",
            f"energa_{ser}_export_price",
            f"energa_{ser}_coefficient_price",
            f"energa_{ser}_export_cost_stats",
            f"energa_{ser}_export_1_cost_stats",
            f"energa_{ser}_export_2_cost_stats",
        }
    try:
        coeff = float(coefficient)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return set()
    if coeff >= 0.7:
        return {f"energa_{mid}_bank_pln", f"energa_{mid}_rcem_auto"}
    return {f"energa_{mid}_bank_kwh", f"energa_{mid}_bank_level"}


def orphan_removed_uids(meter_id: str, serial: str) -> set:
    """Unique IDs of entities removed in v0.3.0 (auto-cleaned, v0.3.0).

    - `Wykryj pierwszy odczyt` button: blind 730-day auto-backfill on
      setup replaced manual detection (overengineering, per user ask).
    - Export cost placeholders/stats: export is priced live via the
      RCEm/Cena Oddania entity now, never frozen at 0.95.
    Pure helper so the rule stays unit-tested; sensor.py applies it.
    """
    mid, ser = str(meter_id), str(serial or meter_id)
    return {
        f"energa_{mid}_detect_first_data",
        f"energa_{ser}_export_cost_stats",
        f"energa_{ser}_export_1_cost_stats",
        f"energa_{ser}_export_2_cost_stats",
    }


def month_to_date_forecast(
    mtd_net_pln: float, day_of_month: int, days_in_month: int
) -> float:
    """Linear month-end forecast from month-to-date net position."""
    if day_of_month < 1 or days_in_month < 1:
        return round(mtd_net_pln, 2)
    elapsed = min(day_of_month, days_in_month)
    return round(mtd_net_pln / elapsed * days_in_month, 2)


def deposit_valid_until(year: int, month: int) -> date:
    """Date until which a monthly deposit stays valid (assignment + 12m).

    Deposit for month M is assigned in M+1 and valid 12 months from
    assignment, i.e. until the last day of month M+13.
    """
    idx = (year * 12 + (month - 1)) + 13
    y, m0 = divmod(idx, 12)
    return _last_day_of(y, m0 + 1)


def trailing_months(today=None, count: int = 13) -> list:
    """[(year, month), ...] oldest-first ending with today's month."""
    from datetime import date as _date

    today = today or _date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(max(1, int(count))):
        out.append((y, m))
        m -= 1
        if m < 1:
            m = 12
            y -= 1
    return out[::-1]


def _last_day_of(year: int, month: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last)


class FlowAccumulator:
    """Pure helper (no HA) splitting a moving base value into charge/discharge.

    Feeds the native Bank charge/discharge sensors (v0.2.12, Energy battery):
    - old net-metering: feed with Bilans (net_exp*coeff - net_imp);
      Bilans growth charges the battery, shrinkage discharges it.
    - new net-billing: feed one instance with net_export (charge side)
      and a second instance with net_import (discharge side).

    First update() only anchors the baseline (no spike after restart);
    use restored to re-seed totals after HA restart.
    """

    def __init__(self, initial: float = 0.0) -> None:
        self.charge = round(float(initial), 2)
        self.discharge = round(float(initial), 2)
        self._last: float | None = None

    def restore(self, charge: float | None, discharge: float | None) -> None:
        """Re-seed totals (e.g. from HA last state after restart)."""
        if charge is not None:
            self.charge = round(float(charge), 2)
        if discharge is not None:
            self.discharge = round(float(discharge), 2)

    def update(self, base: float | None) -> tuple[float, float]:
        """Fold a new base reading into (charge, discharge) totals."""
        if base is None:
            return (self.charge, self.discharge)
        base = float(base)
        if self._last is None:
            self._last = base
            return (self.charge, self.discharge)
        delta = base - self._last
        self._last = base
        if delta > 0:
            self.charge = round(self.charge + delta, 2)
        elif delta < 0:
            self.discharge = round(self.discharge - delta, 2)
        return (self.charge, self.discharge)


def flow_history_series(hourly, coefficient: float, old_system: bool) -> tuple:
    """Historical (charge, discharge) cumulative series (v0.2.23).

    Replays the live FlowAccumulator semantics over imported hourly flows
    so Download History also backfills the Energy battery (not just Panel
    Energia). Baselines don't matter (only deltas flow through).

    Args:
        hourly: iterable of (import_kwh, export_kwh) per hour, chronological.
        coefficient: prosumer factor (old system only).
        old_system: True = Bilans movement; False = export/import growth.

    Returns (charge_points, discharge_points): lists of cumulative kWh
    aligned with `hourly` (first point 0.0 — anchor, like live).
    Pure function, unit-tested.
    """
    try:
        coeff = float(coefficient)
    except (ValueError, TypeError):
        coeff = 0.8
    charge, discharge = [], []
    ch_tot, dis_tot = 0.0, 0.0
    cum_exp, cum_imp = 0.0, 0.0
    prev_base = None
    for row in hourly or []:
        try:
            imp, exp = max(0.0, float(row[0])), max(0.0, float(row[1]))
        except (ValueError, TypeError, IndexError):
            charge.append(round(ch_tot, 2))
            discharge.append(round(dis_tot, 2))
            continue
        if old_system:
            # Mirror live mode: Bilans of CUMULATIVE flows, split deltas.
            cum_exp += exp
            cum_imp += imp
            base = cum_exp * coeff - cum_imp
            if prev_base is None:
                prev_base = base  # anchor, like live first update
            else:
                delta = base - prev_base
                prev_base = base
                if delta > 0:
                    ch_tot += delta
                elif delta < 0:
                    dis_tot -= delta
        else:
            # Mirror live mode: export growth charges, import growth discharges.
            if exp > 0:
                ch_tot += exp
            if imp > 0:
                dis_tot += imp
        charge.append(round(ch_tot, 2))
        discharge.append(round(dis_tot, 2))
    return (charge, discharge)
