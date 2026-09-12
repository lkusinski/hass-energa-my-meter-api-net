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
from html.parser import HTMLParser
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


class _PSETableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self.current_table: list[list[str]] = []
        self.current_row: list[str] = []
        self.current_cell: list[str] = []
        self.in_cell: bool = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.current_table = []
        elif tag == "tr":
            self.current_row = []
        elif tag in ("td", "th"):
            self.current_cell = []
            self.in_cell = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self.in_cell = False
            cell_text = "".join(self.current_cell).replace("\xa0", " ").strip()
            self.current_row.append(cell_text)
        elif tag == "tr":
            if any(c for c in self.current_row):
                self.current_table.append(self.current_row)
            self.current_row = []
        elif tag == "table":
            if self.current_table:
                self.tables.append(self.current_table)
                self.current_table = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_cell.append(data)


_DATE_RE = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})\s*$")
_PRICE_RE = re.compile(r"^\s*([-\d\s]+[.,]\d+)\s*$")
_YEAR_RE = re.compile(r"\b(202\d)\b")
_KOREKTA_NUM_RE = re.compile(r"korekta\s*(\d+)", re.IGNORECASE)


def parse_rcem_html(
    html: str,
    source_url: str = "https://www.pse.pl/oire/rcem-rynkowa-miesieczna-cena-energii-elektrycznej",
) -> list[MarketPriceRecord]:
    """Parse PSE RCEm HTML tables into MarketPriceRecord instances.

    Handles official PSE HTML structure:
    - Multiple tables divided by year (2022-2026).
    - Months defined in separate header rows or inline.
    - Initial publications (RCEm) and corrections (skorygowana RCEm* / RCEm korekta N).
    - Robust isolation per row (no cross-month regex bleeding).
    """
    if not html:
        return []

    parser = _PSETableParser()
    parser.feed(html)

    records: list[MarketPriceRecord] = []
    rev_counters: dict[tuple[int, int], int] = {}

    for table in parser.tables:
        table_year: int | None = None
        current_month: int | None = None

        for row in table:
            row_text = " ".join(row).strip()
            if not row_text:
                continue

            # 1. Detect year in short header rows (e.g. ['2026'])
            if len(row) <= 2:
                ym = _YEAR_RE.search(row_text)
                if ym and not any(m in row_text.lower() for m in PSE_MONTH_MAP):
                    table_year = int(ym.group(1))
                    continue

            # 2. Detect month in row
            found_month: int | None = None
            found_month_year: int | None = None
            for cell in row:
                cell_lower = cell.lower()
                for m_name, m_num in PSE_MONTH_MAP.items():
                    if re.search(rf"\b{m_name}\b", cell_lower):
                        found_month = m_num
                        ym = _YEAR_RE.search(cell)
                        if ym:
                            found_month_year = int(ym.group(1))
                        break
                if found_month:
                    break

            if len(row) == 1 and found_month:
                current_month = found_month
                if found_month_year:
                    table_year = found_month_year
                continue

            has_rcem = any("rcem" in c.lower() or "skorygowan" in c.lower() for c in row)
            price_cell: str | None = None
            date_cell: str | None = None

            for c in row:
                if c == "-":
                    continue
                if _DATE_RE.match(c):
                    date_cell = c
                elif _PRICE_RE.match(c) and "%" not in c and not price_cell:
                    price_cell = c

            if not has_rcem or not date_cell or not price_cell:
                if found_month:
                    current_month = found_month
                    if found_month_year:
                        table_year = found_month_year
                continue

            applicable_month = found_month or current_month
            if not applicable_month:
                continue

            dm = _DATE_RE.match(date_cell)
            if not dm:
                continue
            try:
                pub_date = date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
            except (ValueError, TypeError):
                continue

            if found_month_year:
                applicable_year = found_month_year
            elif table_year:
                applicable_year = table_year
            elif applicable_month == 12 and pub_date.month == 1:
                applicable_year = pub_date.year - 1
            elif applicable_month > pub_date.month:
                applicable_year = pub_date.year - 1
            else:
                applicable_year = pub_date.year

            clean_price = price_cell.replace(" ", "").replace(",", ".")
            try:
                val_mwh = Decimal(clean_price)
            except InvalidOperation:
                continue
            val_kwh = round(val_mwh / Decimal("1000"), 5)

            row_lower = row_text.lower()
            is_correction = bool("skorygowan" in row_lower or "korekta" in row_lower)
            k_num_match = _KOREKTA_NUM_RE.search(row_text)

            key = (applicable_year, applicable_month)
            if k_num_match:
                revision = int(k_num_match.group(1))
                rev_counters[key] = max(rev_counters.get(key, 1), revision)
            elif is_correction:
                rev = rev_counters.get(key, 1) + 1
                rev_counters[key] = rev
                revision = rev
            else:
                revision = 1
                rev_counters[key] = 1

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
                    raw_snippet=row_text,
                )
            )

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
