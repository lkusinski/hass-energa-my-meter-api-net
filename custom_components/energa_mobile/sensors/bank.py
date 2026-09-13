"""Bank and virtual storage sensors for Energa My Meter integration."""

import logging
from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import (
    CONF_BANK_RCE_PRICE,
    CONF_ENABLE_AUTO_SETTLEMENT,
    CONF_PROSUMER_COEFFICIENT,
    CONF_RCE_AUTO_FETCH,
    CONF_SETTLEMENT_DATE,
    CONF_USE_ROLLING_365D,
    DEFAULT_BALANCE_BASELINE,
    DEFAULT_BANK_INITIAL_KWH,
    DEFAULT_BANK_INITIAL_KWH_L1,
    DEFAULT_BANK_INITIAL_KWH_L2,
    DEFAULT_BANK_INITIAL_PLN,
    DEFAULT_BANK_RCE_PRICE,
    DEFAULT_ENABLE_AUTO_SETTLEMENT,
    DEFAULT_PROSUMER_COEFFICIENT,
    DEFAULT_SETTLEMENT_DATE,
    DEFAULT_USE_ROLLING_365D,
    FIFO_MIN_COVERAGE_MONTHS,
    ROLLING_MIN_COVERAGE_DAYS,
    get_meter_baseline,
    get_meter_initial_bank,
    get_price_for_key,
    get_prosumer_coefficient,
)
from ..settlement import (
    FlowAccumulator,
    bank_from_invoice_date,
    days_to_settlement,
    deposit_valid_until,
    fifo_dual_zone_kwh_bank,
    fifo_kwh_bank,
    next_settlement_date,
    parse_settlement_date,
    rolling_kwh_bank,
    trailing_months,
    warehouse_level_pct,
)

_LOGGER = logging.getLogger(__name__)

def _fifo_bank_from_monthly(monthly: dict, coeff: float, has_zones: bool = False):
    """Shared FIFO-12m bank math (v0.3.0, dual-zone support v1.4.0).

    Args:
        monthly: coordinator._monthly[meter_id] = {(y, m): {suffix: kWh}}.
        coeff: prosumer coefficient.
        has_zones: whether the meter has multi-zone tariff.

    Returns (bank_kwh, detail) when >= FIFO_MIN_COVERAGE_MONTHS of flows exist,
    else (None, None). Same rule as the Bank sensor so the Level (%)
    sensor never disagrees with it.
    """
    from datetime import date as _date

    if not monthly:
        return (None, None)
    flows = []
    flows_1 = []
    flows_2 = []
    has_any_zone_data = False
    for (fy, fm) in trailing_months(_date.today(), 13):
        d = monthly.get((fy, fm), {})
        try:
            exp1 = float(d.get("export_1", 0.0))
            exp2 = float(d.get("export_2", 0.0))
            imp1 = float(d.get("import_1", 0.0))
            imp2 = float(d.get("import_2", 0.0))
            if exp1 > 0 or exp2 > 0 or imp1 > 0 or imp2 > 0:
                has_any_zone_data = True
            exp = float(d.get("export", exp1 + exp2))
            imp = float(d.get("import", imp1 + imp2))
        except (ValueError, TypeError):
            exp1, exp2, imp1, imp2 = 0.0, 0.0, 0.0, 0.0
            exp, imp = 0.0, 0.0
        flows.append((fy, fm, imp, exp))
        flows_1.append((fy, fm, imp1, exp1))
        flows_2.append((fy, fm, imp2, exp2))
    if sum(1 for (_, _, i, e) in flows if i > 0 or e > 0) < FIFO_MIN_COVERAGE_MONTHS:
        return (None, None)
    if has_zones or has_any_zone_data:
        return fifo_dual_zone_kwh_bank(flows_1, flows_2, coeff, today=_date.today())
    return fifo_kwh_bank(flows, coeff)




class EnergaBankKwhSensor(CoordinatorEntity, SensorEntity):
    """Virtual storage in kWh for old prosumer (net-metering, per strefa G12W).

    For old system (coefficient 0.8/0.7): bank = max(0, Bilans) + initial_kwh.
    Bilans = (export - baseline_export) * coefficient - (import - baseline_import).
    Uses per-zone meter totals if G12W. 1.23 NOT applied (old system).
    """

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry, has_zones: bool = False, serial: str = "") -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._serial = serial
        self._entry = entry
        self._has_zones = has_zones
        self._attr_name = "Bank Wirtualny kWh"
        self._attr_unique_id = f"energa_{meter_id}_bank_kwh"
        self._attr_has_entity_name = True
        self._attr_state_class = None
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:battery-charging"
        self._attr_device_info = device_info

    @property
    def native_value(self):
        totals = self.coordinator._meter_totals.get(str(self._meter_id))
        if not totals:
            return None

        opts = self._entry.options
        mid = str(self._meter_id)
        ser = str(getattr(self, "_serial", ""))

        # Get baselines
        bi = get_meter_baseline(opts, "import", meter_id=mid, serial=ser, default=DEFAULT_BALANCE_BASELINE)
        be = get_meter_baseline(opts, "export", meter_id=mid, serial=ser, default=DEFAULT_BALANCE_BASELINE)

        coeff = get_prosumer_coefficient(opts, meter_id=mid, serial=ser)
        initial = get_meter_initial_bank(opts, "kwh", meter_id=mid, serial=ser, default=DEFAULT_BANK_INITIAL_KWH)
        bank_1 = None
        bank_2 = None
        init_l1 = 0.0
        init_l2 = 0.0

        if self._has_zones:
            # Per-zone baselines if available, else global
            bi1 = get_meter_baseline(opts, "import_1", meter_id=mid, serial=ser, default=bi)
            bi2 = get_meter_baseline(opts, "import_2", meter_id=mid, serial=ser, default=bi)
            be1 = get_meter_baseline(opts, "export_1", meter_id=mid, serial=ser, default=be)
            be2 = get_meter_baseline(opts, "export_2", meter_id=mid, serial=ser, default=be)

            imp1 = float(totals.get("import_1", totals.get("import", 0)))
            imp2 = float(totals.get("import_2", 0))
            exp1 = float(totals.get("export_1", totals.get("export", 0)))
            exp2 = float(totals.get("export_2", 0))

            init_l1 = get_meter_initial_bank(opts, "kwh_l1", meter_id=mid, serial=ser, default=DEFAULT_BANK_INITIAL_KWH_L1)
            init_l2 = get_meter_initial_bank(opts, "kwh_l2", meter_id=mid, serial=ser, default=DEFAULT_BANK_INITIAL_KWH_L2)
            if init_l1 > 0 or init_l2 > 0:
                initial = round(init_l1 + init_l2, 2)

            # If per-zone baselines not set, use total baselines with total import/export
            if bi1 == bi and bi2 == bi:
                # No per-zone baseline — use total import/export minus global baseline
                net_imp = float(totals.get("import", 0)) - bi
                net_exp = float(totals.get("export", 0)) - be
            else:
                net_imp = (imp1 - bi1) + (imp2 - bi2)
                net_exp = (exp1 - be1) + (exp2 - be2)

            # Per-zone net flows & bank for L1 and L2
            net_imp1 = imp1 - bi1
            net_exp1 = exp1 - be1
            bilans1 = (net_exp1 * coeff) - net_imp1
            bank_1 = round(max(0.0, bilans1) + init_l1, 2)

            net_imp2 = imp2 - bi2
            net_exp2 = exp2 - be2
            bilans2 = (net_exp2 * coeff) - net_imp2
            bank_2 = round(max(0.0, bilans2) + init_l2, 2)
            bank = round(bank_1 + bank_2, 2)
            bilans = round(bilans1 + bilans2, 2)
        else:
            net_imp = float(totals.get("import", 0)) - bi
            net_exp = float(totals.get("export", 0)) - be
            bilans = (net_exp * coeff) - net_imp
            bank = max(0, bilans) + initial
        mode = "baseline"
        source_desc = "net-metering 0.8 roczny (old) — faktury FES"
        formula_desc = "max(0, (export-baseline)*coeff - (import-baseline)) + initial"

        _LOGGER.debug(
            "BankKwh %s: imp=%.2f exp=%.2f coeff=%.2f bilans=%.2f initial=%.2f bank=%.2f",
            mid, net_imp, net_exp, coeff, bilans, initial, bank,
        )

        monthly = getattr(self.coordinator, "_monthly", {}).get(str(mid), {})
        settle_str = str(opts.get(CONF_SETTLEMENT_DATE, DEFAULT_SETTLEMENT_DATE)).strip()
        fifo_detail = None
        inv_detail = None

        # v1.5.0 Priority 1: Invoice cut-off date mode
        if settle_str and monthly and (initial > 0 or init_l1 > 0 or init_l2 > 0):
            inv_bank, inv_detail = bank_from_invoice_date(
                settle_str, monthly, init_1=init_l1 or initial, init_2=init_l2, coeff=coeff
            )
            if inv_bank is not None and inv_detail:
                bank = inv_bank
                bank_1 = inv_detail.get("bank_kwh_l1")
                bank_2 = inv_detail.get("bank_kwh_l2")
                mode = "invoice_date"
                source_desc = f"rozliczenie od daty faktury {settle_str}"
                formula_desc = f"stan_faktury({settle_str}) + net_export_od_faktury*coeff - net_import_od_faktury"
                net_imp = inv_detail.get("net_import_kwh", 0.0)
                net_exp = inv_detail.get("net_export_kwh", 0.0)
                bilans = inv_detail.get("bilans_kwh", 0.0)

        # v1.5.0 Priority 2: Automatic FIFO mode from API (Zero config out-of-the-box!)
        elif bi == 0.0 and be == 0.0 and initial == 0.0 and (not init_l1 and not init_l2):
            if monthly:
                try:
                    coeff_f = float(opts.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT))
                except (ValueError, TypeError):
                    coeff_f = DEFAULT_PROSUMER_COEFFICIENT
                _fifo_bank, fifo_detail = _fifo_bank_from_monthly(monthly, coeff_f, has_zones=self._has_zones)
                if _fifo_bank is not None and fifo_detail is not None:
                    bank = _fifo_bank
                    mode = "fifo_12m_api"
                    source_desc = "automatyczne rozliczenie FIFO 12m z API Energa"
                    formula_desc = "FIFO 12 m-cy wg art. 4 ust. 11 ustawy o OZE"
                    if "bank_kwh_l1" in fifo_detail:
                        bank_1 = fifo_detail["bank_kwh_l1"]
                    if "bank_kwh_l2" in fifo_detail:
                        bank_2 = fifo_detail["bank_kwh_l2"]
                    _LOGGER.debug(
                        "BankKwh %s fifo: bank=%.2f (L1=%s, L2=%s) expired=%.2f uncovered=%.2f",
                        mid, bank, bank_1, bank_2,
                        fifo_detail.get("expired_kwh", 0),
                        fifo_detail.get("uncovered_kwh", 0),
                    )

        # v0.2.11 Priority 3: Rolling 365d if enabled and baseline mode
        elif opts.get(CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT) and opts.get(
            CONF_USE_ROLLING_365D, DEFAULT_USE_ROLLING_365D
        ):
            rolling = getattr(self.coordinator, "_rolling_365", {}).get(str(mid), {})
            coverage = int(rolling.get("_coverage_days", 0))
            if coverage >= ROLLING_MIN_COVERAGE_DAYS:
                exp365 = rolling.get(
                    "export",
                    rolling.get("export_1", 0) + rolling.get("export_2", 0),
                )
                imp365 = rolling.get(
                    "import",
                    rolling.get("import_1", 0) + rolling.get("import_2", 0),
                )
                bank = rolling_kwh_bank(exp365, imp365, coeff)
                mode = "rolling_365d"

        # Build rich attributes for Lovelace visibility
        attrs = {
            "net_import_kwh": round(net_imp, 2),
            "net_export_kwh": round(net_exp, 2),
            "coefficient": coeff,
            "bilans_kwh": round(bilans, 2),
            "initial_kwh": initial,
            "source": source_desc,
            "formula": formula_desc,
            "unit": "kWh — ile energii możesz jeszcze odebrać za darmo",
            "settlement_mode": mode,
            "rule_version": "net_metering_fifo_12m_v1",
            "settlement_type": "net_metering",
            "calculated_at": datetime.now(timezone.utc).isoformat(),
        }
        if mode == "invoice_date" and inv_detail:
            attrs["invoice_date"] = settle_str
            attrs["months_since_invoice"] = inv_detail.get("months_since_invoice", 0)
        elif mode in ("fifo_12m", "fifo_12m_api") and fifo_detail:
            attrs["fifo_expired_kwh"] = fifo_detail.get("expired_kwh")
            attrs["fifo_uncovered_kwh"] = fifo_detail.get("uncovered_kwh")
            attrs["fifo_deposits_kwh"] = fifo_detail.get("deposits_kwh")
            attrs["fifo_note"] = (
                "Magazyn odtworzony z miesięcznych przepływów (FIFO 12 m-cy, "
                "bez przepisywania z faktury). Wymaga historii min. 3 mies."
            )
        if mode == "rolling_365d":
            attrs["coverage_days"] = coverage
        if mode == "fifo_12m" and fifo_detail:
            attrs["fifo_months"] = fifo_detail.get("months_used")
            attrs["fifo_expired_kwh"] = fifo_detail.get("expired_kwh")
            attrs["fifo_uncovered_kwh"] = fifo_detail.get("uncovered_kwh")
            attrs["fifo_deposits_kwh"] = fifo_detail.get("deposits_kwh")
            attrs["fifo_note"] = (
                "Magazyn odtworzony z miesięcznych przepływów (FIFO 12 m-cy, "
                "bez przepisywania z faktury). Wymaga historii min. 3 mies."
            )
        if opts.get(CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT):
            settle_str = opts.get(CONF_SETTLEMENT_DATE, DEFAULT_SETTLEMENT_DATE)
            base = parse_settlement_date(settle_str)
            if base is not None:
                from datetime import date as _date

                attrs["settlement_next"] = next_settlement_date(base, _date.today()).isoformat()
                attrs["days_to_settlement"] = days_to_settlement(settle_str)
            attrs["validity_note"] = (
                "FIFO 12 m-cy od końca miesiąca wprowadzenia, najstarsza energia "
                "najpierw (energa.pl net-metering). Reset 1.01 NIE obowiązuje."
            )
        if self._has_zones:
            attrs.update({
                "import_1": round(float(totals.get("import_1", 0)), 2),
                "import_2": round(float(totals.get("import_2", 0)), 2),
                "export_1": round(float(totals.get("export_1", 0)), 2),
                "export_2": round(float(totals.get("export_2", 0)), 2),
                "per_strefa_note": "L1 droga / L2 tania — bank łączny, per-strefa w atrybutach",
            })
            if bank_1 is not None:
                attrs["bank_kwh_l1"] = round(bank_1, 2)
            if bank_2 is not None:
                attrs["bank_kwh_l2"] = round(bank_2, 2)
            if bank and bank > 0:
                if bank_1 is not None:
                    attrs["bank_l1_share_pct"] = round((bank_1 / bank) * 100.0, 1)
                if bank_2 is not None:
                    attrs["bank_l2_share_pct"] = round((bank_2 / bank) * 100.0, 1)
        self._attr_extra_state_attributes = attrs

        return round(bank, 2)


class EnergaBankZoneSensor(CoordinatorEntity, SensorEntity):
    """Sub-sensor for zone-specific virtual warehouse (L1 or L2) for G12/G12w."""

    def __init__(
        self,
        coordinator,
        meter_id: str,
        device_info: DeviceInfo,
        entry: ConfigEntry,
        zone: int,
        serial: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._serial = serial
        self._entry = entry
        self._zone = zone
        zone_label = "L1 (Dzień)" if zone == 1 else "L2 (Noc)"
        self._attr_name = f"Bank Wirtualny {zone_label} kWh"
        self._attr_unique_id = f"energa_{meter_id}_bank_kwh_l{zone}"
        self._attr_has_entity_name = True
        self._attr_state_class = None
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:battery-clock" if zone == 1 else "mdi:battery-clock-outline"
        self._attr_device_info = device_info

    @property
    def native_value(self):
        totals = self.coordinator._meter_totals.get(str(self._meter_id))
        if not totals:
            return None

        opts = self._entry.options
        mid = str(self._meter_id)
        ser = str(getattr(self, "_serial", ""))
        coeff = get_prosumer_coefficient(opts, meter_id=mid, serial=ser)

        # 1. Invoice date mode (v1.5.0)
        settle_str = str(opts.get(CONF_SETTLEMENT_DATE, DEFAULT_SETTLEMENT_DATE)).strip()
        monthly = getattr(self.coordinator, "_monthly", {}).get(str(mid), {})
        init_l1 = get_meter_initial_bank(opts, "kwh_l1", meter_id=mid, serial=ser, default=DEFAULT_BANK_INITIAL_KWH_L1)
        init_l2 = get_meter_initial_bank(opts, "kwh_l2", meter_id=mid, serial=ser, default=DEFAULT_BANK_INITIAL_KWH_L2)
        initial = get_meter_initial_bank(opts, "kwh", meter_id=mid, serial=ser, default=DEFAULT_BANK_INITIAL_KWH)
        if settle_str and monthly and (init_l1 > 0 or init_l2 > 0 or initial > 0):
            inv_bank, inv_detail = bank_from_invoice_date(
                settle_str, monthly, init_1=init_l1 or initial, init_2=init_l2, coeff=coeff
            )
            if inv_bank is not None and inv_detail:
                key = "bank_kwh_l1" if self._zone == 1 else "bank_kwh_l2"
                return round(inv_detail.get(key, 0.0), 2)

        # 2. Check FIFO mode (v1.5.0: automatic when no baselines or initial entered)
        bi = get_meter_baseline(opts, "import", meter_id=mid, serial=ser, default=DEFAULT_BALANCE_BASELINE)
        be = get_meter_baseline(opts, "export", meter_id=mid, serial=ser, default=DEFAULT_BALANCE_BASELINE)
        if bi == 0.0 and be == 0.0 and initial == 0.0 and not init_l1 and not init_l2:
            if monthly:
                _fifo_bank, fifo_detail = _fifo_bank_from_monthly(monthly, coeff, has_zones=True)
                if _fifo_bank is not None and fifo_detail is not None:
                    key = "bank_kwh_l1" if self._zone == 1 else "bank_kwh_l2"
                    if key in fifo_detail:
                        return round(fifo_detail[key], 2)

        # Baseline mode
        if self._zone == 1:
            bi_z = get_meter_baseline(opts, "import_1", meter_id=mid, serial=ser, default=bi)
            be_z = get_meter_baseline(opts, "export_1", meter_id=mid, serial=ser, default=be)
            imp_z = float(totals.get("import_1", totals.get("import", 0)))
            exp_z = float(totals.get("export_1", totals.get("export", 0)))
            init_z = init_l1
        else:
            bi_z = get_meter_baseline(opts, "import_2", meter_id=mid, serial=ser, default=bi)
            be_z = get_meter_baseline(opts, "export_2", meter_id=mid, serial=ser, default=be)
            imp_z = float(totals.get("import_2", 0))
            exp_z = float(totals.get("export_2", 0))
            init_z = init_l2

        net_imp = imp_z - bi_z
        net_exp = exp_z - be_z
        bilans = (net_exp * coeff) - net_imp
        bank = max(0.0, bilans) + init_z
        return round(bank, 2)

    @property
    def extra_state_attributes(self):
        zone_label = "dzienna" if self._zone == 1 else "nocna/weekendowa"
        return {
            "zone": self._zone,
            "description": f"Magazyn wirtualny dla strefy {self._zone} ({zone_label})",
            "unit": "kWh",
        }


class EnergaBankPlnSensor(CoordinatorEntity, SensorEntity):
    """Virtual storage in PLN for new prosumer (net-billing, RCE×1.23).

    For new system (coefficient 0.0): bank in PLN.
    bank = initial_pln + export×RCE×1.23 - import×cena_per_strefa.
    Per strefa G12W: import_1 × cena_1 + import_2 × cena_2.
    RCE fetched from PSE or manual input, ×1.23 (VAT on energy sold).
    """

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry, has_zones: bool = False, serial: str = "") -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._serial = serial
        self._entry = entry
        self._has_zones = has_zones
        self._attr_name = "Bank Wirtualny PLN"
        self._attr_unique_id = f"energa_{meter_id}_bank_pln"
        self._attr_has_entity_name = True
        self._attr_state_class = None
        self._attr_native_unit_of_measurement = "PLN"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_icon = "mdi:cash-check"
        self._attr_device_info = device_info

    @property
    def native_value(self):
        totals = self.coordinator._meter_totals.get(str(self._meter_id))
        if not totals:
            return None

        opts = self._entry.options
        mid = str(self._meter_id)
        ser = str(getattr(self, "_serial", ""))

        # Prefer coordinator RCE cache if auto-fetch enabled
        coord_rce = getattr(self.coordinator, "_rce_cache", None)
        if opts.get(CONF_RCE_AUTO_FETCH) and coord_rce is not None:
            rce = float(coord_rce)
        else:
            rce = float(opts.get(CONF_BANK_RCE_PRICE, DEFAULT_BANK_RCE_PRICE))
        initial = get_meter_initial_bank(opts, "pln", meter_id=mid, serial=ser, default=DEFAULT_BANK_INITIAL_PLN)

        bi = get_meter_baseline(opts, "import", meter_id=mid, serial=ser, default=DEFAULT_BALANCE_BASELINE)
        be = get_meter_baseline(opts, "export", meter_id=mid, serial=ser, default=DEFAULT_BALANCE_BASELINE)

        if self._has_zones:
            # Per-zone baselines if available
            bi1 = get_meter_baseline(opts, "import_1", meter_id=mid, serial=ser, default=bi)
            bi2 = get_meter_baseline(opts, "import_2", meter_id=mid, serial=ser, default=bi)
            be1 = get_meter_baseline(opts, "export_1", meter_id=mid, serial=ser, default=be)
            be2 = get_meter_baseline(opts, "export_2", meter_id=mid, serial=ser, default=be)

            imp1 = float(totals.get("import_1", totals.get("import", 0)))
            imp2 = float(totals.get("import_2", 0))
            exp1 = float(totals.get("export_1", totals.get("export", 0)))
            exp2 = float(totals.get("export_2", 0))

            # If per-zone baselines not set, use total
            if bi1 == bi and bi2 == bi:
                net_imp = float(totals.get("import", 0)) - bi
                net_exp = float(totals.get("export", 0)) - be
                tot_cur = imp1 + imp2
                if tot_cur > 0:
                    p1 = get_price_for_key(opts, "import_1", meter_id=mid, serial=ser)
                    p2 = get_price_for_key(opts, "import_2", meter_id=mid, serial=ser)
                    effective_price = (imp1 * p1 + imp2 * p2) / tot_cur
                else:
                    effective_price = get_price_for_key(opts, "import", meter_id=mid, serial=ser)
                net_imp_cost = net_imp * effective_price
            else:
                net_imp1 = imp1 - bi1
                net_imp2 = imp2 - bi2
                price1 = get_price_for_key(opts, "import_1", meter_id=mid, serial=ser)
                price2 = get_price_for_key(opts, "import_2", meter_id=mid, serial=ser)
                net_imp_cost = net_imp1 * price1 + net_imp2 * price2
                net_exp = (exp1 - be1) + (exp2 - be2)
                net_imp = net_imp1 + net_imp2
        else:
            net_imp = float(totals.get("import", 0)) - bi
            net_exp = float(totals.get("export", 0)) - be
            price = get_price_for_key(opts, "import", meter_id=mid, serial=ser)
            net_imp_cost = net_imp * price

        comp_export = net_exp * rce * 1.23
        gross_deposit = max(0.0, initial + comp_export)
        net_balance = initial + comp_export - net_imp_cost
        deposit_applied = min(gross_deposit, max(0.0, net_imp_cost))
        deposit_remaining = max(0.0, gross_deposit - deposit_applied)

        _LOGGER.debug(
            "BankPln %s: net_imp_cost=%.2f net_exp=%.2f rce=%.5f comp_export=%.2f initial=%.2f deposit_remaining=%.2f net_balance=%.2f",
            mid, net_imp_cost, net_exp, rce, comp_export, initial, deposit_remaining, net_balance,
        )

        coord_cache = getattr(self.coordinator, "_rce_cache", None)
        if opts.get(CONF_RCE_AUTO_FETCH) and coord_cache is not None:
            rce_source = getattr(self.coordinator, "_rce_source", None) or "PSE auto"
        else:
            rce_source = "manual"
        attrs = {
            "net_import_kwh": round(net_imp, 2),
            "net_export_kwh": round(net_exp, 2),
            "rce_price": rce,
            "rce_source": rce_source,
            "vat_multiplier": 1.23,
            "compensation_export_pln": round(comp_export, 2),
            "import_cost_pln": round(net_imp_cost, 2),
            "initial_pln": initial,
            "gross_deposit_pln": round(gross_deposit, 2),
            "deposit_applied_pln": round(deposit_applied, 2),
            "deposit_remaining_pln": round(deposit_remaining, 2),
            "net_financial_balance_pln": round(net_balance, 2),
            "source": "net-billing RCE×1.23 miesięczny (nowy system)",
            "formula": "max(0, initial + export×RCE×1.23 - import×cena_strefa)",
            "unit": "PLN — depozyt prosumencki (aktywo, nigdy ujemne); bilans netto w atrybucie net_financial_balance_pln",
            "rule_version": "net_billing_fifo_12m_v1",
            "settlement_type": "net_billing_rcem",
            "calculated_at": datetime.now(timezone.utc).isoformat(),
        }
        if opts.get(CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT):
            from datetime import date as _date

            _today = _date.today()
            attrs["deposit_valid_until"] = deposit_valid_until(_today.year, _today.month).isoformat()
            attrs["refund_cap_note"] = (
                "Niewykorzystany depozyt zwracany po 12 m-cach max 20% (RCEm) "
                "/ 30% (RCE od 01.02.2025), do końca 13. miesiąca (Dz.U. 1847)."
            )
            attrs["validity_note"] = (
                "Depozyt ważny 12 m-cy od przypisania (M+1, ×1.23), najstarsze "
                "środki najpierw. Zerowanie co miesiąc NIE obowiązuje."
            )
            attrs["hourly_netting_note"] = (
                "Sprzedawca bilansuje godzinowo (faktura 07: 456 kWh z delty "
                "licznika 523 kWh); sensor liczy z delt licznika — przybliżenie."
            )
            settle_str = opts.get(CONF_SETTLEMENT_DATE, DEFAULT_SETTLEMENT_DATE)
            if parse_settlement_date(settle_str) is not None:
                attrs["days_to_settlement"] = days_to_settlement(settle_str)
        if self._has_zones:
            price1 = get_price_for_key(opts, "import_1", meter_id=mid, serial=ser)
            price2 = get_price_for_key(opts, "import_2", meter_id=mid, serial=ser)
            attrs.update({
                "price_1": price1,
                "price_2": price2,
                "import_1": round(float(totals.get("import_1", 0)), 2),
                "import_2": round(float(totals.get("import_2", 0)), 2),
                "per_strefa_note": "L1 droga ×1.30 / L2 tania ×0.65 — koszt liczony per strefa",
            })
        self._attr_extra_state_attributes = attrs

        return round(deposit_remaining, 2)


class EnergaBankLevelSensor(CoordinatorEntity, SensorEntity):
    """Warehouse fill level in % (old net-metering only, v0.3.0).

    level = Bank kWh / deposits (export×coeff credited in the live
    12-month window) × 100. 100% = nothing withdrawn/expired yet,
    0% = warehouse empty. Needs ~11 months of history (FIFO mode);
    without history the state is None (unknown) instead of a guess —
    the kWh gauge (Bank) stays the source of truth meanwhile.

    device_class BATTERY + % unit: renders as a battery gauge in HA.
    """

    def __init__(self, coordinator, meter_id: str, device_info: DeviceInfo, entry: ConfigEntry, serial: str = "") -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._entry = entry
        self._attr_name = "Magazyn Poziom"
        self._attr_unique_id = f"energa_{meter_id}_bank_level"
        self._attr_has_entity_name = True
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_icon = "mdi:battery-medium"
        self._attr_device_info = device_info

    @property
    def native_value(self):
        opts = self._entry.options
        try:
            coeff = float(opts.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT))
        except (ValueError, TypeError):
            coeff = DEFAULT_PROSUMER_COEFFICIENT
        if coeff < 0.7:
            return None  # new net-billing has no kWh warehouse
        if not opts.get(CONF_ENABLE_AUTO_SETTLEMENT, DEFAULT_ENABLE_AUTO_SETTLEMENT):
            return None
        monthly = getattr(self.coordinator, "_monthly", {}).get(str(self._meter_id), {})
        bank, detail = _fifo_bank_from_monthly(monthly, coeff)
        if bank is None or not detail:
            return None
        level = warehouse_level_pct(bank, detail.get("deposits_kwh"))
        months_used = int(detail.get("months_used", 0) or 0)
        self._attr_extra_state_attributes = {
            "bank_kwh": bank,
            "deposits_12m_kwh": detail.get("deposits_kwh"),
            "expired_12m_kwh": detail.get("expired_kwh"),
            "uncovered_12m_kwh": detail.get("uncovered_kwh"),
            "months_used": months_used,
            "coverage_status": (
                f"partial ({months_used}/12m)" if months_used < 12 else "full (12m)"
            ),
            "source": "FIFO 12 m-cy z miesięcznych przepływów (jak Bank kWh)",
            "formula": "Bank / wkłady_12m × 100",
        }
        return level


class EnergaBankFlowSensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Native bank charge/discharge totals for the Energy battery (v0.2.12).

    Energy Dashboard batteries need total_increasing FLOW sensors, while
    Bank kWh/PLN sensors expose STATE. These two sensors (charge/discharge)
    accumulate Bilans movement between coordinator updates via
    FlowAccumulator (first reading only anchors, restart-safe through
    HA state restore). Replaces the bank_energii.yaml template pair.

    - Old net-metering: base is Bilans (net_exp*coeff - net_imp).
    - New net-billing: charge follows net_export, discharge net_import
      (raw kWh; money value lives in Bank PLN).
    """

    def __init__(
        self, coordinator, meter_id: str, device_info: DeviceInfo,
        entry: ConfigEntry, has_zones: bool = False,
        direction: str = "charge", serial: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._serial = serial
        self._entry = entry
        self._has_zones = has_zones
        self._direction = direction
        self._flows = FlowAccumulator()
        is_charge = direction == "charge"
        self._attr_name = (
            "Bank Ładowanie" if is_charge else "Bank Rozładowanie"
        )
        self._attr_unique_id = f"energa_{meter_id}_bank_{direction}"
        self._attr_has_entity_name = True
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = (
            "mdi:battery-arrow-up" if is_charge else "mdi:battery-arrow-down"
        )
        self._attr_device_info = device_info
        self._restored = False

    async def async_added_to_hass(self) -> None:
        """Restore previous cumulative flow state on startup (anti-reset)."""
        await super().async_added_to_hass()
        candidates = []
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import get_last_statistics
            last_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, self.entity_id, True, {"sum"}
            )
            _pts = last_stat.get(self.entity_id) or []
            _sums = [p.get("sum") for p in _pts if p.get("sum") is not None]
            if _sums:
                candidates.append(max(0.0, max(_sums)))
        except Exception:
            pass
        last = await self.async_get_last_state()
        if last is not None and last.state not in (None, "unknown", "unavailable", "none"):
            try:
                candidates.append(float(last.state))
            except (ValueError, TypeError):
                pass
        if candidates:
            value = max(candidates)
            if self._direction == "charge":
                self._flows.restore(charge=value, discharge=None)
            else:
                self._flows.restore(charge=None, discharge=value)
        self._restored = True

    def _nets(self):
        """(net_import, net_export) from meter totals minus baselines."""
        totals = self.coordinator._meter_totals.get(str(self._meter_id))
        if not totals:
            return None
        opts = self._entry.options
        mid = str(self._meter_id)
        ser = str(getattr(self, "_serial", ""))
        bi = get_meter_baseline(opts, "import", meter_id=mid, serial=ser, default=DEFAULT_BALANCE_BASELINE)
        be = get_meter_baseline(opts, "export", meter_id=mid, serial=ser, default=DEFAULT_BALANCE_BASELINE)
        if self._has_zones:
            bi1 = get_meter_baseline(opts, "import_1", meter_id=mid, serial=ser, default=bi)
            bi2 = get_meter_baseline(opts, "import_2", meter_id=mid, serial=ser, default=bi)
            be1 = get_meter_baseline(opts, "export_1", meter_id=mid, serial=ser, default=be)
            be2 = get_meter_baseline(opts, "export_2", meter_id=mid, serial=ser, default=be)
            if bi1 == bi and bi2 == bi:
                net_imp = float(totals.get("import", 0)) - bi
                net_exp = float(totals.get("export", 0)) - be
            else:
                imp1 = float(totals.get("import_1", totals.get("import", 0)))
                imp2 = float(totals.get("import_2", 0))
                exp1 = float(totals.get("export_1", totals.get("export", 0)))
                exp2 = float(totals.get("export_2", 0))
                net_imp = (imp1 - bi1) + (imp2 - bi2)
                net_exp = (exp1 - be1) + (exp2 - be2)
        else:
            net_imp = float(totals.get("import", 0)) - bi
            net_exp = float(totals.get("export", 0)) - be
        return (net_imp, net_exp)

    @property
    def native_value(self):
        """Return None — flow statistics flow exclusively via async_import_statistics.

        Prevents Home Assistant Core's recorder from calculating competing
        statistics from states table deltas, which previously caused sum resets
        and multi-megawatt spikes on the Energy Dashboard battery section.
        """
        return None


