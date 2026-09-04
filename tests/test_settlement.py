"""Tests for v0.2.11 settlement helpers (FIFO calibration).

Legal background (verified 2026-09-04):
- Old net-metering: 12 months from introduction month-end, FIFO.
- New net-billing: deposit valid 12 months from assignment (M+1, x1.23),
  refund cap 20% RCEm / 30% RCE.
A plain calendar reset (Jan 1 / monthly) would NOT comply.
"""

from datetime import date

from custom_components.energa_mobile.settlement import (
    days_to_settlement,
    deposit_valid_until,
    fifo_kwh_bank,
    flow_history_series,
    is_export_prosumer,
    latest_official_rcem,
    month_to_date_forecast,
    next_settlement_date,
    orphan_bank_uids,
    parse_official_rcem_table,
    parse_settlement_date,
    rolling_kwh_bank,
    target_rcem_month,
)

SAMPLE_HTML = """
<table><tbody>
<tr><td bgcolor="#eeeeee" colspan="4"><b>czerwiec</b></td></tr>
<tr><td>RCEm&nbsp;</td><td align="right">273,20</td><td align="center">11.07.2026</td><td align="center">-</td></tr>
<tr><td bgcolor="#eeeeee" colspan="4"><b>lipiec</b></td></tr>
<tr><td>RCEm&nbsp;</td><td align="right">262,88</td><td align="center">11.08.2026</td><td align="center">-</td></tr>
<tr><td bgcolor="#eeeeee" colspan="4"><b>sierpień</b></td></tr>
<tr><td>RCEm&nbsp;</td><td align="right">301,10</td><td align="center">11.09.2026</td><td align="center">-</td></tr>
</tbody></table>
"""


class TestParseSettlementDate:
    def test_valid(self):
        assert parse_settlement_date("2026-06-30") == date(2026, 6, 30)

    def test_empty(self):
        assert parse_settlement_date("") is None
        assert parse_settlement_date(None) is None

    def test_invalid(self):
        assert parse_settlement_date("30.06.2026") is None
        assert parse_settlement_date("junk") is None


class TestNextSettlementDate:
    def test_future_this_year(self):
        assert next_settlement_date(date(2026, 6, 30), date(2026, 9, 3)) == date(2027, 6, 30)

    def test_today(self):
        assert next_settlement_date(date(2026, 6, 30), date(2026, 6, 30)) == date(2026, 6, 30)

    def test_feb29_fallback(self):
        assert next_settlement_date(date(2024, 2, 29), date(2025, 1, 1)) == date(2025, 2, 28)


class TestDaysToSettlement:
    def test_unset(self):
        assert days_to_settlement("") is None

    def test_g12w_stare_invoice_anniversary(self):
        # 2026-06-30 anniversary, today 2026-09-03 -> 2027-06-30
        assert days_to_settlement("2026-06-30", date(2026, 9, 3)) == 300


class TestTargetRcemMonth:
    def test_after_publication(self):
        # Sep 15 -> August published on Sep 11
        assert target_rcem_month(date(2026, 9, 15)) == (2026, 8)

    def test_before_publication(self):
        # Sep 3 -> latest published is July (Aug comes Sep 11)
        assert target_rcem_month(date(2026, 9, 3)) == (2026, 7)

    def test_january_edge(self):
        # Jan 5 -> November of previous year
        assert target_rcem_month(date(2026, 1, 5)) == (2025, 11)


class TestOfficialRcemTable:
    def test_parse(self):
        rows = parse_official_rcem_table(SAMPLE_HTML)
        assert (2026, 6, 0.2732) in rows
        assert (2026, 7, 0.26288) in rows
        assert (2026, 8, 0.3011) in rows

    def test_latest_respects_publication(self):
        # Sep 3: August RCEm (publ. Sep 11) not yet out -> July
        assert latest_official_rcem(SAMPLE_HTML, date(2026, 9, 3)) == (2026, 7, 0.26288)
        # Sep 15: August available
        assert latest_official_rcem(SAMPLE_HTML, date(2026, 9, 15))[2] == 0.3011


class TestRollingBank:
    def test_surplus(self):
        # 2022.55 * 0.8 - 503.86 = 1114.18 (G12W-stare scenario)
        assert rolling_kwh_bank(2022.55, 503.86, 0.8) == 1114.18

    def test_floored_at_zero(self):
        assert rolling_kwh_bank(100.0, 500.0, 0.8) == 0.0


class TestForecast:
    def test_linear(self):
        # -200 PLN on day 10 of 30-day month -> -600 forecast
        assert month_to_date_forecast(-200.0, 10, 30) == -600.0

    def test_full_month(self):
        assert month_to_date_forecast(150.0, 30, 30) == 150.0


class TestDepositValidUntil:
    def test_july_deposit(self):
        # Deposit for 07.2026 assigned 08.2026, valid 12m -> 31.08.2027
        assert deposit_valid_until(2026, 7) == date(2027, 8, 31)

    def test_december_rollover(self):
        assert deposit_valid_until(2026, 12) == date(2028, 1, 31)


class TestIsExportProsumer:
    """v0.2.15: obis_minus alone must not spawn prosumer sensors.

    Consumer meters (e.g. G11 without PV) may still report
    export OBIS codes with zero readings — Bank 0.0, phantom
    charge/discharge flows and a Bilans == -import are noise.
    """

    def test_empty(self):
        assert is_export_prosumer(None) is False
        assert is_export_prosumer({}) is False

    def test_obis_codes_alone_not_enough(self):
        assert is_export_prosumer({"obis_minus": "1-0:2.8.0"}) is False
        assert is_export_prosumer({"obis_minus": "x", "total_minus": 0}) is False

    def test_zero_totals_not_prosumer(self):
        meter = {"obis_minus": "x", "total_minus": 0.0,
                 "total_minus_1": 0, "total_minus_2": 0.0}
        assert is_export_prosumer(meter) is False

    def test_nonzero_export_total(self):
        assert is_export_prosumer({"total_minus": 523.5}) is True
        assert is_export_prosumer({"total_minus_1": 0, "total_minus_2": 12.3}) is True

    def test_seller_flag_without_export_yet(self):
        # New PV, nothing exported yet — still a prosumer
        assert is_export_prosumer({"is_prosumer": True, "total_minus": 0}) is True

    def test_invalid_values_defensive(self):
        assert is_export_prosumer({"total_minus": "junk"}) is False
        assert is_export_prosumer({"total_minus": None}) is False


class TestOrphanBankUids:
    """v0.2.19: which stale unique IDs the setup cleanup removes."""

    def test_consumer_full_set_but_keeps_forecast(self):
        doomed = orphan_bank_uids("73000003", "73000003", False, 0.8)
        assert "energa_73000003_prosumer_balance" in doomed
        assert "energa_73000003_bank_kwh" in doomed
        assert "energa_73000003_bank_charge" in doomed
        assert "energa_73000003_export_stats" in doomed
        # v0.2.17+: consumers keep their import-bill forecast
        assert "energa_73000003_bill_forecast" not in doomed

    def test_old_system_dooms_pln_bank_and_rcem(self):
        doomed = orphan_bank_uids("71000001", "71000001", True, 0.8)
        assert doomed == {"energa_71000001_bank_pln", "energa_71000001_rcem_auto"}

    def test_new_system_dooms_kwh_bank(self):
        doomed = orphan_bank_uids("72000002", "72000002", True, 0.0)
        assert doomed == {"energa_72000002_bank_kwh", "energa_72000002_bank_level"}

    def test_invalid_coefficient_dooms_nothing(self):
        assert orphan_bank_uids("1", "1", True, None) == set()
        assert orphan_bank_uids("1", "1", True, "junk") == set()


class TestFifoKwhBank:
    """v0.2.20: warehouse reconstructed from monthly flows (no typing)."""

    def test_stable_prosumer(self):
        flows = [(2025, m, 50.0, 100.0) for m in range(9, 13)]
        flows += [(2026, m, 50.0, 100.0) for m in range(1, 9)]
        bank, detail = fifo_kwh_bank(flows, 0.8, date(2026, 9, 3))
        # 12 live months x (80-50)
        assert bank == 360.0
        assert detail["months_used"] == 12

    def test_expired_energy_vanishes(self):
        flows = [(2025, 1, 0.0, 100.0)]  # introduced 20 months ago
        bank, detail = fifo_kwh_bank(flows, 0.8, date(2026, 9, 3))
        assert bank == 0.0
        assert detail["expired_kwh"] == 80.0

    def test_oldest_first_order(self):
        # m1: +100, m2: import 150 (eats m1 fully, 50 uncovered), m3: +100
        flows = [(2026, 1, 0.0, 100.0), (2026, 2, 150.0, 0.0), (2026, 3, 0.0, 100.0)]
        bank, detail = fifo_kwh_bank(flows, 1.0, date(2026, 9, 3))
        assert bank == 100.0
        assert detail["uncovered_kwh"] == 50.0

    def test_empty_and_invalid(self):
        assert fifo_kwh_bank([], 0.8)[0] == 0.0
        assert fifo_kwh_bank(None, 0.8)[0] == 0.0
        assert fifo_kwh_bank([("x", 1, 2, 3)], 0.8)[0] == 0.0
        assert fifo_kwh_bank([(2026, 5, -10.0, -5.0)], 0.8)[0] == 0.0

    def test_future_months_ignored(self):
        flows = [(2026, 9, 0.0, 100.0), (2027, 5, 0.0, 100.0)]
        bank, _ = fifo_kwh_bank(flows, 0.8, date(2026, 9, 3))
        assert bank == 80.0


class TestFlowHistorySeries:
    """v0.2.23: replay live accumulator semantics over hourly history."""

    def test_old_system_mirrors_live(self):
        from custom_components.energa_mobile.settlement import FlowAccumulator

        hours = [(2.0, 5.0), (1.0, 0.0), (0.0, 3.0), (4.0, 1.0)]
        ch, dis = flow_history_series(hours, 0.8, True)
        acc = FlowAccumulator()
        cum_e = cum_i = 0.0
        for imp, exp in hours:
            cum_e += exp
            cum_i += imp
            acc.update(cum_e * 0.8 - cum_i)
        assert (ch[-1], dis[-1]) == (acc.charge, acc.discharge)
        assert ch[-1] > 0 and dis[-1] > 0

    def test_new_system_sums_growth(self):
        ch, dis = flow_history_series([(2.0, 5.0), (1.0, 0.0)], 0.0, False)
        assert ch == [5.0, 5.0]
        assert dis == [2.0, 3.0]

    def test_empty_and_invalid(self):
        assert flow_history_series([], 0.8, True) == ([], [])
        assert flow_history_series(None, 0.8, False) == ([], [])
        # invalid row keeps totals, next valid row anchors (no delta yet)
        ch, dis = flow_history_series([("x", None), (1.0, 2.0)], 0.8, True)
        assert (ch, dis) == ([0.0, 0.0], [0.0, 0.0])


class TestFifoDepositsAndLevel:
    """v0.3.0: deposits total + warehouse fill level %."""

    def test_deposits_counted(self):
        from datetime import date as _date

        from custom_components.energa_mobile.settlement import (
            fifo_kwh_bank,
            warehouse_level_pct,
        )

        flows = [(2025, m, 50.0, 100.0) for m in range(9, 13)]
        flows += [(2026, m, 50.0, 100.0) for m in range(1, 9)]
        bank, detail = fifo_kwh_bank(flows, 0.8, _date(2026, 9, 3))
        assert bank == 360.0
        # 12 live months x 100 x 0.8 credited
        assert detail["deposits_kwh"] == 960.0
        assert warehouse_level_pct(bank, detail["deposits_kwh"]) == 37.5

    def test_level_bounds_and_unknown(self):
        from custom_components.energa_mobile.settlement import (
            warehouse_level_pct,
        )

        assert warehouse_level_pct(0.0, 100.0) == 0.0
        assert warehouse_level_pct(100.0, 100.0) == 100.0
        assert warehouse_level_pct(200.0, 100.0) == 100.0  # capped
        assert warehouse_level_pct(50.0, 0.0) is None
        assert warehouse_level_pct(None, 100.0) is None
        assert warehouse_level_pct(50.0, None) is None
        assert warehouse_level_pct("junk", 100.0) is None

    def test_empty_detail_has_deposits_key(self):
        from custom_components.energa_mobile.settlement import fifo_kwh_bank

        _, detail = fifo_kwh_bank([], 0.8)
        assert detail["deposits_kwh"] == 0.0


class TestOrphanRemovedUids:
    """v0.3.0 removals: Wykryj button + export cost placeholders."""

    def test_button_and_export_costs_doomed(self):
        from custom_components.energa_mobile.settlement import (
            orphan_removed_uids,
        )

        doomed = orphan_removed_uids("310002", "71000001")
        assert "energa_310002_detect_first_data" in doomed
        assert "energa_71000001_export_cost_stats" in doomed
        assert "energa_71000001_export_1_cost_stats" in doomed
        assert "energa_71000001_export_2_cost_stats" in doomed

    def test_import_costs_survive(self):
        from custom_components.energa_mobile.settlement import (
            orphan_removed_uids,
        )

        doomed = orphan_removed_uids("310002", "71000001")
        assert not any("import" in u for u in doomed)


class TestAnchorFlowSeries:
    """v0.3.4: reimports continue from pre-range sums (no reset dips)."""

    def test_fresh_start_at_zero(self):
        from custom_components.energa_mobile.settlement import (
            anchor_flow_series,
        )

        assert anchor_flow_series([0.0, 1.5, 3.0], 0.0) == [
            (0.0, 0.0), (1.5, 1.5), (3.0, 1.5),
        ]

    def test_partial_reimport_continues(self):
        from custom_components.energa_mobile.settlement import (
            anchor_flow_series,
        )

        out = anchor_flow_series([0.0, 2.0], 5889.33)
        assert out[0] == (5889.33, 0.0)  # anchor, no spike
        assert out[1] == (5891.33, 2.0)

    def test_defensive(self):
        from custom_components.energa_mobile.settlement import (
            anchor_flow_series,
        )

        assert anchor_flow_series([], 5.0) == []
        assert anchor_flow_series(None, 5.0) == []
        assert anchor_flow_series([1.0], None)[0][0] == 1.0
        assert anchor_flow_series([1.0], "junk")[0] == (1.0, 1.0)


class TestResetAwareDelta:
    """v0.3.5: monthly/MTD/rolling sums survive a mid-window series reset."""

    def test_monotonic_equals_last_minus_first(self):
        from custom_components.energa_mobile.settlement import (
            reset_aware_delta,
        )

        assert reset_aware_delta([10.0, 12.5, 15.0]) == 5.0

    def test_mid_window_reset_counts_both_sides(self):
        from custom_components.energa_mobile.settlement import (
            reset_aware_delta,
        )

        assert reset_aware_delta([100.0, 105.0, 110.0, 20.0, 25.0]) == 15.0

    def test_reset_on_last_row_counts_flow_before_it(self):
        from custom_components.energa_mobile.settlement import (
            reset_aware_delta,
        )

        # Month-boundary row rewritten with sum 0.0: only August flow counts.
        assert reset_aware_delta([5512.09, 5579.83, 2.68]) == 67.74

    def test_defensive(self):
        from custom_components.energa_mobile.settlement import (
            reset_aware_delta,
        )

        assert reset_aware_delta([]) == 0.0
        assert reset_aware_delta(None) == 0.0
        assert reset_aware_delta([5.0]) == 0.0
        assert reset_aware_delta([7.0, 7.0, 7.0]) == 0.0

    def test_negative_month_clamped_in_fifo(self):
        from custom_components.energa_mobile.settlement import (
            fifo_kwh_bank,
        )

        # A raw negative month (pre-v0.3.5 data) must not corrupt the bank.
        bank, detail = fifo_kwh_bank(
            [(2026, 7, 100.0, 200.0), (2026, 8, -5509.0, -6290.0)], 0.8,
            today=__import__("datetime").date(2026, 9, 4),
        )
        assert bank >= 0.0
        assert detail["deposits_kwh"] == 160.0


class TestBucketFlows:
    """v0.3.9: hourly points land in the right (import, export) slot."""

    def test_single_zone_export_goes_to_slot_1(self):
        from datetime import datetime

        from custom_components.energa_mobile.settlement import bucket_flows

        h1 = datetime(2026, 9, 1, 12)
        h2 = datetime(2026, 9, 1, 13)
        out = bucket_flows(
            [
                ([{"dt": h1, "value": 2.0}, {"dt": h2, "value": 3.0}], 0),
                ([{"dt": h1, "value": 5.0}, {"dt": h2, "value": 7.0}], 1),
            ]
        )
        assert out == [(h1, (2.0, 5.0)), (h2, (3.0, 7.0))]

    def test_zoned_series_share_slots(self):
        from datetime import datetime

        from custom_components.energa_mobile.settlement import bucket_flows

        h = datetime(2026, 9, 1, 12)
        out = bucket_flows(
            [
                ([{"dt": h, "value": 1.0}], 0),
                ([{"dt": h, "value": 2.0}], 0),
                ([{"dt": h, "value": 3.0}], 1),
                ([{"dt": h, "value": 4.0}], 1),
            ]
        )
        assert out == [(h, (3.0, 7.0))]

    def test_guards(self):
        from datetime import datetime

        from custom_components.energa_mobile.settlement import bucket_flows

        h = datetime(2026, 9, 1, 12)
        out = bucket_flows(
            [
                ([{"dt": h, "value": -1.0}, {"dt": h, "value": 500.0}], 0),
                ([{"dt": h, "value": "junk"}], 1),
                ([{"dt": h, "value": 1.0}], 7),
            ],
            max_hourly=100.0,
        )
        assert out == []
