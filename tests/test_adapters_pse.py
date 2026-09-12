"""Unit tests for PSE RCEm adapter and parser.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 5, 7 & 8.
Verifies:
- Explicit month parsing (no naive pub_month - 1).
- Correction and revision handling.
- Decimal precision.
- Historical cutoff filtering.
"""

from datetime import date
from decimal import Decimal
import pytest


from custom_components.energa_mobile.adapters.pse.rcem_parser import (
    get_effective_rcem,
    parse_rcem_html,
)

SAMPLE_PSE_HTML = """
<table>
    <tr>
        <td><b>lipiec</b></td>
        <td>RCEm&nbsp;</td>
        <td align="right">262,88</td>
        <td align="center">12.08.2026</td>
    </tr>
    <tr>
        <td><b>czerwiec</b></td>
        <td>RCEm&nbsp;</td>
        <td align="right">323,34</td>
        <td align="center">11.07.2026</td>
    </tr>
    <tr>
        <td><b>czerwiec</b></td>
        <td>RCEm korekta 2&nbsp;</td>
        <td align="right">325,10</td>
        <td align="center">25.07.2026</td>
    </tr>
    <tr>
        <td><b>grudzień</b></td>
        <td>RCEm&nbsp;</td>
        <td align="right">410,50</td>
        <td align="center">13.01.2026</td>
    </tr>
</table>
"""


def test_parse_explicit_month_and_decimal():
    records = parse_rcem_html(SAMPLE_PSE_HTML)
    assert len(records) == 4

    # Check July 2026
    july = next(r for r in records if r.applicable_month == 7)
    assert july.applicable_year == 2026
    assert july.price_mwh == Decimal("262.88")
    assert july.price_kwh == Decimal("0.26288")
    assert july.is_correction is False
    assert july.revision == 1

    # Check December (published in Jan 2026 -> must be Dec 2025!)
    dec = next(r for r in records if r.applicable_month == 12)
    assert dec.applicable_year == 2025
    assert dec.price_mwh == Decimal("410.50")
    assert dec.price_kwh == Decimal("0.41050")


def test_rcem_correction_and_as_of_filtering():
    records = parse_rcem_html(SAMPLE_PSE_HTML)

    # In June 2026: initial was published on 11.07.2026 (323.34),
    # and correction 2 on 25.07.2026 (325.10).

    # If evaluated as of 2026-07-15 (before correction):
    eff_early = get_effective_rcem(records, 2026, 6, as_of=date(2026, 7, 15))
    assert eff_early is not None
    assert eff_early.revision == 1
    assert eff_early.price_kwh == Decimal("0.32334")

    # If evaluated as of 2026-08-01 (after correction):
    eff_late = get_effective_rcem(records, 2026, 6, as_of=date(2026, 8, 1))
    assert eff_late is not None
    assert eff_late.revision == 2
    assert eff_late.price_kwh == Decimal("0.32510")
    assert eff_late.is_correction is True


@pytest.mark.asyncio
async def test_async_fetch_rce_day():
    """Verify async fetching of daily RCE records via PSE API."""
    from unittest.mock import AsyncMock, MagicMock
    from custom_components.energa_mobile.adapters.pse.rce_client import async_fetch_rce_day

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "value": [
            {
                "dtime": "2026-09-01 00:15:00",
                "period": "00:00 - 00:15",
                "rce_pln": 520.0,
                "dtime_utc": "2026-08-31 22:15:00",
                "period_utc": "22:00 - 22:15",
                "business_date": "2026-09-01",
                "publication_ts_utc": "2026-08-31 12:01:00",
            }
        ]
    })

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_resp

    records = await async_fetch_rce_day(mock_session, date(2026, 9, 1))
    assert len(records) == 1
    assert records[0].price_kwh == Decimal("0.520")
    assert records[0].business_date == date(2026, 9, 1)


REAL_PSE_2026_SNIPPET = """
<table border="1">
    <thead>
        <tr><th colspan="4"><strong>2026</strong></th></tr>
        <tr><th></th><th>cena [zł/MWh]**</th><th>data publikacji</th><th>różnica skorygowanej RCEm od poprzednio obliczonej ceny [%]</th></tr>
    </thead>
    <tbody>
        <tr><td colspan="4"><strong>styczeń</strong></td></tr>
        <tr><td>RCEm</td><td align="right">551,96</td><td align="center">11.02.2026</td><td align="right">-</td></tr>
        <tr><td>skorygowana RCEm*</td><td align="right">-</td><td align="center">-</td><td align="right">-</td></tr>
        <tr><td colspan="4"><strong>luty</strong></td></tr>
        <tr><td>RCEm</td><td align="right">339,01</td><td align="center">11.03.2026</td><td align="right">-</td></tr>
        <tr><td>skorygowana RCEm*</td><td align="right">331,39</td><td align="center">11.06.2026</td><td align="right">- 2,25</td></tr>
        <tr><td colspan="4"><strong>marzec</strong></td></tr>
        <tr><td>RCEm</td><td align="right">191,95</td><td align="center">11.04.2026</td><td align="right">-</td></tr>
        <tr><td>skorygowana RCEm*</td><td align="right">-</td><td align="center">-</td><td align="right">-</td></tr>
        <tr><td colspan="4"><strong>kwiecień</strong></td></tr>
        <tr><td>RCEm</td><td align="right">132,92</td><td align="center">11.05.2026</td><td align="right">-</td></tr>
        <tr><td>skorygowana RCEm*</td><td align="right">-</td><td align="center">-</td><td align="right">-</td></tr>
        <tr><td colspan="4"><strong>maj</strong></td></tr>
        <tr><td>RCEm</td><td align="right">191,37</td><td align="center">11.06.2026</td><td align="right">-</td></tr>
        <tr><td>skorygowana RCEm*</td><td align="right">-</td><td align="center">-</td><td align="right">-</td></tr>
    </tbody>
</table>
"""


def test_pse_table_structure_and_corrections():
    """Verify bugfix for Issue #1:
    - Tables with year in header
    - Dedicated month header rows
    - 'skorygowana RCEm*' recognized as correction
    - Missing &nbsp; does not jump rows (April != May)
    - Feb 2026 correction captured
    """
    records = parse_rcem_html(REAL_PSE_2026_SNIPPET)
    # Expected: Jan (1), Feb (2), Mar (1), Apr (1), May (1) = 6 records
    assert len(records) == 6

    # 1. February initial + correction
    feb_records = [r for r in records if r.applicable_year == 2026 and r.applicable_month == 2]
    assert len(feb_records) == 2
    assert feb_records[0].price_mwh == Decimal("339.01")
    assert feb_records[0].revision == 1
    assert feb_records[0].is_correction is False

    assert feb_records[1].price_mwh == Decimal("331.39")
    assert feb_records[1].revision == 2
    assert feb_records[1].is_correction is True
    assert feb_records[1].publication_date == date(2026, 6, 11)

    # 2. April 2026 vs May 2026 isolation
    apr = next(r for r in records if r.applicable_year == 2026 and r.applicable_month == 4)
    may = next(r for r in records if r.applicable_year == 2026 and r.applicable_month == 5)

    assert apr.price_mwh == Decimal("132.92")
    assert apr.price_kwh == Decimal("0.13292")
    assert apr.publication_date == date(2026, 5, 11)

    assert may.price_mwh == Decimal("191.37")
    assert may.price_kwh == Decimal("0.19137")
    assert may.publication_date == date(2026, 6, 11)


def test_pse_full_real_page_if_available():
    import os
    real_html_path = "/tmp/pse_rcem.html"
    if not os.path.exists(real_html_path):
        pytest.skip("Real PSE HTML dump not found")
    with open(real_html_path, encoding="utf-8") as f:
        html = f.read()
    records = parse_rcem_html(html)
    assert len(records) == 94
    corrections = [r for r in records if r.is_correction]
    assert len(corrections) == 43
    years = {r.applicable_year for r in records}
    assert years == {2022, 2023, 2024, 2025, 2026}



