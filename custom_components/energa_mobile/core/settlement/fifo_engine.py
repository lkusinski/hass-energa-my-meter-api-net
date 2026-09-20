"""Shared FIFO net-metering engine (old system, physical kWh warehouse).

Single source of truth for the 12-month, oldest-first allocation used by the
production sensors (`settlement.fifo_kwh_bank` / `fifo_dual_zone_kwh_bank`) and
by the pure domain layer (`core/settlement/fifo_net_metering`).

Extracted verbatim from `settlement.py` during P2.1. Numeric behaviour is pinned
byte-for-byte by `tests/test_fifo_golden.py` — this module, and the wrappers in
`settlement.py`, must keep producing identical values (they are invoice-validated
to the grosz). Pure functions only (no Home Assistant imports).

Rules (Energa net-metering, verified 2026-09-04): energy introduced in month M
(export x coefficient) can be collected until the END of month M+12; each month's
import consumes the OLDEST live energy first. Uncovered import is lost (it was
paid), expired leftovers vanish.
"""

from __future__ import annotations

from collections import defaultdict


def _run_fifo(monthly_flows, coefficient: float, today, record: bool) -> tuple:
    """Core old-system FIFO allocation.

    Returns ``(bank_kwh, detail, trace)``. ``trace`` is ``None`` unless
    ``record`` is set; when set it carries per-lot and per-allocation
    provenance for the pure domain layer. Recording only appends to lists and
    never touches the arithmetic, so `fifo_kwh_bank` stays byte-identical.
    """
    from datetime import date as _date

    today = today or _date.today()  # noqa: DTZ011 (naive local date is the project-wide convention)
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
    trace = {"lots": [], "allocations": [], "periods": []} if record else None
    if not agg:
        return (0.0, detail, trace)
    cur_idx = today.year * 12 + today.month
    buckets: list = []  # [expiry_month_idx, balance_kwh, lot_record|None]
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
        for exp_i, bal, lot in buckets:
            if exp_i < idx:
                expired += bal
                if lot is not None:
                    lot["expired_kwh"] += bal
            else:
                live.append([exp_i, bal, lot])
        buckets = live
        intro = agg[(y, m)][1] * coeff
        lot_rec = None
        if intro > 0:
            if trace is not None:
                lot_rec = {"year": y, "month": m, "credited_kwh": intro,
                           "remaining_kwh": intro, "expired_kwh": 0.0,
                           "expiry_idx": idx + 12}
                trace["lots"].append(lot_rec)
            buckets.append([idx + 12, intro, lot_rec])
            deposited += intro
        need = agg[(y, m)][0]
        if need > 0 or intro > 0:
            used += 1
        period = None
        if trace is not None:
            period = {"year": y, "month": m, "deposit_kwh": intro,
                      "import_kwh": need, "uncovered_kwh": 0.0}
        for b in buckets:
            if need <= 0:
                break
            take = min(b[1], need)
            b[1] -= take
            need -= take
            if b[2] is not None:
                b[2]["remaining_kwh"] -= take
                trace["allocations"].append(
                    {"year": y, "month": m, "lot": b[2], "amount_kwh": take})
        uncovered += max(0.0, need)
        if period is not None:
            period["uncovered_kwh"] = max(0.0, need)
            trace["periods"].append(period)
        buckets = [b for b in buckets if b[1] > 1e-9]
    bank = 0.0
    for exp_i, bal, lot in buckets:
        if exp_i < cur_idx:
            expired += bal
            if lot is not None:
                lot["expired_kwh"] += bal
        else:
            bank += bal
    detail.update({
        "expired_kwh": round(expired, 2),
        "uncovered_kwh": round(uncovered, 2),
        "months_used": used,
        "deposits_kwh": round(deposited, 2),
    })
    return (round(bank, 2), detail, trace)


def fifo_kwh_bank(monthly_flows, coefficient: float, today=None) -> tuple:
    """Old-system warehouse from monthly flows with real FIFO expiry.

    Args:
        monthly_flows: iterable of (year, month, import_kwh, export_kwh).
        coefficient: prosumer factor (0.8 / 0.7).
        today: reference date (default: today).

    Returns (bank_kwh, detail) where detail holds expired_kwh,
    uncovered_kwh, deposits_kwh (total credited in the live window)
    and months_used. Pure function, unit-tested.
    """
    bank, detail, _ = _run_fifo(monthly_flows, coefficient, today, record=False)
    return (bank, detail)


def fifo_kwh_bank_trace(monthly_flows, coefficient: float, today=None) -> tuple:
    """Same as `fifo_kwh_bank` but also returns per-lot provenance.

    Returns ``(bank_kwh, detail, trace)`` where ``trace`` holds the chronological
    ``lots`` (credited/remaining/expired kWh, expiry month index), the
    ``allocations`` (which month consumed how much of which lot) and the monthly
    ``periods``. Used by the pure domain layer so it shares exactly one
    allocation algorithm with production.
    """
    return _run_fifo(monthly_flows, coefficient, today, record=True)


def fifo_dual_zone_kwh_bank(
    flows_l1, flows_l2, coefficient: float, today=None
) -> tuple[float, dict]:
    """Dual-zone (L1 day / L2 night) old-system warehouse calculation (v1.4.0).

    Energa Operator net-metering rules for multi-zone tariffs (G12, G12w, G12r):
    energy introduced in Zone 1 settles ONLY against consumption in Zone 1.
    Energy introduced in Zone 2 settles ONLY against consumption in Zone 2.
    Each zone maintains its own independent FIFO 12-month expiry queue.

    Args:
        flows_l1: iterable of (year, month, import_kwh, export_kwh) for Zone 1.
        flows_l2: iterable of (year, month, import_kwh, export_kwh) for Zone 2.
        coefficient: prosumer factor (0.8 / 0.7).
        today: reference date (default: today).

    Returns:
        (total_bank, combined_detail)
    """
    bank_1, detail_1 = fifo_kwh_bank(flows_l1, coefficient, today=today)
    bank_2, detail_2 = fifo_kwh_bank(flows_l2, coefficient, today=today)
    total_bank = round(bank_1 + bank_2, 2)

    total_expired = round(detail_1.get("expired_kwh", 0.0) + detail_2.get("expired_kwh", 0.0), 2)
    total_uncovered = round(detail_1.get("uncovered_kwh", 0.0) + detail_2.get("uncovered_kwh", 0.0), 2)
    total_deposits = round(detail_1.get("deposits_kwh", 0.0) + detail_2.get("deposits_kwh", 0.0), 2)
    months_used = max(detail_1.get("months_used", 0), detail_2.get("months_used", 0))

    l1_share = round((bank_1 / total_bank * 100.0), 1) if total_bank > 0 else 0.0
    l2_share = round((bank_2 / total_bank * 100.0), 1) if total_bank > 0 else 0.0

    combined_detail = {
        "bank_kwh_l1": bank_1,
        "bank_kwh_l2": bank_2,
        "bank_l1_share_pct": l1_share,
        "bank_l2_share_pct": l2_share,
        "expired_kwh": total_expired,
        "uncovered_kwh": total_uncovered,
        "deposits_kwh": total_deposits,
        "months_used": months_used,
        "detail_l1": detail_1,
        "detail_l2": detail_2,
    }
    return (total_bank, combined_detail)
