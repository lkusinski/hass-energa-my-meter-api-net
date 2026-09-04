"""Domain models for readings, observations, and revisions (pure domain).

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 4 & 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib


@dataclass(frozen=True)
class IntervalReading:
    """Canonical interval reading.

    Amounts are always Decimal, never float.
    Time is canonically in UTC.
    """

    ppe_id: str
    meter_id: str
    register: str
    interval_start_utc: datetime
    resolution: str = "1h"                  # "15min", "1h", "1d"
    import_kwh: Decimal = Decimal("0.0")
    export_kwh: Decimal = Decimal("0.0")
    quality: str = "ok"                     # "ok", "estimated", "missing", "uncertain"
    source: str = "energa"
    revision: int = 1
    observation_id: str | None = None

    @property
    def event_key(self) -> str:
        """Deterministic event key for deduplication and event identification."""
        dt_str = self.interval_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{self.source}:{self.ppe_id}:{self.meter_id}:{self.register}:{dt_str}:{self.resolution}"

    @property
    def revision_key(self) -> str:
        """Deterministic revision key."""
        return f"{self.event_key}:rev{self.revision}"


@dataclass(frozen=True)
class SourceObservation:
    """Raw payload observation archive for auditability and provenance."""

    observation_id: str
    source: str
    fetched_at_utc: datetime
    endpoint: str
    http_status: int
    payload_hash: str
    raw_payload: str

    @classmethod
    def create(
        cls,
        source: str,
        endpoint: str,
        http_status: int,
        raw_payload: str,
        fetched_at_utc: datetime | None = None,
    ) -> SourceObservation:
        """Construct observation computing its sha256 hash and unique ID."""
        now = fetched_at_utc or datetime.now(timezone.utc)
        payload_hash = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
        obs_id = f"obs_{source}_{payload_hash[:16]}_{int(now.timestamp())}"
        return cls(
            observation_id=obs_id,
            source=source,
            fetched_at_utc=now,
            endpoint=endpoint,
            http_status=http_status,
            payload_hash=payload_hash,
            raw_payload=raw_payload,
        )


@dataclass(frozen=True)
class ReadingRevision:
    """Audit log record of a reading revision or correction."""

    event_key: str
    revision: int
    effective_at_utc: datetime
    supersedes_revision: int | None = None
    reason: str = "initial"
    provenance: str = ""
