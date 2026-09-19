"""Bill forecast and component sensors for Energa My Meter integration."""

import logging
from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import (
    CONF_BANK_RCE_PRICE,
    CONF_RCE_AUTO_FETCH,
    CONF_TARIFF_CAPACITY,
    DEFAULT_BANK_RCE_PRICE,
    ROLLING_MIN_COVERAGE_DAYS,
    get_price_for_key,
    get_prosumer_coefficient,
)
from ..settlement import (
    is_export_prosumer,
    month_to_date_forecast,
    settlement_system_label,
    settlement_system_name,
)
from ..tariff import (
    capacity_for_annual_use,
    compute_bill,
    fees_from_options,
    mtd_invoice_bases,
    split_cover,
    tariff_family,
)
from .bank import bank_kwh_snapshot

_LOGGER = logging.getLogger(__name__)

class EnergaBillForecastSensor(CoordinatorEntity, SensorEntity):
    """Month-end bill forecast as a full invoice (v0.2.14).

    MTD flows come from recorder statistics (same `_mtd` cache as v0.2.11);
    the full bill (sale + excise + trade fee + distribution + VAT 23% −
    prosumer settlement) is computed with `tariff.compute_bill` and
    linearly extrapolated to month end.

    - New net-billing: deposit = export×RCEm×1.23 lowers the payable.
    - Old net-metering: import covered by the virtual warehouse (up to the
      current Bank kWh, split day/night proportionally) pays no energy
      charge and no variable distribution/quality fee — fixed fees,
      excise and OZE/cogen always stay (as on the invoice).
    - State = forecast payable (do_zapłaty) at month end; MTD bill and the
      legacy energy-only numbers stay in attributes for compatibility.
    Created only when enable_auto_settlement is on (needs history).
    """

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry, has_zones: bool = False, serial: str = "") -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._serial = serial
        self._entry = entry
        self._has_zones = has_zones
        self._attr_name = "Prognoza Rachunku"
        self._attr_unique_id = f"energa_{meter_id}_bill_forecast"
        self._attr_has_entity_name = True
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "PLN"
        # NOTE: no monetary device_class (monetary+measurement rejected
        # by HA, v0.2.12 fix); forecast is a projection, not a meter total.
        self._attr_icon = "mdi:calendar-clock"
        self._attr_device_info = device_info

    def _mtd_dict(self) -> dict:
        """This meter's month-to-date flow cache from the coordinator."""
        store = getattr(self.coordinator, "_mtd", {}) or {}
        return (
            store.get(str(self._meter_id))
            or store.get(str(getattr(self, "_serial", "")))
            or {}
        )

    def _mtd_parts(self):
        """(import_kwh, export_kwh) month-to-date from coordinator cache."""
        mtd = self._mtd_dict()
        imp = mtd.get("import", mtd.get("import_1", 0) + mtd.get("import_2", 0))
        exp = mtd.get("export", mtd.get("export_1", 0) + mtd.get("export_2", 0))
        return float(imp), float(exp)

    def _mtd_bases(self) -> dict:
        """Invoice bases for this meter (hourly-netted salda when available).

        Pure logic lives in :func:`tariff.mtd_invoice_bases` so it stays
        unit-tested without Home Assistant.
        """
        return mtd_invoice_bases(
            self._mtd_dict(), self._has_zones, self._is_old_system()
        )

    def _mtd_zone_flows(self):
        """(import_day, import_night, export_total) MTD per zone.

        v1.9.0: NEW net-billing is settled hour by hour by the seller, so the
        energy + variable-distribution base is the hourly-netted "salda
        dodatnie" and the export credited to the deposit is "salda ujemne"
        (keys written by the coordinator from recorder hourly statistics; see
        docs/FAKTURY_ROZLICZENIA.md). Old net-metering — and any meter whose
        hourly salda are unavailable — keeps gross meter flows.
        """
        b = self._mtd_bases()
        return b["import_day"], b["import_night"], b["export"]

    def _mtd_excise(self):
        """(excise_day, excise_night) MTD = the hourly "nakładka" kWh.

        Net-billing only, and only when hourly salda are known. Excise
        (5 PLN/MWh) is charged on gross import minus salda dodatnie —
        verified on Agrestowa 07-08.2026 (0,35 / 0,31 PLN). Returns None
        when unavailable so callers keep the informational-only path.
        """
        b = self._mtd_bases()
        if b["add_excise"]:
            return (b["excise_day"], b["excise_night"])
        return None

    def _annual_import_estimate(self):
        """Annual grid import (kWh) for the URE capacity-fee bracket.

        Prefers trailing-365-day statistics (needs history + coverage);
        otherwise annualizes lifetime meter totals over the meter age
        (first-data date from entry data). None when unknowable.
        """
        mid = str(self._meter_id)
        try:
            rolling = getattr(self.coordinator, "_rolling_365", {}).get(mid, {})
            cov = int(rolling.get("_coverage_days", 0))
            imp365 = rolling.get(
                "import", rolling.get("import_1", 0) + rolling.get("import_2", 0)
            )
            if cov >= ROLLING_MIN_COVERAGE_DAYS and float(imp365) > 0:
                return float(imp365)
            if cov >= 30 and float(imp365) > 0:
                return round(float(imp365) / cov * 365, 1)
        except (ValueError, TypeError):
            pass
        totals = (getattr(self.coordinator, "_meter_totals", {}) or {}).get(mid)
        if not totals:
            return None
        try:
            lifetime = float(totals.get("import", 0))
        except (ValueError, TypeError):
            return None
        if lifetime <= 0:
            return None
        try:
            from datetime import date as _date

            data = getattr(self._entry, "data", {}) or {}
            first_s = data.get(f"meter_{self._meter_id}_first_data_date") or data.get(
                "first_data_date"
            )
            if not first_s:
                return None
            y, m, d = (int(p) for p in str(first_s).split("-"))
            days = max(30, (_date.today() - _date(y, m, d)).days)
            return round(lifetime / days * 365, 1)
        except (ValueError, TypeError):
            return None

    def _meter_dict(self) -> dict | None:
        """This sensor's meter from the coordinator payload (or ``None``)."""
        for m in getattr(self.coordinator, "data", None) or []:
            if str(m.get("meter_point_id")) == str(self._meter_id):
                return m
        return None

    def _is_consumer(self) -> bool:
        """True for a one-way meter (no export): label it a consumer.

        A plain consumer has coefficient 0.0, which ``_is_old_system`` would
        otherwise read as "new net-billing" — the warzywna defect (2026-09-18).
        """
        meter = self._meter_dict()
        if meter is None:
            return False
        return not is_export_prosumer(meter)

    def _settlement_type(self) -> str:
        """Fine-grained settlement enum (preserves historic prosumer values)."""
        if self._is_consumer():
            return settlement_system_name(False, False)
        return "net_metering" if self._is_old_system() else "net_billing_rcem"

    def _system_label(self) -> str:
        return settlement_system_label(
            not self._is_consumer(), self._is_old_system()
        )

    def _is_old_system(self) -> bool:
        """Old net-metering (coeff >= 0.7) vs new net-billing."""
        coeff = get_prosumer_coefficient(
            self._entry.options, meter_id=str(self._meter_id), serial=str(getattr(self, "_serial", ""))
        )
        return coeff >= 0.7

    def _warehouse_snapshot(self) -> dict:
        """Shared Bank kWh engine snapshot (P1.4)."""
        return bank_kwh_snapshot(
            self.coordinator,
            self._entry,
            self._meter_id,
            serial=str(getattr(self, "_serial", "")),
            has_zones=self._has_zones,
        )

    def _warehouse_cover(self):
        """Current Bank kWh available to cover this month's import.

        v1.9.2 (P1.4): delegates to the Bank sensor engine so FIFO 12m /
        invoice cut-off / rolling 365d and the initial L1/L2 balances are
        honoured, matching the "Bank Wirtualny kWh" entity.
        """
        bank = self._warehouse_snapshot().get("bank_kwh")
        return max(0.0, float(bank)) if bank is not None else 0.0

    def _warehouse_cover_zones(self, imp_day: float, imp_night: float):
        """Per-zone warehouse coverage (L1/L2) for the bill (P1.4).

        When the Bank engine knows both zone balances, each zone covers at
        most its own import — the same ``min(bank_lx, import_lx)`` rule as
        ``core/verification.py`` (verify_period). Otherwise the total is
        split proportionally to the import (legacy behaviour).

        Returns ``(cover_day, cover_night)`` in kWh.
        """
        snapshot = self._warehouse_snapshot()
        bank = snapshot.get("bank_kwh")
        if bank is None:
            return 0.0, 0.0
        bank_1 = snapshot.get("bank_kwh_l1")
        bank_2 = snapshot.get("bank_kwh_l2")
        if bank_1 is not None and bank_2 is not None:
            return (
                max(0.0, min(float(bank_1), float(imp_day))),
                max(0.0, min(float(bank_2), float(imp_night))),
            )
        return split_cover(max(0.0, float(bank)), imp_day, imp_night)

    def _rce(self) -> float:
        opts = self._entry.options
        coord_rce = getattr(self.coordinator, "_rce_cache", None)
        if opts.get(CONF_RCE_AUTO_FETCH) and coord_rce is not None:
            return float(coord_rce)
        return float(opts.get(CONF_BANK_RCE_PRICE, DEFAULT_BANK_RCE_PRICE))

    def _meter_tariff(self):
        """Tariff string of this meter (G11 vs G12W fee table, v0.3.0)."""
        for m in self.coordinator.data or []:
            if str(m.get("meter_point_id")) == str(self._meter_id):
                return m.get("tariff")
        return None

    @property
    def native_value(self):
        from datetime import date as _date

        mtd = (
            getattr(self.coordinator, "_mtd", {}).get(str(self._meter_id))
            or getattr(self.coordinator, "_mtd", {}).get(str(getattr(self, "_serial", "")))
        )
        if not mtd:
            return None
        imp_mtd, exp_mtd = self._mtd_parts()
        imp_d, imp_n, exp_tot = self._mtd_zone_flows()
        excise = self._mtd_excise()
        excise_d, excise_n = excise if excise else (0.0, 0.0)
        opts = self._entry.options
        mid = str(self._meter_id)
        ser = str(getattr(self, "_serial", ""))
        if self._has_zones:
            m1 = mtd.get("import_1", 0)
            m2 = mtd.get("import_2", 0)
            p1 = get_price_for_key(opts, "import_1", meter_id=mid, serial=ser)
            p2 = get_price_for_key(opts, "import_2", meter_id=mid, serial=ser)
            imp_cost = m1 * p1 + m2 * p2
        else:
            imp_cost = imp_mtd * get_price_for_key(opts, "import", meter_id=mid, serial=ser)
        rce = self._rce()
        mtd_net = exp_mtd * rce * 1.23 - imp_cost
        today = _date.today()
        import calendar as _cal

        forecast = month_to_date_forecast(
            mtd_net, today.day, _cal.monthrange(today.year, today.month)[1]
        )

        # === v0.2.14 full-bill math (invoice reconstruction) ===
        # v0.3.0: fee table follows the meter tariff (G11 invoice table
        # for single-zone meters, G12W otherwise).
        # v1.9.2 (P1.3): pass the settlement system so the seller product
        # (Oferta Podstawowa vs taryfa urzędowa) is inferred exactly like
        # verify_period — the forecast must use the same rate table.
        old_system = self._is_old_system()
        fees = fees_from_options(
            opts, self._meter_tariff(), old_system=old_system
        )
        # v0.2.18: capacity fee auto-bracket (URE 2026) unless overridden.
        capacity_source = "manual (Options tariff_capacity)"
        if CONF_TARIFF_CAPACITY not in (opts or {}):
            annual = self._annual_import_estimate()
            if annual is not None:
                fees["capacity"] = capacity_for_annual_use(annual)
                capacity_source = (
                    f"auto URE 2026 (roczny pobór ~{annual:.0f} kWh)"
                )
        if old_system:
            cover_d, cover_n = self._warehouse_cover_zones(imp_d, imp_n)
            # No PLN deposit in the old system — coverage only.
            # (None would auto-compute export×RCEm×1.23.)
            deposit_mtd = 0.0
        else:
            cover_d, cover_n = 0.0, 0.0
            deposit_mtd = None  # computed inside compute_bill
        try:
            bill_mtd = compute_bill(
                imp_d, imp_n, exp_tot, rce, fees,
                cover_day=cover_d, cover_night=cover_n,
                deposit_pln=deposit_mtd,
                excise_day=excise_d, excise_night=excise_n,
                add_excise=excise is not None,
            )
        except (ValueError, TypeError):
            bill_mtd = None
        # Pre-computed hourly profile forecast from coordinator's executor thread (v1.6.9)
        cache = getattr(self.coordinator, "_profile_forecast_cache", None)
        profile_res = cache.get(str(self._meter_id)) if isinstance(cache, dict) else None
        if profile_res is None:
            storage = getattr(self.coordinator, "storage", None)
            if storage:
                try:
                    from ..projections.forecast import HourlyProfileForecaster
                    canonical_readings = storage.get_readings(
                        ppe_id=str(self._meter_id),
                        resolution="1h",
                    )
                    if canonical_readings:
                        forecaster = HourlyProfileForecaster(
                            readings=canonical_readings,
                            tariff_code=self._meter_tariff(),
                        )
                        if forecaster.history_days_count >= 7:
                            month_start_utc = datetime(today.year, today.month, 1, 0, 0, tzinfo=timezone.utc)
                            mtd_readings = [
                                r for r in canonical_readings
                                if r.interval_start_utc >= month_start_utc
                            ]
                            profile_res = forecaster.forecast_month(
                                current_date=today,
                                mtd_readings=mtd_readings,
                                tariff_options=opts,
                                rce_price=rce,
                                warehouse_kwh=self._warehouse_cover() if old_system else 0.0,
                                is_old_system=old_system,
                            )
                except Exception as pf_err:
                    _LOGGER.debug("Profile forecaster fallback failed: %s", pf_err)

        days_in_month = _cal.monthrange(today.year, today.month)[1]
        elapsed = min(max(today.day, 1), days_in_month)

        if profile_res and profile_res.method == "hourly_profile_wal":
            f_imp_d = float(profile_res.forecast_import_t1_kwh)
            f_imp_n = float(profile_res.forecast_import_t2_kwh)
            f_exp = float(profile_res.forecast_export_total_kwh)
            forecast_method = (
                f"hourly_profile_wal ({profile_res.history_days_count} dni historii, "
                f"trend: {profile_res.trend_factor:.2f})"
            )
        else:
            # Early-month volatility smoothing (day 1-6): blend MTD with trailing history
            rolling = getattr(self.coordinator, "_rolling_365", {}).get(str(self._meter_id), {})
            cov = int(rolling.get("_coverage_days", 0)) if rolling else 0

            if elapsed < 7 and cov >= 14:
                w_mtd = elapsed / 7.0
                w_hist = 1.0 - w_mtd

                t_imp_d = float(rolling.get("import_1" if self._has_zones else "import", 0)) / cov
                t_imp_n = float(rolling.get("import_2", 0)) / cov if self._has_zones else 0.0
                t_exp = (
                    (float(rolling.get("export_1", 0)) + float(rolling.get("export_2", 0))) / cov
                    if self._has_zones
                    else float(rolling.get("export", 0)) / cov
                )

                m_imp_d = imp_d / elapsed
                m_imp_n = imp_n / elapsed
                m_exp = exp_tot / elapsed

                f_imp_d = (w_mtd * m_imp_d + w_hist * t_imp_d) * days_in_month
                f_imp_n = (w_mtd * m_imp_n + w_hist * t_imp_n) * days_in_month
                f_exp = (w_mtd * m_exp + w_hist * t_exp) * days_in_month
                forecast_method = f"smoothed_blend_7d (dzień {elapsed}/7, {w_hist*100:.0f}% historia)"
            else:
                factor = days_in_month / elapsed
                f_imp_d, f_imp_n, f_exp = imp_d * factor, imp_n * factor, exp_tot * factor
                forecast_method = "linear_mtd"


        if old_system:
            f_cover_d, f_cover_n = self._warehouse_cover_zones(f_imp_d, f_imp_n)
            f_deposit = 0.0
        else:
            f_cover_d, f_cover_n = 0.0, 0.0
            f_deposit = None
        try:
            bill_fc = compute_bill(
                f_imp_d, f_imp_n, f_exp, rce, fees,
                cover_day=f_cover_d, cover_night=f_cover_n,
                deposit_pln=f_deposit,
                excise_day=excise_d * days_in_month / elapsed,
                excise_night=excise_n * days_in_month / elapsed,
                add_excise=excise is not None,
            )
        except (ValueError, TypeError):
            bill_fc = None

        consumer = self._is_consumer()
        attrs = {
            "mtd_import_kwh": round(imp_mtd, 2),
            "mtd_export_kwh": round(exp_mtd, 2),
            "mtd_net_pln": round(mtd_net, 2),
            "forecast_pln": forecast,
            "forecast_method": forecast_method,
            "day_of_month": today.day,
            "rce_price": rce,
            "rce_source": getattr(self.coordinator, "_rce_source", None) or "manual",
            "formula": "mtd_net/days_elapsed*days_in_month; mtd_net=export×RCE×1.23-import×cena",
            "rule_version": "ustawa_oze_art4_ust11_v1",
            "system": self._system_label(),
            "settlement_type": self._settlement_type(),
            "period": f"{today.year}-{today.month:02d}",
            "calculated_at": datetime.now(timezone.utc).isoformat(),
        }
        if not consumer:
            attrs["note"] = (
                "Depozyt pokrywa tylko energię czynną (bez dystrybucji i opłat stałych)"
            )
        self._attr_extra_state_attributes = attrs
        if profile_res and profile_res.method == "hourly_profile_wal":
            self._attr_extra_state_attributes.update({
                "profile_confidence": profile_res.confidence_score,
                "profile_history_days": profile_res.history_days_count,
                "profile_trend_factor": profile_res.trend_factor,
                "forecast_import_t1_kwh": float(profile_res.forecast_import_t1_kwh),
                "forecast_import_t2_kwh": float(profile_res.forecast_import_t2_kwh),
                "forecast_export_t1_kwh": float(profile_res.forecast_export_t1_kwh),
                "forecast_export_t2_kwh": float(profile_res.forecast_export_t2_kwh),
                # v1.9.2 (P0.1): the WAL path's own invoice. Before the fix
                # this was always None (swallowed TypeError); exposing it makes
                # the forecast auditable alongside the linear fallback.
                "forecast_payable_pln": (
                    float(profile_res.forecast_payable_pln)
                    if profile_res.forecast_payable_pln is not None
                    else None
                ),
                "bill_breakdown": profile_res.bill_breakdown,
            })
        if bill_mtd is not None and bill_fc is not None:

            self._attr_extra_state_attributes.update({
                "system": self._system_label(),
                "mtd_import_day_kwh": round(imp_d, 2),
                "mtd_import_night_kwh": round(imp_n, 2),
                "mtd_sale_total_pln": bill_mtd["sale_total"],
                "mtd_distr_total_pln": bill_mtd["distr_total"],
                "mtd_sale_gross_pln": bill_mtd["sale_gross"],
                "mtd_distr_gross_pln": bill_mtd["distr_gross"],
                "mtd_netto_pln": bill_mtd["netto"],
                "mtd_vat_pln": bill_mtd["vat"],
                "mtd_brutto_pln": bill_mtd["brutto"],
                "mtd_deposit_pln": bill_mtd["deposit"],
                "mtd_deposit_applied_pln": bill_mtd["deposit_applied"],
                "mtd_do_zaplaty_pln": bill_mtd["do_zaplaty"],
                "forecast_brutto_pln": bill_fc["brutto"],
                "forecast_sale_gross_pln": bill_fc["sale_gross"],
                "forecast_distr_gross_pln": bill_fc["distr_gross"],
                "forecast_deposit_applied_pln": bill_fc["deposit_applied"],
                "forecast_do_zaplaty_pln": bill_fc["do_zaplaty"],

                "cover_day_kwh": cover_d,
                "cover_night_kwh": cover_n,
                "capacity_source": capacity_source,
                "fee_table": tariff_family(self._meter_tariff()),
                "fee_note": "Stawki z Options (taryfa) lub domyślne z faktur (G11 bez PV, G12W 07 i 05-06.2026); "
                "mocowa/abonament stałe z faktury — sprawdź z taryfą OSD",
            })
            if not consumer:
                self._attr_extra_state_attributes["hourly_netting_note"] = (
                    "Licznik: delty dobowe; sprzedawca bilansuje godzinowo "
                    "— przybliżenie ~1% (kWh) / ~13% (depozyt PLN)"
                )
            return bill_fc["do_zaplaty"]
        return forecast

    @property
    def available(self) -> bool:
        coords_mtd = getattr(self.coordinator, "_mtd", {})
        return (
            self.coordinator.data is not None
            and (
                str(self._meter_id) in coords_mtd
                or str(getattr(self, "_serial", "")) in coords_mtd
            )
        )


class EnergaBillCurrentSensor(EnergaBillForecastSensor):
    """Month-to-date actual bill so far (v1.0.3).

    Calculates the exact bill to pay from day 1 of the month until today
    based on actual consumption, distribution fees, and prosumer settlement
    (deducting deposit for energy purchase in net-billing, or warehouse coverage
    in net-metering).
    """

    def __init__(
        self,
        coordinator,
        meter_id: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
        has_zones: bool = False,
        serial: str = "",
    ) -> None:
        super().__init__(
            coordinator,
            meter_id=meter_id,
            device_info=device_info,
            entry=entry,
            has_zones=has_zones,
            serial=serial,
        )
        self._attr_name = "Dotychczasowy Rachunek"
        self._attr_unique_id = f"energa_{meter_id}_bill_current"
        self._attr_icon = "mdi:cash-clock"

    def _calculate_bill_mtd(self):
        from datetime import date as _date

        mtd = (
            getattr(self.coordinator, "_mtd", {}).get(str(self._meter_id))
            or getattr(self.coordinator, "_mtd", {}).get(str(getattr(self, "_serial", "")))
        )
        if not mtd:
            return None, {}
        imp_mtd, exp_mtd = self._mtd_parts()
        bases = self._mtd_bases()
        imp_d, imp_n, exp_tot = (
            bases["import_day"],
            bases["import_night"],
            bases["export"],
        )
        excise = self._mtd_excise()
        excise_d, excise_n = excise if excise else (0.0, 0.0)
        # per-zone credited export (salda ujemne for net-billing)
        exp_d, exp_n = bases["export_day"], bases["export_night"]
        opts = self._entry.options
        rce = self._rce()
        today = _date.today()

        # v1.9.2 (P1.3): same product/rate inference as verify_period.
        old_system = self._is_old_system()
        fees = fees_from_options(
            opts, self._meter_tariff(), old_system=old_system
        )
        capacity_source = "manual (Options tariff_capacity)"
        if CONF_TARIFF_CAPACITY not in (opts or {}):
            annual = self._annual_import_estimate()
            if annual is not None:
                fees["capacity"] = capacity_for_annual_use(annual)
                capacity_source = f"auto URE 2026 (roczny pobór ~{annual:.0f} kWh)"

        if old_system:
            cover_d, cover_n = self._warehouse_cover_zones(imp_d, imp_n)
            deposit_mtd = 0.0
        else:
            cover_d, cover_n = 0.0, 0.0
            deposit_mtd = None
        try:
            bill_mtd = compute_bill(
                imp_d, imp_n, exp_tot, rce, fees,
                cover_day=cover_d, cover_night=cover_n,
                deposit_pln=deposit_mtd,
                excise_day=excise_d, excise_night=excise_n,
                add_excise=excise is not None,
            )
        except (ValueError, TypeError):
            bill_mtd = None

        if bill_mtd is None:
            return None, {}

        attrs = {
            "period": f"{today.year}-{today.month:02d}",
            "day_of_month": today.day,
            "calculated_at": datetime.now(timezone.utc).isoformat(),
            "system": self._system_label(),
            "settlement_type": self._settlement_type(),
            "mtd_import_kwh": round(imp_mtd, 2),
            "mtd_export_kwh": round(exp_mtd, 2),
            "mtd_import_day_kwh": round(imp_d, 2),
            "mtd_import_night_kwh": round(imp_n, 2),
            "mtd_export_day_kwh": round(exp_d, 2),
            "mtd_export_night_kwh": round(exp_n, 2),
            "mtd_sale_total_pln": bill_mtd.get("sale_total"),
            "mtd_distr_total_pln": bill_mtd.get("distr_total"),
            "mtd_sale_gross_pln": bill_mtd.get("sale_gross", round((bill_mtd.get("sale_total") or 0.0) * 1.23, 2)),
            "mtd_distr_gross_pln": bill_mtd.get("distr_gross", round((bill_mtd.get("distr_total") or 0.0) * 1.23, 2)),
            "mtd_netto_pln": bill_mtd.get("netto"),
            "mtd_vat_pln": bill_mtd.get("vat"),
            "mtd_brutto_pln": bill_mtd.get("brutto"),
            "mtd_deposit_pln": bill_mtd.get("deposit"),
            "mtd_deposit_applied_pln": bill_mtd.get("deposit_applied"),
            "mtd_do_zaplaty_pln": bill_mtd.get("do_zaplaty"),
            "cover_day_kwh": cover_d,
            "cover_night_kwh": cover_n,
            "capacity_source": capacity_source,
            "rce_price": rce,
            "fee_table": tariff_family(self._meter_tariff()),
            "unit_of_measurement": "PLN",
        }
        return bill_mtd, attrs

    @property
    def native_value(self):
        bill_mtd, attrs = self._calculate_bill_mtd()
        if bill_mtd is None:
            return None
        self._attr_extra_state_attributes = attrs
        return bill_mtd["do_zaplaty"]


class EnergaBillComponentSensor(EnergaBillCurrentSensor):
    """Dedicated breakdown sensor for MTD bill components (v1.0.4).

    Exposes individual metrics (gross cost, energy cost, distribution cost,
    deposit generated, deposit applied, warehouse coverage, MTD energy volumes) as native entities.
    """

    def __init__(
        self,
        coordinator,
        meter_id: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
        component_key: str,
        name: str,
        icon: str,
        unit: str = "PLN",
        device_class: SensorDeviceClass | None = SensorDeviceClass.MONETARY,
        has_zones: bool = False,
        serial: str = "",
    ) -> None:
        super().__init__(
            coordinator,
            meter_id=meter_id,
            device_info=device_info,
            entry=entry,
            has_zones=has_zones,
            serial=serial,
        )
        self._component_key = component_key
        self._attr_name = name
        self._attr_unique_id = f"energa_{meter_id}_mtd_{component_key}"
        self._attr_has_entity_name = True
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        # MTD components are period breakdown metrics, not accumulative meters.
        # State class MUST be None to prevent HA recorder from logging false reset spikes.
        self._attr_state_class = None

    @property
    def native_value(self):
        bill_mtd, attrs = self._calculate_bill_mtd()
        if bill_mtd is None:
            return None
        self._attr_extra_state_attributes = {
            "period": attrs.get("period"),
            "calculated_at": attrs.get("calculated_at"),
            "system": attrs.get("system"),
        }
        if self._component_key == "sale_total":
            self._attr_extra_state_attributes.update({
                "netto_pln": attrs.get("mtd_sale_total_pln"),
                "gross_pln": attrs.get("mtd_sale_gross_pln"),
                "vat_rate": "23%",
                "tax_included": True,
            })
        elif self._component_key == "distr_total":
            self._attr_extra_state_attributes.update({
                "netto_pln": attrs.get("mtd_distr_total_pln"),
                "gross_pln": attrs.get("mtd_distr_gross_pln"),
                "vat_rate": "23%",
                "tax_included": True,
            })
        elif self._component_key == "brutto":
            self._attr_extra_state_attributes.update({
                "netto_pln": attrs.get("mtd_netto_pln"),
                "vat_pln": attrs.get("mtd_vat_pln"),
                "vat_rate": "23%",
                "tax_included": True,
            })
        if self._component_key == "deposit_applied":
            val = attrs.get("mtd_deposit_applied_pln")
            if val is not None:
                try:
                    num = float(val)
                    self._attr_extra_state_attributes["deposit_applied_positive_pln"] = round(num, 2)
                    self._attr_extra_state_attributes["is_deduction"] = True
                    return -round(abs(num), 2) if num > 0 else 0.0
                except (ValueError, TypeError):
                    return 0.0
            return None
        key_map = {
            "brutto": attrs.get("mtd_brutto_pln"),
            "sale_total": attrs.get("mtd_sale_gross_pln"),
            "distr_total": attrs.get("mtd_distr_gross_pln"),
            "deposit": attrs.get("mtd_deposit_pln"),
            "deposit_applied": attrs.get("mtd_deposit_applied_pln"),
            "cover_day": attrs.get("cover_day_kwh"),
            "cover_night": attrs.get("cover_night_kwh"),
            "energy_import": attrs.get("mtd_import_kwh"),
            "energy_export": attrs.get("mtd_export_kwh"),
            "energy_import_1": attrs.get("mtd_import_day_kwh"),
            "energy_import_2": attrs.get("mtd_import_night_kwh"),
            "energy_export_1": attrs.get("mtd_export_day_kwh"),
            "energy_export_2": attrs.get("mtd_export_night_kwh"),
        }
        return key_map.get(self._component_key)


