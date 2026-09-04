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
