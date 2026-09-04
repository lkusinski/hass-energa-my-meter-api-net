"""Energa API Client adhering to target acquisition contract.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 5 & 6.
Invariants:
- Returns raw payload (SourceObservation) alongside normalized domain records (IntervalReading).
- Semi-open time ranges [start, end) (no hardcoded YYYY-MM-31).
- Retry with jitter for 429/5xx status codes.
- Preserves raw API response for full provenance and auditability.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import random
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ...core.readings.models import IntervalReading, SourceObservation

_LOGGER = logging.getLogger(__name__)

WARSAW_TZ = ZoneInfo("Europe/Warsaw")
BASE_URL = "https://api-mojlicznik.energa-operator.pl/dp"


class EnergaAdapterError(Exception):
    """Base exception for Energa API adapter."""


class EnergaRateLimitError(EnergaAdapterError):
    """429 Rate limited."""


def normalize_chart_payload(
    raw_json_str: str,
    endpoint: str,
    ppe_id: str,
    meter_id: str,
    register: str = "1.8.0",
    resolution: str = "1h",
) -> tuple[SourceObservation, list[IntervalReading]]:
    """Normalize a raw Energa chart response into an Observation and IntervalReadings.

    Extracts hourly readings with UTC timestamps and Decimal kWh values.
    Preserves raw JSON text in SourceObservation for provenance.
    """
    obs = SourceObservation.create(
        source="energa",
        endpoint=endpoint,
        http_status=200,
        raw_payload=raw_json_str,
    )

    readings: list[IntervalReading] = []
    try:
        data = json.loads(raw_json_str)
    except json.JSONDecodeError as err:
        _LOGGER.warning("Failed to decode Energa chart JSON: %s", err)
        return obs, []

    # API returns list of points or dict with "response" / "points"
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("response") or data.get("points") or []

    is_export = "export" in register or register.startswith("2.8")

    for item in items:
        if not isinstance(item, dict):
            continue

        # Extract timestamp: may be 'time', 'timestamp', 'date', or epoch in ms
        dt_val = None
        if "time" in item:
            # Epoch milliseconds or ISO string
            t_val = item["time"]
            if isinstance(t_val, (int, float)):
                dt_val = datetime.fromtimestamp(t_val / 1000.0, tz=timezone.utc)
            elif isinstance(t_val, str):
                try:
                    dt_val = datetime.fromisoformat(t_val.replace("Z", "+00:00"))
                except ValueError:
                    pass
        elif "date" in item and "hour" in item:
            # e.g. "2026-09-04", hour: 12
            try:
                d_part = date.fromisoformat(item["date"])
                h_part = int(item["hour"]) - 1  # 1-indexed to 0-indexed
                local_dt = datetime(d_part.year, d_part.month, d_part.day, h_part, 0, tzinfo=WARSAW_TZ)
                dt_val = local_dt.astimezone(timezone.utc)
            except (ValueError, TypeError):
                pass

        if not dt_val:
            continue

        # Extract kWh value
        val = item.get("value") or item.get("val") or item.get("kwh")
        if val is None:
            continue

        try:
            val_dec = Decimal(str(round(float(val), 4)))
        except (ValueError, TypeError):
            continue

        if val_dec < Decimal("0.0"):
            val_dec = Decimal("0.0")

        readings.append(
            IntervalReading(
                ppe_id=ppe_id,
                meter_id=meter_id,
                register=register,
                interval_start_utc=dt_val,
                resolution=resolution,
                import_kwh=Decimal("0.0") if is_export else val_dec,
                export_kwh=val_dec if is_export else Decimal("0.0"),
                quality="ok",
                source="energa",
                observation_id=obs.observation_id,
            )
        )

    return obs, readings


class EnergaApiClient:
    """Async client for Energa Mój Licznik API with retry and rate-limiting."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = BASE_URL,
        max_retries: int = 3,
        request_timeout: float = 25.0,
    ) -> None:
        self.session = session
        self.base_url = base_url
        self.max_retries = max_retries
        self.timeout = aiohttp.ClientTimeout(total=request_timeout)

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        json_data: Any = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, str]:
        """Execute HTTP request with exponential backoff and jitter for 429/5xx."""
        url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
        attempt = 0

        while True:
            attempt += 1
            try:
                async with self.session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_data,
                    params=params,
                    timeout=self.timeout,
                ) as resp:
                    text = await resp.text()

                    if resp.status == 429:
                        if attempt > self.max_retries:
                            raise EnergaRateLimitError(f"HTTP 429 Rate limit exceeded at {url}")
                        retry_after = resp.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after else (2.0 ** attempt) + random.uniform(0.1, 1.0)
                        _LOGGER.warning("Rate limited (429), backing off for %.2fs", delay)
                        await asyncio.sleep(delay)
                        continue

                    if resp.status >= 500:
                        if attempt > self.max_retries:
                            raise EnergaAdapterError(f"HTTP {resp.status} Server Error at {url}")
                        delay = (1.5 ** attempt) + random.uniform(0.1, 0.5)
                        _LOGGER.warning("Server error (%d), retrying in %.2fs", resp.status, delay)
                        await asyncio.sleep(delay)
                        continue

                    return resp.status, text

            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                if attempt > self.max_retries:
                    raise EnergaAdapterError(f"Connection error to {url}: {err}") from err
                delay = (1.5 ** attempt) + random.uniform(0.1, 0.5)
                await asyncio.sleep(delay)
