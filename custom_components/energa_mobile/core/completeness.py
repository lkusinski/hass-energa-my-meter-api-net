"""Pure completeness evaluation for a user-selected settlement period.

Given the period bounds (``date`` entities) and a per-register hourly reading
map collected from the recorder (or the Energa API as a fallback), decide
whether the period is fully covered:

``complete``
    Every calendar day in ``[start, end]`` has a covered reading (a
    statistics row exists, even if the value is ``0``).
``incomplete``
    At least one required day has no reading.
``unknown``
    The period bounds are not set (or are invalid), so nothing can be judged.

The module deliberately has **no Home Assistant imports** (mirroring
``core.verification``) so the logic stays unit-testable. Weekends are never
counted as missing: a Saturday/Sunday without a reading must not turn a valid
period into a false ``incomplete`` (the meter may genuinely have no load, or
the API may only publish weekday rows).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

STATE_COMPLETE = "complete"
STATE_INCOMPLETE = "incomplete"
STATE_UNKNOWN = "unknown"

# Cap the missing-day list surfaced to the UI; the count stays exact.
MAX_MISSING_LIST = 92


def parse_date(value) -> date | None:
    """Parse a date/datetime/ISO string defensively into a ``date``."""
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


def registers_for_meter(meter: dict) -> list:
    """Return the normalised register keys a meter must cover.

    Keys match the ones produced by the service collectors
    (``import_1``/``import_2`` and, for prosumers, ``export_1``/``export_2``);
    single-zone meters use the ``_1`` suffix as well.
    """
    meter = meter or {}
    try:
        has_zones = int(meter.get("zone_count", 1) or 1) > 1
    except (ValueError, TypeError):
        has_zones = False
    import_registers = ["import_1", "import_2"] if has_zones else ["import_1"]
    registers = list(import_registers)
    try:
        from ..settlement import is_export_prosumer

        prosumer = bool(is_export_prosumer(meter))
    except Exception:  # noqa: BLE001 - detection must never break the check
        prosumer = False
    if prosumer:
        registers += [r.replace("import", "export") for r in import_registers]
    return registers


def _to_local_date(value, tz) -> date | None:
    """Normalise a series key (datetime / date / epoch seconds) to a local date."""
    try:
        if isinstance(value, datetime):
            moment = (
                value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            )
            return moment.astimezone(tz).date()
        if isinstance(value, date):
            return value
        return datetime.fromtimestamp(float(value), tz=tz).date()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _covered_dates(series, tz) -> set:
    """Local dates for which ``series`` (``{key: kWh}``) has a reading."""
    days: set = set()
    for key in (series or {}):
        moment = _to_local_date(key, tz)
        if moment is not None:
            days.add(moment)
    return days


def _day_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _attributes(
    state: str,
    *,
    period_start,
    period_end,
    expected_days: int,
    available_days: int,
    missing_days: list,
    source_checked,
    checked_at: str,
    registers: dict | None = None,
) -> dict:
    pct = (
        round((available_days / expected_days) * 100, 1) if expected_days else 0.0
    )
    attrs = {
        "period_start": period_start,
        "period_end": period_end,
        "expected_days": expected_days,
        "available_days": available_days,
        "missing_days": missing_days,
        "completeness_pct": pct,
        "source_checked": source_checked,
        "checked_at": checked_at,
    }
    if registers is not None:
        attrs["registers"] = registers
    attrs["state"] = state
    return attrs


def unknown_result(period_start=None, period_end=None, *, source_checked=None, now=None) -> dict:
    """Result for an unset/invalid period (state ``unknown``)."""
    checked = _iso_now(now)
    return _attributes(
        STATE_UNKNOWN,
        period_start=period_start,
        period_end=period_end,
        expected_days=0,
        available_days=0,
        missing_days=[],
        source_checked=source_checked,
        checked_at=checked,
    )


def evaluate_completeness(
    *,
    period_start,
    period_end,
    hourly_by_zone: dict | None,
    registers: list | None = None,
    source_checked: str | None = None,
    tz=timezone.utc,
    now=None,
) -> dict:
    """Evaluate coverage of ``[period_start, period_end]``.

    Args:
        period_start/period_end: date / datetime / ISO string bounds (inclusive).
        hourly_by_zone: ``{register: {epoch_or_datetime: kWh}}`` readings, as
            produced by the recorder/API collectors.
        registers: required register keys; defaults to the keys present in
            ``hourly_by_zone``.
        source_checked: ``"recorder"`` | ``"api"`` (surfaced for the user).
        tz: timezone used to bucket hourly readings into local days.
        now: override for the ``checked_at`` timestamp (tests).

    Returns:
        Attribute dict with ``state`` plus ``period_start``, ``period_end``,
        ``expected_days``, ``available_days``, ``missing_days``,
        ``completeness_pct``, ``source_checked``, ``checked_at`` and per-zone
        ``registers`` counts.
    """
    start = parse_date(period_start)
    end = parse_date(period_end)
    if start is None or end is None or end < start:
        return unknown_result(
            period_start=_as_iso(period_start),
            period_end=_as_iso(period_end),
            source_checked=source_checked,
            now=now,
        )

    hourly = hourly_by_zone or {}
    if registers:
        required = list(registers)
    else:
        required = [key for key in hourly]

    expected = list(_day_range(start, end))
    expected_count = len(expected)

    covered_by_register = {
        register: _covered_dates(hourly.get(register), tz) for register in required
    }
    active = [register for register in required if covered_by_register[register]]

    # A day is available when any active register has a reading for it, or it
    # is a weekend (never treated as missing).
    available = set()
    for day in expected:
        if day.weekday() >= 5:
            available.add(day)
            continue
        if any(day in covered_by_register[register] for register in active):
            available.add(day)

    missing = [day for day in expected if day not in available]
    missing_count = len(missing)
    state = STATE_COMPLETE if missing_count == 0 else STATE_INCOMPLETE

    register_counts = {}
    for register in required:
        covered = covered_by_register[register]
        register_counts[register] = {
            "expected_days": expected_count,
            "available_days": len([d for d in expected if d in covered]),
            "missing_days": len([d for d in expected if d not in covered]),
            "has_data": bool(covered),
        }

    return _attributes(
        state,
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        expected_days=expected_count,
        available_days=expected_count - missing_count,
        missing_days=[d.isoformat() for d in missing[:MAX_MISSING_LIST]],
        source_checked=source_checked,
        checked_at=_iso_now(now),
        registers=register_counts,
    )


def _as_iso(value) -> str | None:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed is not None else None


def _iso_now(now=None) -> str:
    moment = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.replace(microsecond=0).isoformat()


__all__ = [
    "MAX_MISSING_LIST",
    "STATE_COMPLETE",
    "STATE_INCOMPLETE",
    "STATE_UNKNOWN",
    "evaluate_completeness",
    "parse_date",
    "registers_for_meter",
    "unknown_result",
]
