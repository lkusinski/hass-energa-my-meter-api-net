"""SQLite WAL Canonical Storage implementation (pure standard library).

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 4, 5 & 6.
Enforces:
- Append-only logical storage with explicit revisions and observation provenance.
- Amounts stored as Decimal strings (no lossy floats).
- Idempotent upserts: ON CONFLICT DO NOTHING.
- WAL journal mode for safe concurrent reads/writes.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import logging
from pathlib import Path
import sqlite3
from typing import Generator

from ...core.identity.models import PPE, MeterLifecycle, SettlementType
from ...core.readings.models import IntervalReading, ReadingRevision, SourceObservation

_LOGGER = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1

SCHEMA_V1_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ppe (
    ppe_id TEXT PRIMARY KEY,
    customer_label TEXT,
    settlement_type TEXT NOT NULL,
    prosumer_coefficient TEXT NOT NULL,
    timezone TEXT NOT NULL,
    effective_from TEXT,
    effective_to TEXT
);

CREATE TABLE IF NOT EXISTS meter_lifecycle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ppe_id TEXT NOT NULL,
    meter_id TEXT NOT NULL,
    serial TEXT NOT NULL,
    register TEXT NOT NULL,
    zone TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    offset_kwh TEXT NOT NULL,
    source TEXT NOT NULL,
    FOREIGN KEY (ppe_id) REFERENCES ppe (ppe_id)
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_ppe ON meter_lifecycle (ppe_id, register);

CREATE TABLE IF NOT EXISTS source_observation (
    observation_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    raw_payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observation_hash ON source_observation (payload_hash);

CREATE TABLE IF NOT EXISTS interval_reading (
    event_key TEXT NOT NULL,
    source TEXT NOT NULL,
    ppe_id TEXT NOT NULL,
    meter_id TEXT NOT NULL,
    register TEXT NOT NULL,
    interval_start_utc TEXT NOT NULL,
    resolution TEXT NOT NULL,
    import_kwh TEXT NOT NULL,
    export_kwh TEXT NOT NULL,
    quality TEXT NOT NULL,
    revision INTEGER NOT NULL,
    observation_id TEXT,
    PRIMARY KEY (event_key, revision),
    FOREIGN KEY (observation_id) REFERENCES source_observation (observation_id)
);
CREATE INDEX IF NOT EXISTS idx_reading_query ON interval_reading (ppe_id, register, interval_start_utc);

CREATE TABLE IF NOT EXISTS reading_revision (
    event_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    effective_at_utc TEXT NOT NULL,
    supersedes_revision INTEGER,
    reason TEXT,
    provenance TEXT,
    PRIMARY KEY (event_key, revision)
);

CREATE TABLE IF NOT EXISTS job_checkpoint (
    job_name TEXT PRIMARY KEY,
    ppe_id TEXT,
    cursor TEXT,
    last_success_utc TEXT,
    last_error TEXT,
    status TEXT NOT NULL
);
"""


class CanonicalStorage:
    """SQLite-backed canonical storage for energy readings and settlements."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._mem_conn: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:")
            self._mem_conn.row_factory = sqlite3.Row
        self._init_db()

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        if self._mem_conn is not None:
            try:
                yield self._mem_conn
                self._mem_conn.commit()
            except Exception:
                self._mem_conn.rollback()
                raise
            return

        conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Configure PRAGMAs and run migrations."""
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with self._connection() as conn:
            # WAL mode is persistent across connections for file DBs
            if self.db_path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=5000;")

            # Check schema version
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
            )
            cur = conn.execute("SELECT MAX(version) FROM schema_version;")
            row = cur.fetchone()
            current_v = row[0] if row and row[0] is not None else 0

            if current_v < 1:
                conn.executescript(SCHEMA_V1_SQL)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?);",
                    (1, datetime.now(timezone.utc).isoformat()),
                )

    # -------------------------------------------------------------------------
    # Identity: PPE & Meter Lifecycle
    # -------------------------------------------------------------------------

    def upsert_ppe(self, ppe: PPE) -> None:
        """Insert or update a PPE record."""
        sql = """
        INSERT INTO ppe (
            ppe_id, customer_label, settlement_type,
            prosumer_coefficient, timezone, effective_from, effective_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ppe_id) DO UPDATE SET
            customer_label = excluded.customer_label,
            settlement_type = excluded.settlement_type,
            prosumer_coefficient = excluded.prosumer_coefficient,
            timezone = excluded.timezone,
            effective_from = excluded.effective_from,
            effective_to = excluded.effective_to;
        """
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    ppe.ppe_id,
                    ppe.customer_label,
                    ppe.settlement_type.value,
                    str(ppe.prosumer_coefficient),
                    ppe.timezone,
                    ppe.effective_from.isoformat() if ppe.effective_from else None,
                    ppe.effective_to.isoformat() if ppe.effective_to else None,
                ),
            )

    def get_ppe(self, ppe_id: str) -> PPE | None:
        """Fetch PPE by ID."""
        sql = "SELECT * FROM ppe WHERE ppe_id = ?;"
        with self._connection() as conn:
            cur = conn.execute(sql, (ppe_id,))
            row = cur.fetchone()
            if not row:
                return None
            return PPE(
                ppe_id=row["ppe_id"],
                customer_label=row["customer_label"] or "",
                settlement_type=SettlementType(row["settlement_type"]),
                prosumer_coefficient=Decimal(row["prosumer_coefficient"]),
                timezone=row["timezone"],
                effective_from=datetime.fromisoformat(row["effective_from"]).date()
                if row["effective_from"]
                else None,
                effective_to=datetime.fromisoformat(row["effective_to"]).date()
                if row["effective_to"]
                else None,
            )

    def add_meter_lifecycle(self, lifecycle: MeterLifecycle) -> None:
        """Record physical meter attachment/lifecycle boundary."""
        sql = """
        INSERT INTO meter_lifecycle (
            ppe_id, meter_id, serial, register, zone,
            valid_from, valid_to, offset_kwh, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    lifecycle.ppe_id,
                    lifecycle.meter_id,
                    lifecycle.serial,
                    lifecycle.register,
                    lifecycle.zone,
                    lifecycle.valid_from.isoformat() if lifecycle.valid_from else None,
                    lifecycle.valid_to.isoformat() if lifecycle.valid_to else None,
                    str(lifecycle.offset_kwh),
                    lifecycle.source,
                ),
            )

    def get_meter_lifecycles(self, ppe_id: str) -> list[MeterLifecycle]:
        """Fetch all meter lifecycle history for a PPE."""
        sql = "SELECT * FROM meter_lifecycle WHERE ppe_id = ? ORDER BY id ASC;"
        with self._connection() as conn:
            cur = conn.execute(sql, (ppe_id,))
            out = []
            for row in cur.fetchall():
                out.append(
                    MeterLifecycle(
                        ppe_id=row["ppe_id"],
                        meter_id=row["meter_id"],
                        serial=row["serial"],
                        register=row["register"],
                        zone=row["zone"],
                        valid_from=datetime.fromisoformat(row["valid_from"])
                        if row["valid_from"]
                        else None,
                        valid_to=datetime.fromisoformat(row["valid_to"])
                        if row["valid_to"]
                        else None,
                        offset_kwh=Decimal(row["offset_kwh"]),
                        source=row["source"],
                    )
                )
            return out

    # -------------------------------------------------------------------------
    # Observation Archive
    # -------------------------------------------------------------------------

    def save_observation(self, obs: SourceObservation) -> bool:
        """Save raw payload observation. Returns True if newly inserted, False if duplicate."""
        sql = """
        INSERT INTO source_observation (
            observation_id, source, fetched_at_utc, endpoint,
            http_status, payload_hash, raw_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observation_id) DO NOTHING;
        """
        with self._connection() as conn:
            cur = conn.execute(
                sql,
                (
                    obs.observation_id,
                    obs.source,
                    obs.fetched_at_utc.isoformat(),
                    obs.endpoint,
                    obs.http_status,
                    obs.payload_hash,
                    obs.raw_payload,
                ),
            )
            return cur.rowcount > 0

    def has_observation_hash(self, payload_hash: str) -> bool:
        """Check if an identical raw payload hash has already been archived."""
        sql = "SELECT 1 FROM source_observation WHERE payload_hash = ? LIMIT 1;"
        with self._connection() as conn:
            cur = conn.execute(sql, (payload_hash,))
            return cur.fetchone() is not None

    # -------------------------------------------------------------------------
    # Canonical Interval Readings & Revisions
    # -------------------------------------------------------------------------

    def insert_readings_idempotent(self, readings: list[IntervalReading]) -> int:
        """Idempotently insert interval readings.

        ON CONFLICT (event_key, revision) DO NOTHING.
        Returns the count of newly inserted rows.
        """
        if not readings:
            return 0

        sql = """
        INSERT INTO interval_reading (
            event_key, source, ppe_id, meter_id, register,
            interval_start_utc, resolution, import_kwh, export_kwh,
            quality, revision, observation_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key, revision) DO NOTHING;
        """
        params = [
            (
                r.event_key,
                r.source,
                r.ppe_id,
                r.meter_id,
                r.register,
                r.interval_start_utc.isoformat(),
                r.resolution,
                str(r.import_kwh),
                str(r.export_kwh),
                r.quality,
                r.revision,
                r.observation_id,
            )
            for r in readings
        ]
        with self._connection() as conn:
            cur = conn.executemany(sql, params)
            return cur.rowcount

    def get_readings(
        self,
        ppe_id: str,
        register: str | None = None,
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
        resolution: str = "1h",
    ) -> list[IntervalReading]:
        """Fetch canonical readings matching filters, ordered by time ASC."""
        conditions = ["ppe_id = ?", "resolution = ?"]
        params: list[str] = [ppe_id, resolution]

        if register:
            conditions.append("register = ?")
            params.append(register)
        if start_utc:
            conditions.append("interval_start_utc >= ?")
            params.append(start_utc.isoformat())
        if end_utc:
            conditions.append("interval_start_utc < ?")
            params.append(end_utc.isoformat())

        # For multiple revisions of same event_key, pick highest revision
        where_clause = " AND ".join(conditions)
        sql = f"""
        SELECT * FROM interval_reading r
        WHERE {where_clause}
          AND r.revision = (
              SELECT MAX(revision) FROM interval_reading sub
              WHERE sub.event_key = r.event_key
          )
        ORDER BY interval_start_utc ASC;
        """
        with self._connection() as conn:
            cur = conn.execute(sql, params)
            out = []
            for row in cur.fetchall():
                out.append(
                    IntervalReading(
                        ppe_id=row["ppe_id"],
                        meter_id=row["meter_id"],
                        register=row["register"],
                        interval_start_utc=datetime.fromisoformat(
                            row["interval_start_utc"]
                        ),
                        resolution=row["resolution"],
                        import_kwh=Decimal(row["import_kwh"]),
                        export_kwh=Decimal(row["export_kwh"]),
                        quality=row["quality"],
                        source=row["source"],
                        revision=row["revision"],
                        observation_id=row["observation_id"],
                    )
                )
            return out

    # -------------------------------------------------------------------------
    # Resumable Checkpoints
    # -------------------------------------------------------------------------

    def save_checkpoint(
        self,
        job_name: str,
        ppe_id: str | None,
        cursor: str,
        status: str = "in_progress",
        last_error: str | None = None,
    ) -> None:
        """Save a resumable job checkpoint."""
        sql = """
        INSERT INTO job_checkpoint (
            job_name, ppe_id, cursor, last_success_utc, last_error, status
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_name) DO UPDATE SET
            ppe_id = excluded.ppe_id,
            cursor = excluded.cursor,
            last_success_utc = CASE WHEN excluded.status = 'completed' THEN excluded.last_success_utc ELSE job_checkpoint.last_success_utc END,
            last_error = excluded.last_error,
            status = excluded.status;
        """
        now_str = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                sql,
                (job_name, ppe_id, cursor, now_str, last_error, status),
            )

    def get_checkpoint(self, job_name: str) -> dict | None:
        """Fetch checkpoint for a job."""
        sql = "SELECT * FROM job_checkpoint WHERE job_name = ?;"
        with self._connection() as conn:
            cur = conn.execute(sql, (job_name,))
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)
