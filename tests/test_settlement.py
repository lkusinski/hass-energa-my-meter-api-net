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
    is_export_prosumer,
    latest_official_rcem,
    month_to_date_forecast,
    next_settlement_date,
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

    Consumer meters (e.g. G11 bez fotowoltaiki without PV) may still report
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
