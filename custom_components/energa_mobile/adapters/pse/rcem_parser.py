"""Robust parser for official PSE RCEm market prices (pure Python standard library).

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 5 & 8.
Solves P1:
- Applicable month is parsed explicitly from the row text (month name), NOT inferred as publication_month - 1.
- Supports multiple revisions/corrections per month.
- Converts to Decimal without floating-point inaccuracies.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

from .models import MarketPriceRecord

PSE_MONTH_MAP = {
    "styczeń": 1, "stycznia": 1,
    "luty": 2, "lutego": 2,
    "marzec": 3, "marca": 3,
    "kwiecień": 4, "kwietnia": 4,
    "maj": 5, "maja": 5,
    "czerwiec": 6, "czerwca": 6,
    "lipiec": 7, "lipca": 7,
    "sierpień": 8, "sierpnia": 8,
    "wrzesień": 9, "września": 9,
    "październik": 10, "października": 10,
    "listopad": 11, "listopada": 11,
    "grudzień": 12, "grudnia": 12,
}


def parse_rcem_html(
    html: str,
    source_url: str = "https://www.pse.pl/oire/rcem-rynkowa-miesieczna-cena-energii-elektrycznej",
) -> list[MarketPriceRecord]:
    """Parse PSE RCEm HTML table into MarketPriceRecord instances.

    Each row in the PSE table has the month name, indicator name, price in PLN/MWh,
    and publication date DD.MM.YYYY.
    """
    if not html:
        return []

    pattern = re.compile(
        r"<b>\s*([A-Za-zżźćńółęąśŻŹĆĄŚĘŁÓŃ]+)\s*(?:(\d{4}))?\s*</b>"
        r"[\s\S]*?RCEm(?:\s*korekta\s*(\d+)?)?&nbsp;[\s\S]*?"
        r'<td align="right">\s*([-\d\s]+[.,]\d+)\s*</td>\s*'
        r'<td align="center">\s*(\d{2})\.(\d{2})\.(\d{4})\s*</td>',
        re.IGNORECASE,
    )

    records: list[MarketPriceRecord] = []

    for match in pattern.finditer(html):
        month_str, explicit_year_str, corr_rev, raw_val, pd, pm, py = match.groups()
        month_norm = month_str.lower().strip()
        applicable_month = PSE_MONTH_MAP.get(month_norm)
        if not applicable_month:
            continue

        try:
            pub_date = date(int(py), int(pm), int(pd))
        except (ValueError, TypeError):
            continue

        # Determine applicable year:
        # If explicitly stated in the month column (e.g. "grudzień 2024"), use it.
        # Otherwise: if applicable_month == 12 and pub_date.month == 1, year = pub_date.year - 1;
        # otherwise year = pub_date.year.
        if explicit_year_str:
            applicable_year = int(explicit_year_str)
        elif applicable_month == 12 and pub_date.month == 1:
            applicable_year = pub_date.year - 1
        elif applicable_month > pub_date.month:
            applicable_year = pub_date.year - 1
        else:
            applicable_year = pub_date.year

        # Clean up price string (remove spaces, replace comma with dot)
        clean_val = raw_val.replace(" ", "").replace(",", ".")
        try:
            val_mwh = Decimal(clean_val)
        except InvalidOperation:
            continue

        val_kwh = round(val_mwh / Decimal("1000"), 5)

        is_correction = bool("korekta" in match.group(0).lower())
        revision = int(corr_rev) if corr_rev else (2 if is_correction else 1)

        records.append(
            MarketPriceRecord(
                price_type="RCEM",
                applicable_year=applicable_year,
                applicable_month=applicable_month,
                publication_date=pub_date,
                revision=revision,
                price_mwh=val_mwh,
                price_kwh=val_kwh,
                source_url=source_url,
                is_correction=is_correction,
                raw_snippet=match.group(0),
            )
        )

    # Sort so that latest revision / publication date is last
    records.sort(key=lambda r: (r.applicable_year, r.applicable_month, r.publication_date, r.revision))
    return records


def get_effective_rcem(
    records: list[MarketPriceRecord],
    year: int,
    month: int,
    as_of: date | None = None,
) -> MarketPriceRecord | None:
    """Get the effective (latest valid) RCEm price for a specific month as of a given date."""
    cutoff = as_of or date.today()
    candidates = [
        r for r in records
        if r.applicable_year == year
        and r.applicable_month == month
        and r.publication_date <= cutoff
    ]
    if not candidates:
        return None
    # Highest revision / latest published
    return max(candidates, key=lambda r: (r.publication_date, r.revision))
