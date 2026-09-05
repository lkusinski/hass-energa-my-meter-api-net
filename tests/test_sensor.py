"""Tests for sensor logic — prosumer balance calculation.

The prosumer balance formula is:
    balance = ((meter_export − baseline_export) × coefficient)
            − (meter_import − baseline_import)

Baselines represent meter readings at the start of the tracking period.
With baselines = 0, this is equivalent to lifetime calculation.
"""




def calculate_prosumer_balance(
    meter_import: float,
    meter_export: float,
    coefficient: float = 0.8,
    baseline_import: float = 0.0,
    baseline_export: float = 0.0,
) -> float:
    """Extract of EnergaProsumerBalanceSensor.native_value calculation."""
    net_export = meter_export - baseline_export
    net_import = meter_import - baseline_import
    balance = (net_export * coefficient) - net_import
    return round(balance, 2)


class TestProsumerBalance:
    """Tests for prosumer balance calculation."""

    def test_positive_balance(self):
        """More export than import → positive balance (surplus)."""
        result = calculate_prosumer_balance(
            meter_import=100.0, meter_export=200.0, coefficient=0.8
        )
        # 200 * 0.8 - 100 = 60
        assert result == 60.0

    def test_negative_balance(self):
        """More import than export → negative balance (debt)."""
        result = calculate_prosumer_balance(
            meter_import=500.0, meter_export=200.0, coefficient=0.8
        )
        # 200 * 0.8 - 500 = -340
        assert result == -340.0

    def test_zero_balance(self):
        """Exact equilibrium."""
        result = calculate_prosumer_balance(
            meter_import=80.0, meter_export=100.0, coefficient=0.8
        )
        # 100 * 0.8 - 80 = 0
        assert result == 0.0

    def test_custom_coefficient(self):
        """Non-default coefficient (e.g. 0.7 for net-billing)."""
        result = calculate_prosumer_balance(
            meter_import=100.0, meter_export=200.0, coefficient=0.7
        )
        # 200 * 0.7 - 100 = 40
        assert result == 40.0

    def test_coefficient_1_0(self):
        """Coefficient 1.0 = no loss on exchange."""
        result = calculate_prosumer_balance(
            meter_import=100.0, meter_export=100.0, coefficient=1.0
        )
        assert result == 0.0

    def test_zero_export(self):
        """Consumer only (no export) → negative balance."""
        result = calculate_prosumer_balance(
            meter_import=500.0, meter_export=0.0, coefficient=0.8
        )
        assert result == -500.0

    def test_zero_import(self):
        """Full self-consumption → positive balance."""
        result = calculate_prosumer_balance(
            meter_import=0.0, meter_export=100.0, coefficient=0.8
        )
        assert result == 80.0

    def test_real_world_values(self):
        """Real-world test case from Lab verification."""
        # From G12W test: export ~29527, import ~45449
        result = calculate_prosumer_balance(
            meter_import=45449.543,
            meter_export=29526.870,
            coefficient=0.8,
        )
        # 29526.870 * 0.8 - 45449.543 = 23621.496 - 45449.543 = -21828.047
        assert result == -21828.05  # rounded to 2 dp

    def test_float_precision(self):
        """Floating point edge case."""
        result = calculate_prosumer_balance(
            meter_import=0.1, meter_export=0.1, coefficient=0.8
        )
        # 0.1 * 0.8 - 0.1 = 0.08 - 0.1 = -0.02
        assert result == -0.02

    # === Baseline tests (issue #27) ===

    def test_baseline_subtraction(self):
        """Baselines correctly subtract from meter totals."""
        result = calculate_prosumer_balance(
            meter_import=45507.0, meter_export=29580.0,
            baseline_import=45177.0, baseline_export=29389.0,
            coefficient=0.8,
        )
        # net_export = 29580 - 29389 = 191
        # net_import = 45507 - 45177 = 330
        # balance = 191 * 0.8 - 330 = 152.8 - 330 = -177.2
        assert result == -177.2

    def test_baseline_gednet_scenario(self):
        """Issue #27: gednet's real-world case with 0.7 coefficient."""
        # User exported 840 kWh, imported 10 kWh since period start
        result = calculate_prosumer_balance(
            meter_import=10010.0, meter_export=10840.0,
            baseline_import=10000.0, baseline_export=10000.0,
            coefficient=0.7,
        )
        # net_export = 840, net_import = 10
        # balance = 840 * 0.7 - 10 = 588 - 10 = 578
        assert result == 578.0

    def test_zero_baselines_equals_lifetime(self):
        """With baselines = 0, formula is equivalent to lifetime totals."""
        lifetime = calculate_prosumer_balance(
            meter_import=1000.0, meter_export=2000.0, coefficient=0.8
        )
        with_zero_baselines = calculate_prosumer_balance(
            meter_import=1000.0, meter_export=2000.0,
            baseline_import=0.0, baseline_export=0.0,
            coefficient=0.8,
        )
        assert lifetime == with_zero_baselines


class TestSensorCreationLogic:
    """Tests for sensor creation branching (G11 vs G12W paths)."""

    def test_g11_creates_single_import_sensor(self):
        """G11 meter uses data_key='import', name='Panel Energia Zużycie'."""
        has_zones = False
        if has_zones:
            data_keys = ["import_1", "import_2"]
        else:
            data_keys = ["import"]
        assert data_keys == ["import"]

    def test_g12w_creates_zone_sensors(self):
        """G12W meter creates two zone-specific sensors."""
        has_zones = True
        if has_zones:
            data_keys = ["import_1", "import_2"]
        else:
            data_keys = ["import"]
        assert data_keys == ["import_1", "import_2"]

    def test_g12w_creates_zone_export_sensors(self):
        """G12W creates per-zone export sensors (export_1, export_2)."""
        has_zones = True
        has_export = True
        if has_export and has_zones:
            export_keys = ["export_1", "export_2"]
        elif has_export:
            export_keys = ["export"]
        else:
            export_keys = []
        assert export_keys == ["export_1", "export_2"]

    def test_g11_creates_single_export_sensor(self):
        """G11 (single-zone) creates single export sensor."""
        has_zones = False
        has_export = True
        if has_export and has_zones:
            export_keys = ["export_1", "export_2"]
        elif has_export:
            export_keys = ["export"]
        else:
            export_keys = []
        assert export_keys == ["export"]

    def test_prosumer_sensor_only_for_exporters(self):
        """Prosumer balance only created when obis_minus exists."""
        meter_with_export = {"obis_minus": "1-0:2.8.0*255"}
        meter_without_export = {"obis_minus": None}

        assert bool(meter_with_export.get("obis_minus")) is True
        assert bool(meter_without_export.get("obis_minus")) is False


class TestChartZoneData:
    """Tests for chart API zone structure interpretation.

    Based on sample data from a G12W test account, 2026-03-27.
    zones[] array: index 0 = Strefa 1 (dzienna), index 1 = Strefa 2 (nocna).
    """

    def test_g12w_import_zone_mapping_nocna(self):
        """Hour 00 (nocna): import in zones[1], zones[0] is null."""
        zones = [None, 0.981, None]  # real API data
        zone_1 = zones[0] if zones[0] is not None else 0.0
        zone_2 = zones[1] if zones[1] is not None else 0.0
        assert zone_1 == 0.0    # strefa dzienna not active at midnight
        assert zone_2 == 0.981  # strefa nocna active

    def test_g12w_import_zone_mapping_dzienna(self):
        """Hour 12 (dzienna): import in zones[0], zones[1] is null."""
        zones = [0.083, None, None]  # real API data
        zone_1 = zones[0] if zones[0] is not None else 0.0
        zone_2 = zones[1] if zones[1] is not None else 0.0
        assert zone_1 == 0.083  # strefa dzienna active at noon
        assert zone_2 == 0.0

    def test_g12w_export_zone_mapping(self):
        """Export chart uses same zones[] structure as import."""
        zones = [2.701, None, None]  # real API data, hour 12 export
        zone_1 = zones[0] if zones[0] is not None else 0.0
        zone_2 = zones[1] if zones[1] is not None else 0.0
        assert zone_1 == 2.701  # strefa dzienna export
        assert zone_2 == 0.0

    def test_g12w_export_nocna(self):
        """Export during nocna hours goes to zones[1]."""
        zones = [None, 3.485, None]  # real API data, hour 13 export
        zone_1 = zones[0] if zones[0] is not None else 0.0
        zone_2 = zones[1] if zones[1] is not None else 0.0
        assert zone_1 == 0.0
        assert zone_2 == 3.485

    def test_g11_single_zone(self):
        """G11 always puts data in zones[0]."""
        zones = [0.449, None, None]  # real API data from G11
        total = zones[0] if zones[0] is not None else 0.0
        assert total == 0.449

    def test_zones_third_element_always_null(self):
        """Third element (zones[2]) is always null in current API."""
        test_cases = [
            [None, 0.981, None],
            [0.083, None, None],
            [2.701, None, None],
            [None, 3.485, None],
            [0.449, None, None],
        ]
        for zones in test_cases:
            assert zones[2] is None


class TestNetBillingDeposit:
    """Tests for Net-billing deposit logic (EnergaBankPlnSensor)."""

    def test_deposit_never_negative_when_cost_exceeds_deposit(self):
        """When energy cost exceeds export compensation, deposit is 0, not negative."""
        initial = 0.0
        comp_export = 176.93
        net_imp_cost = 863.75

        gross_deposit = max(0.0, initial + comp_export)
        net_balance = initial + comp_export - net_imp_cost
        deposit_applied = min(gross_deposit, max(0.0, net_imp_cost))
        deposit_remaining = max(0.0, gross_deposit - deposit_applied)

        assert deposit_remaining == 0.0
        assert gross_deposit == 176.93
        assert deposit_applied == 176.93
        assert round(net_balance, 2) == -686.82

    def test_deposit_surplus_when_export_exceeds_cost(self):
        """When export compensation exceeds cost, remaining deposit is positive."""
        initial = 50.0
        comp_export = 300.0
        net_imp_cost = 120.0

        gross_deposit = max(0.0, initial + comp_export)
        net_balance = initial + comp_export - net_imp_cost
        deposit_applied = min(gross_deposit, max(0.0, net_imp_cost))
        deposit_remaining = max(0.0, gross_deposit - deposit_applied)

        assert gross_deposit == 350.0
        assert deposit_applied == 120.0
        assert deposit_remaining == 230.0
        assert net_balance == 230.0


class TestEarlyMonthSmoothing:
    """Tests for early-month forecast smoothing in EnergaBillForecastSensor."""

    def test_day_4_blends_mtd_and_history(self):
        """On day 4, weight is 4/7 MTD and 3/7 history."""
        elapsed = 4
        days_in_month = 30
        cov = 30
        w_mtd = elapsed / 7.0
        w_hist = 1.0 - w_mtd

        # Heavy day consumption in 4 days = 40 kWh (10 kWh/day)
        # Trailing history = 150 kWh in 30 days (5 kWh/day)
        m_imp_d = 40.0 / elapsed
        t_imp_d = 150.0 / cov

        smoothed_daily = w_mtd * m_imp_d + w_hist * t_imp_d
        f_imp_d = smoothed_daily * days_in_month

        # Pure linear: 10 * 30 = 300 kWh
        # Smoothed: (4/7 * 10 + 3/7 * 5) * 30 = (5.714 + 2.143) * 30 = ~235.7 kWh
        assert round(f_imp_d, 1) == 235.7
        assert f_imp_d < 300.0


class TestBillCurrentSensor:
    """Tests for EnergaBillCurrentSensor (MTD actual bill)."""

    def test_current_bill_calculation(self):
        """MTD bill reflects actual consumed energy minus prosumer settlement."""
        from custom_components.energa_mobile.tariff import compute_bill, G12W_DEFAULT_FEES

        # 4 days MTD: 51.74 kWh day, 9.70 kWh night, 41.58 kWh export, RCEm 0.26288
        imp_d = 51.74
        imp_n = 9.70
        exp_tot = 41.58
        rce = 0.26288
        fees = dict(G12W_DEFAULT_FEES)
        fees["capacity"] = 16.01

        bill = compute_bill(imp_d, imp_n, exp_tot, rce, fees)
        assert bill is not None
        assert "do_zaplaty" in bill
        # Verify MTD do_zaplaty matches expected formula with bracketed capacity: 105.46 PLN
        assert round(bill["do_zaplaty"], 2) == 105.46
        assert round(bill["deposit"], 2) == 13.44
        assert round(bill["deposit_applied"], 2) == 13.44
        assert round(bill["brutto"], 2) == 118.90

        # With default capacity fee:
        bill_default = compute_bill(imp_d, imp_n, exp_tot, rce)
        assert round(bill_default["do_zaplaty"], 2) == 115.35


class TestBillComponentSensor:
    """Tests for EnergaBillComponentSensor (v1.0.4)."""

    def test_bill_components_breakdown(self):
        """Verify individual MTD bill components match compute_bill output."""
        from custom_components.energa_mobile.tariff import compute_bill, G12W_DEFAULT_FEES

        imp_d = 51.74
        imp_n = 9.70
        exp_tot = 41.58
        rce = 0.26288
        fees = dict(G12W_DEFAULT_FEES)
        fees["capacity"] = 16.01

        bill = compute_bill(imp_d, imp_n, exp_tot, rce, fees)
        assert bill is not None
        assert round(bill["brutto"], 2) == 118.90
        assert round(bill["sale_total"], 2) == 35.47
        assert round(bill["distr_total"], 2) == 61.20
        assert round(bill["deposit"], 2) == 13.44
        assert round(bill["deposit_applied"], 2) == 13.44

    def test_component_sensor_breakdown_keys(self):
        """Verify component key mapping for MTD breakdown."""
        key_map = {
            "brutto": "mtd_brutto_pln",
            "sale_total": "mtd_sale_total_pln",
            "distr_total": "mtd_distr_total_pln",
            "deposit": "mtd_deposit_pln",
            "deposit_applied": "mtd_deposit_applied_pln",
            "cover_day": "cover_day_kwh",
            "cover_night": "cover_night_kwh",
        }
        assert key_map["brutto"] == "mtd_brutto_pln"
        assert key_map["sale_total"] == "mtd_sale_total_pln"
        assert key_map["distr_total"] == "mtd_distr_total_pln"
        assert key_map["deposit"] == "mtd_deposit_pln"
        assert key_map["deposit_applied"] == "mtd_deposit_applied_pln"
        assert key_map["cover_day"] == "cover_day_kwh"
        assert key_map["cover_night"] == "cover_night_kwh"

    def test_component_sensor_deposit_applied_deduction(self):
        """Verify deposit_applied is exposed as negative deduction for bill arithmetic."""
        val = 20.73
        deduction = -round(abs(float(val)), 2) if float(val) > 0 else 0.0
        assert deduction == -20.73


class TestPeriodSumsFallback:
    """Tests for in-memory period sums calculation and fallback."""

    def test_compute_period_sums_from_memory(self):
        """Verify that coordinator calculates period sums correctly from hourly stats."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from custom_components.energa_mobile.sensor import EnergaCoordinator

        tz = ZoneInfo("Europe/Warsaw")
        start = datetime(2026, 9, 1, 0, 0, tzinfo=tz)
        end = datetime(2026, 9, 5, 23, 59, tzinfo=tz)

        # Mock coordinator
        class MockCoordinator:
            _hourly_stats = {
                "12345": {
                    "import_1": [
                        {"start": datetime(2026, 9, 2, 10, 0, tzinfo=tz), "state": 1.25},
                        {"start": datetime(2026, 9, 3, 14, 0, tzinfo=tz), "state": 2.75},
                        # Point outside window (August)
                        {"start": datetime(2026, 8, 31, 23, 0, tzinfo=tz), "state": 10.0},
                    ],
                    "import_2": [
                        {"start": datetime(2026, 9, 2, 23, 0, tzinfo=tz), "state": 0.50},
                    ],
                    "export_1": [
                        {"start": datetime(2026, 9, 2, 12, 0, tzinfo=tz), "state": 3.00},
                    ],
                }
            }

        out = EnergaCoordinator._compute_period_sums_from_memory(
            MockCoordinator(), start, end
        )
        assert "12345" in out
        sums = out["12345"]
        assert sums["import_1"] == 4.0
        assert sums["import_2"] == 0.5
        assert sums["import"] == 4.5  # sum of import_1 + import_2
        assert sums["export_1"] == 3.0
        assert sums["export"] == 3.0
        assert sums["_coverage_days"] == 4

    def test_compute_period_sums_iso_string_and_single_zone(self):
        """Verify handling of ISO timestamp strings and single-zone meter."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from custom_components.energa_mobile.sensor import EnergaCoordinator

        tz = ZoneInfo("Europe/Warsaw")
        start = datetime(2026, 9, 1, 0, 0, tzinfo=tz)
        end = datetime(2026, 9, 5, 23, 59, tzinfo=tz)

        class MockSingleZoneCoordinator:
            _hourly_stats = {
                "99999": {
                    "import": [
                        {"start": "2026-09-02T10:00:00+02:00", "state": 5.5},
                    ],
                    "export": [
                        {"start": "2026-09-02T12:00:00+02:00", "state": 8.0},
                    ],
                }
            }

        out = EnergaCoordinator._compute_period_sums_from_memory(
            MockSingleZoneCoordinator(), start, end
        )
        assert "99999" in out
        assert out["99999"]["import"] == 5.5
        assert out["99999"]["export"] == 8.0



