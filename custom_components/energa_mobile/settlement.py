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
