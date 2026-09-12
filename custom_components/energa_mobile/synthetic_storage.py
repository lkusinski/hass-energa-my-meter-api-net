"""Pure calculation engine for synthetic virtual storage (Net-metering) in Home Assistant.

Legal and mathematical foundation:
- Under Polish Net-metering (Ustawa o OZE art. 4 ust. 1 i 11), prosumers receive
  an opust ratio (0.8 for <= 10 kW, 0.7 for > 10 kW).
- The remaining 20% (or 30%) retained by DSO is the in-kind fee (prowizja rzeczowa)
  covering balancing, storage, and grid distribution.
- Energy discharged from the virtual warehouse has 0 PLN/kWh variable cost.
- This module splits raw meter flows into:
    1. Virtual Battery Charge: export * coeff
    2. Grid Return Fee: export * (1 - coeff) (exported to grid at 0 PLN)
    3. Virtual Battery Discharge: min(import, available_bank)
    4. Net Grid Import: max(0, import - discharge)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def calculate_synthetic_storage(
    hourly_records: list[dict[str, Any]],
    coeff: float = 0.8,
    has_zones: bool = False,
    initial_bank_1: float = 0.0,
    initial_bank_2: float = 0.0,
    base_sums: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Calculate hourly synthetic warehouse and grid series from raw hourly points.

    Args:
        hourly_records: List of dicts sorted by dt, each having:
            'dt': datetime
            'import': float (single zone) or 'import_1', 'import_2' (multi zone)
            'export': float (single zone) or 'export_1', 'export_2' (multi zone)
        coeff: Prosumer discount ratio (0.8 for <=10 kW, 0.7 for >10 kW).
        has_zones: True if G12/G12w multi-zone, False for G11 single-zone.
        initial_bank_1: Starting bank balance for Zone 1 / Day (kWh).
        initial_bank_2: Starting bank balance for Zone 2 / Night (kWh).
        base_sums: Optional previous cumulative sums for incremental updates.

    Returns:
        Dict mapping metric names to lists of {'dt': dt, 'state': float, 'sum': float},
        plus ending bank balances.
    """
    if not hourly_records:
        return {
            "series": {},
            "ending_bank_1": initial_bank_1,
            "ending_bank_2": initial_bank_2,
            "ending_bank": initial_bank_1 + initial_bank_2,
        }

    # Clean and sort chronologically
    records = sorted(hourly_records, key=lambda r: r["dt"])
    base = base_sums or {}

    if has_zones:
        return _calculate_multi_zone(
            records, coeff, initial_bank_1, initial_bank_2, base
        )
    return _calculate_single_zone(
        records, coeff, initial_bank_1 + initial_bank_2, base
    )


def _calculate_single_zone(
    records: list[dict[str, Any]],
    coeff: float,
    initial_bank: float,
    base_sums: dict[str, float],
) -> dict[str, Any]:
    bank = max(0.0, float(initial_bank))
    fee_ratio = max(0.0, 1.0 - coeff)

    sum_charge = max(0.0, float(base_sums.get("magazyn_ladowanie", 0.0)))
    sum_discharge = max(0.0, float(base_sums.get("magazyn_rozladowanie", 0.0)))
    sum_grid_export = max(0.0, float(base_sums.get("siec_oddanie", 0.0)))
    sum_grid_import = max(0.0, float(base_sums.get("siec_pobor", 0.0)))

    series_charge: list[dict[str, Any]] = []
    series_discharge: list[dict[str, Any]] = []
    series_export: list[dict[str, Any]] = []
    series_import: list[dict[str, Any]] = []

    for rec in records:
        dt = rec["dt"]
        raw_imp = max(0.0, float(rec.get("import", 0.0) or 0.0))
        raw_exp = max(0.0, float(rec.get("export", 0.0) or 0.0))

        # 1. Charge & Grid Fee
        charge = round(raw_exp * coeff, 4)
        fee = round(raw_exp * fee_ratio, 4)
        bank += charge

        # 2. Discharge & Net Import
        discharge = min(raw_imp, bank)
        bank = max(0.0, bank - discharge)
        net_import = max(0.0, raw_imp - discharge)

        sum_charge += charge
        sum_discharge += discharge
        sum_grid_export += fee
        sum_grid_import += net_import

        series_charge.append({"dt": dt, "state": charge, "sum": round(sum_charge, 4)})
        series_discharge.append({"dt": dt, "state": discharge, "sum": round(sum_discharge, 4)})
        series_export.append({"dt": dt, "state": fee, "sum": round(sum_grid_export, 4)})
        series_import.append({"dt": dt, "state": net_import, "sum": round(sum_grid_import, 4)})

    return {
        "series": {
            "magazyn_ladowanie": series_charge,
            "magazyn_rozladowanie": series_discharge,
            "siec_oddanie": series_export,
            "siec_pobor": series_import,
        },
        "ending_bank": round(bank, 4),
        "ending_bank_1": round(bank, 4),
        "ending_bank_2": 0.0,
    }


def _calculate_multi_zone(
    records: list[dict[str, Any]],
    coeff: float,
    initial_bank_1: float,
    initial_bank_2: float,
    base_sums: dict[str, float],
) -> dict[str, Any]:
    bank_1 = max(0.0, float(initial_bank_1))
    bank_2 = max(0.0, float(initial_bank_2))
    fee_ratio = max(0.0, 1.0 - coeff)

    sum_ch1 = max(0.0, float(base_sums.get("magazyn_l1_ladowanie", 0.0)))
    sum_dis1 = max(0.0, float(base_sums.get("magazyn_l1_rozladowanie", 0.0)))
    sum_ch2 = max(0.0, float(base_sums.get("magazyn_l2_ladowanie", 0.0)))
    sum_dis2 = max(0.0, float(base_sums.get("magazyn_l2_rozladowanie", 0.0)))
    sum_exp1 = max(0.0, float(base_sums.get("siec_oddanie_strefa_1", 0.0)))
    sum_exp2 = max(0.0, float(base_sums.get("siec_oddanie_strefa_2", 0.0)))
    sum_imp1 = max(0.0, float(base_sums.get("siec_pobor_strefa_1", 0.0)))
    sum_imp2 = max(0.0, float(base_sums.get("siec_pobor_strefa_2", 0.0)))

    s_ch1: list[dict[str, Any]] = []
    s_dis1: list[dict[str, Any]] = []
    s_ch2: list[dict[str, Any]] = []
    s_dis2: list[dict[str, Any]] = []
    s_exp1: list[dict[str, Any]] = []
    s_exp2: list[dict[str, Any]] = []
    s_imp1: list[dict[str, Any]] = []
    s_imp2: list[dict[str, Any]] = []

    for rec in records:
        dt = rec["dt"]
        imp1 = max(0.0, float(rec.get("import_1", 0.0) or 0.0))
        imp2 = max(0.0, float(rec.get("import_2", 0.0) or 0.0))
        exp1 = max(0.0, float(rec.get("export_1", 0.0) or 0.0))
        exp2 = max(0.0, float(rec.get("export_2", 0.0) or 0.0))

        # 1. Charge per zone
        ch1 = round(exp1 * coeff, 4)
        ch2 = round(exp2 * coeff, 4)
        bank_1 += ch1
        bank_2 += ch2

        # 2. Grid Return (Fee to operator)
        fee1 = round(exp1 * fee_ratio, 4)
        fee2 = round(exp2 * fee_ratio, 4)

        # 3. Discharge:
        # Zone 1 (day): first from Bank 1, then fallback to Bank 2
        dis1_from_1 = min(imp1, bank_1)
        bank_1 -= dis1_from_1
        rem1 = imp1 - dis1_from_1
        dis1_from_2 = min(rem1, bank_2)
        bank_2 -= dis1_from_2
        net_imp1 = max(0.0, rem1 - dis1_from_2)

        # Zone 2 (night): first from Bank 2, then fallback to Bank 1
        dis2_from_2 = min(imp2, bank_2)
        bank_2 -= dis2_from_2
        rem2 = imp2 - dis2_from_2
        dis2_from_1 = min(rem2, bank_1)
        bank_1 -= dis2_from_1
        net_imp2 = max(0.0, rem2 - dis2_from_1)

        total_dis_l1 = dis1_from_1 + dis2_from_1
        total_dis_l2 = dis1_from_2 + dis2_from_2

        sum_ch1 += ch1
        sum_ch2 += ch2
        sum_dis1 += total_dis_l1
        sum_dis2 += total_dis_l2
        sum_exp1 += fee1
        sum_exp2 += fee2
        sum_imp1 += net_imp1
        sum_imp2 += net_imp2

        s_ch1.append({"dt": dt, "state": ch1, "sum": round(sum_ch1, 4)})
        s_dis1.append({"dt": dt, "state": total_dis_l1, "sum": round(sum_dis1, 4)})
        s_ch2.append({"dt": dt, "state": ch2, "sum": round(sum_ch2, 4)})
        s_dis2.append({"dt": dt, "state": total_dis_l2, "sum": round(sum_dis2, 4)})
        s_exp1.append({"dt": dt, "state": fee1, "sum": round(sum_exp1, 4)})
        s_exp2.append({"dt": dt, "state": fee2, "sum": round(sum_exp2, 4)})
        s_imp1.append({"dt": dt, "state": net_imp1, "sum": round(sum_imp1, 4)})
        s_imp2.append({"dt": dt, "state": net_imp2, "sum": round(sum_imp2, 4)})

    return {
        "series": {
            "magazyn_l1_ladowanie": s_ch1,
            "magazyn_l1_rozladowanie": s_dis1,
            "magazyn_l2_ladowanie": s_ch2,
            "magazyn_l2_rozladowanie": s_dis2,
            "siec_oddanie_strefa_1": s_exp1,
            "siec_oddanie_strefa_2": s_exp2,
            "siec_pobor_strefa_1": s_imp1,
            "siec_pobor_strefa_2": s_imp2,
        },
        "ending_bank_1": round(bank_1, 4),
        "ending_bank_2": round(bank_2, 4),
        "ending_bank": round(bank_1 + bank_2, 4),
    }


async def async_synthesize_storage_from_recorder(
    hass: Any,
    entry: Any,
    meter: dict[str, Any],
) -> bool:
    """Generate synthetic virtual storage & grid statistics from existing recorder statistics.

    Allows zero-API, instantaneous backfill of the 730-day virtual warehouse
    for existing installations upgrading to v1.6.0+ or pressing Configure Button.
    """
    import functools
    import logging
    from datetime import datetime, timedelta, timezone

    _LOGGER = logging.getLogger(__name__)

    try:
        from .const import (
            CONF_BANK_INITIAL_KWH,
            CONF_BANK_INITIAL_KWH_L1,
            CONF_BANK_INITIAL_KWH_L2,
            CONF_ENABLE_SYNTHETIC_STORAGE,
            CONF_PROSUMER_COEFFICIENT,
            DEFAULT_ENABLE_SYNTHETIC_STORAGE,
            DEFAULT_PROSUMER_COEFFICIENT,
        )
        from .settlement import is_export_prosumer
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.models import StatisticMetaData
        from homeassistant.components.recorder.statistics import (
            async_import_statistics,
            statistics_during_period,
        )
    except Exception as err:
        _LOGGER.debug("Required modules for synthetic synthesis unavailable: %s", err)
        return False

    enable_synth = entry.options.get(
        CONF_ENABLE_SYNTHETIC_STORAGE, DEFAULT_ENABLE_SYNTHETIC_STORAGE
    )
    if not enable_synth:
        return False

    try:
        coeff = float(entry.options.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT))
    except (ValueError, TypeError):
        coeff = DEFAULT_PROSUMER_COEFFICIENT

    if coeff < 0.7:
        return False

    meter_point_id = meter["meter_point_id"]
    serial = str(meter.get("meter_serial", meter_point_id)).lower()
    has_zones = meter.get("zone_count", 1) > 1

    if has_zones:
        imp1_id = f"sensor.energa_{serial}_panel_energia_strefa_1"
        imp2_id = f"sensor.energa_{serial}_panel_energia_strefa_2"
        exp1_id = f"sensor.energa_{serial}_panel_energia_produkcja_strefa_1"
        exp2_id = f"sensor.energa_{serial}_panel_energia_produkcja_strefa_2"
        check_id = f"sensor.energa_{serial}_syntetyczny_magazyn_l1_ladowanie"
        wanted_ids = [imp1_id, imp2_id, exp1_id, exp2_id]
    else:
        imp_id = f"sensor.energa_{serial}_panel_energia_zuzycie"
        exp_id = f"sensor.energa_{serial}_panel_energia_produkcja"
        check_id = f"sensor.energa_{serial}_syntetyczny_magazyn_ladowanie"
        wanted_ids = [imp_id, exp_id]

    now = datetime.now(timezone.utc)
    check_start = now - timedelta(days=30)

    try:
        recorder = get_instance(hass)
    except Exception:
        return False

    try:
        existing_check = await recorder.async_add_executor_job(
            functools.partial(
                statistics_during_period,
                hass,
                check_start,
                now,
                [check_id],
                "day",
                None,
                {"sum"},
            )
        )
        if existing_check and existing_check.get(check_id):
            _LOGGER.debug("Synthetic statistics already exist for meter %s, skipping", serial)
            return False
    except Exception as check_err:
        _LOGGER.debug("Error checking existing synthetic statistics for %s: %s", serial, check_err)

    history_start = now - timedelta(days=735)
    try:
        raw_stats = await recorder.async_add_executor_job(
            functools.partial(
                statistics_during_period,
                hass,
                history_start,
                now,
                wanted_ids,
                "hour",
                None,
                {"state"},
            )
        )
    except Exception as fetch_err:
        _LOGGER.warning("Could not fetch recorder statistics for %s: %s", serial, fetch_err)
        return False

    if not raw_stats:
        return False

    data_by_ts: dict[float, dict] = {}
    for sid, rows in raw_stats.items():
        for r in rows:
            ts = r.get("start")
            st = float(r.get("state") or 0.0)
            rec = data_by_ts.setdefault(ts, {})
            if has_zones:
                if sid == imp1_id: rec["import_1"] = st
                elif sid == imp2_id: rec["import_2"] = st
                elif sid == exp1_id: rec["export_1"] = st
                elif sid == exp2_id: rec["export_2"] = st
            else:
                if sid == imp_id: rec["import"] = st
                elif sid == exp_id: rec["export"] = st

    if not data_by_ts:
        return False

    hourly_records = []
    for ts in sorted(data_by_ts.keys()):
        dt = datetime.fromtimestamp(ts, timezone.utc)
        row = data_by_ts[ts]
        if has_zones:
            hourly_records.append({
                "dt": dt,
                "import_1": row.get("import_1", 0.0),
                "import_2": row.get("import_2", 0.0),
                "export_1": row.get("export_1", 0.0),
                "export_2": row.get("export_2", 0.0),
            })
        else:
            hourly_records.append({
                "dt": dt,
                "import": row.get("import", 0.0),
                "export": row.get("export", 0.0),
            })

    has_export = any(
        (r.get("export", 0.0) > 0 or r.get("export_1", 0.0) > 0 or r.get("export_2", 0.0) > 0)
        for r in hourly_records
    )
    if not has_export and not is_export_prosumer(meter):
        return False

    try:
        coeff = float(entry.options.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT))
    except (ValueError, TypeError):
        coeff = DEFAULT_PROSUMER_COEFFICIENT

    if coeff < 0.7:
        return False

    init_b1 = float(entry.options.get(CONF_BANK_INITIAL_KWH_L1, 0.0) or 0.0)
    init_b2 = float(entry.options.get(CONF_BANK_INITIAL_KWH_L2, 0.0) or 0.0)
    if not has_zones and init_b1 == 0.0 and init_b2 == 0.0:
        init_b1 = float(entry.options.get(CONF_BANK_INITIAL_KWH, 0.0) or 0.0)

    synth_res = calculate_synthetic_storage(
        hourly_records=hourly_records,
        coeff=coeff,
        has_zones=has_zones,
        initial_bank_1=init_b1,
        initial_bank_2=init_b2,
        base_sums={},
    )

    names = {
        "magazyn_l1_ladowanie": "Syntetyczny Magazyn L1 Ładowanie",
        "magazyn_l1_rozladowanie": "Syntetyczny Magazyn L1 Rozładowanie",
        "magazyn_l2_ladowanie": "Syntetyczny Magazyn L2 Ładowanie",
        "magazyn_l2_rozladowanie": "Syntetyczny Magazyn L2 Rozładowanie",
        "siec_oddanie_strefa_1": "Syntetyczna Sieć Oddanie Strefa 1",
        "siec_oddanie_strefa_2": "Syntetyczna Sieć Oddanie Strefa 2",
        "siec_pobor_strefa_1": "Syntetyczna Sieć Pobór Strefa 1",
        "siec_pobor_strefa_2": "Syntetyczna Sieć Pobór Strefa 2",
        "magazyn_ladowanie": "Syntetyczny Magazyn Ładowanie",
        "magazyn_rozladowanie": "Syntetyczny Magazyn Rozładowanie",
        "siec_oddanie": "Syntetyczna Sieć Oddanie",
        "siec_pobor": "Syntetyczna Sieć Pobór",
    }

    for k, pts in synth_res.get("series", {}).items():
        prefix = "syntetyczny_" if k.startswith("magazyn") else "syntetyczna_"
        s_eid = f"sensor.energa_{serial}_{prefix}{k}"
        meta = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=names.get(k, s_eid),
            source="recorder",
            statistic_id=s_eid,
            unit_of_measurement="kWh",
        )
        stats = [{"start": p["dt"], "state": p["state"], "sum": p["sum"]} for p in pts]
        async_import_statistics(hass, meta, stats)

    _LOGGER.info(
        "Successfully synthesized %d hourly records of virtual storage for meter %s from recorder",
        len(hourly_records),
        serial,
    )
    return True
