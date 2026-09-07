"""Official PSE OIRE RCE (Rynkowa Cena Energii) dynamic price API client and parser.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 6 & 11 (Etap 5).
Features:
- Pure parser for PSE api/rce-pln 15-minute / 1-hour interval payloads.
- Decimal precision for MWh and kWh market prices.
- UTC interval start and end tracking.
- Asynchronous fetching for target business dates (including D-1 day-ahead prices published at ~14:00).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
from typing import Any

import aiohttp

from .models import MarketPriceRecord
from ...const import PSE_RCE_API_URL

_LOGGER = logging.getLogger(__name__)


def parse_rce_api_payload(
    payload: dict[str, Any],
    source_url: str = PSE_RCE_API_URL,
) -> list[MarketPriceRecord]:
    """Parse JSON response from PSE RCE API into MarketPriceRecord instances.

    Expected JSON structure:
    {
      "value": [
        {
          "dtime": "2024-09-01 00:15:00",
          "period": "00:00 - 00:15",
          "rce_pln": 439.51000,
          "dtime_utc": "2024-08-31 22:15:00",
          "period_utc": "22:00 - 22:15",
          "business_date": "2024-09-01",
          "publication_ts": "2024-08-31 14:01:16.056",
          "publication_ts_utc": "2024-08-31 12:01:16.056000"
        },
        ...
      ]
    }
    """
    if not payload or not isinstance(payload, dict):
        return []

    items = payload.get("value", [])
    if not isinstance(items, list):
        return []

    records: list[MarketPriceRecord] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        raw_price = item.get("rce_pln")
        if raw_price is None:
            continue

        try:
            val_mwh = Decimal(str(raw_price))
            val_kwh = round(val_mwh / Decimal("1000"), 5)
        except (InvalidOperation, TypeError, ValueError):
            continue

        b_date_str = item.get("business_date")
        if not b_date_str:
            continue
        try:
            b_date = date.fromisoformat(b_date_str)
        except ValueError:
            continue

        # Determine interval start and end in UTC
        dtime_utc_str = item.get("dtime_utc")
        period_utc = item.get("period_utc", "")
        start_utc: datetime | None = None
        end_utc: datetime | None = None

        if dtime_utc_str:
            try:
                # Format: "2024-08-31 22:15:00"
                end_naive = datetime.fromisoformat(dtime_utc_str.strip())
                end_utc = end_naive.replace(tzinfo=timezone.utc)

                # Determine duration from period_utc if available (e.g. "22:00 - 22:15" -> 15 min)
                if "-" in period_utc:
                    p_start, p_end = [p.strip() for p in period_utc.split("-")]
                    # Compute minute delta
                    try:
                        h1, m1 = map(int, p_start.split(":"))
                        h2, m2 = map(int, p_end.split(":"))
                        # Handle midnight wrap e.g. 23:45 - 00:00
                        mins = (h2 * 60 + m2) - (h1 * 60 + m1)
                        if mins <= 0:
                            mins += 24 * 60
                        start_utc = end_utc - timedelta(minutes=mins)
                    except Exception:
                        start_utc = end_utc - timedelta(minutes=15)
                else:
                    start_utc = end_utc - timedelta(minutes=15)
            except ValueError:
                pass

        # Publication date
        pub_ts_str = item.get("publication_ts_utc") or item.get("publication_ts")
        if pub_ts_str:
            try:
                pub_date = datetime.fromisoformat(pub_ts_str.split(".")[0].strip()).date()
            except ValueError:
                pub_date = b_date
        else:
            pub_date = b_date

        resolution = "15M" if start_utc and (end_utc - start_utc) <= timedelta(minutes=15) else "1H"

        record = MarketPriceRecord(
            price_type="RCE",
            applicable_year=b_date.year,
            applicable_month=b_date.month,
            publication_date=pub_date,
            revision=1,
            price_mwh=val_mwh,
            price_kwh=val_kwh,
            source_url=source_url,
            is_correction=False,
            raw_snippet=str(item),
            interval_start_utc=start_utc,
            interval_end_utc=end_utc,
            resolution=resolution,
            business_date=b_date,
        )
        records.append(record)

    # Sort chronologically by interval start UTC
    records.sort(
        key=lambda r: (
            r.interval_start_utc or datetime.min.replace(tzinfo=timezone.utc)
        )
    )
    return records


async def async_fetch_rce_day(
    session: aiohttp.ClientSession,
    target_date: date,
    source_url: str = PSE_RCE_API_URL,
) -> list[MarketPriceRecord]:
    """Fetch 15-minute / hourly RCE prices from PSE API for a specific business date."""
    target_str = target_date.isoformat()
    url = f"{source_url}?$filter=business_date eq '{target_str}'"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                _LOGGER.warning("PSE RCE API returned HTTP %d for %s", resp.status, target_str)
                return []
            data = await resp.json()
            records = parse_rce_api_payload(data, source_url=url)
            _LOGGER.debug(
                "Fetched %d RCE records from PSE for business date %s",
                len(records),
                target_str,
            )
            return records
    except aiohttp.ClientError as err:
        _LOGGER.warning("PSE RCE API client error for %s: %s", target_str, err)
        return []
    except Exception as err:
        _LOGGER.warning("PSE RCE API unexpected error for %s: %s", target_str, err)
        return []
