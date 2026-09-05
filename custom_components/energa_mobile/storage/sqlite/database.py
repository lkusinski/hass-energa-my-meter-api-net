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
from datetime import date, datetime, timezone
from decimal import Decimal
import logging
from pathlib import Path
import sqlite3
from typing import Generator

from ...core.identity.models import PPE, MeterLifecycle, SettlementType
from ...core.readings.models import IntervalReading, ReadingRevision, SourceObservation
from ...adapters.pse.models import MarketPriceRecord
from ...core.settlement.models import LotAllocation, SettlementLot
from ...core.tariffs.models import InvoiceReconciliation

_LOGGER = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2

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

SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS market_price (
    price_id TEXT PRIMARY KEY,
    price_type TEXT NOT NULL,
    applicable_year INTEGER NOT NULL,
    applicable_month INTEGER NOT NULL,
    interval_start_utc TEXT,
    resolution TEXT NOT NULL,
    publication_date TEXT NOT NULL,
    revision INTEGER NOT NULL,
    price_mwh TEXT NOT NULL,
    price_kwh TEXT NOT NULL,
    source_url TEXT NOT NULL,
    is_correction INTEGER NOT NULL,
    raw_snippet TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_price_lookup 
ON market_price (price_type, applicable_year, applicable_month, revision);

CREATE TABLE IF NOT EXISTS settlement_lot (
    lot_id TEXT PRIMARY KEY,
    ppe_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    zone TEXT NOT NULL,
    original_amount TEXT NOT NULL,
    remaining_amount TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    provenance TEXT,
    FOREIGN KEY (ppe_id) REFERENCES ppe (ppe_id)
);
CREATE INDEX IF NOT EXISTS idx_settlement_lot_query 
ON settlement_lot (ppe_id, unit, expires_at);

CREATE TABLE IF NOT EXISTS settlement_allocation (
    allocation_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL,
    consumption_target_id TEXT NOT NULL,
    allocated_amount TEXT NOT NULL,
    allocated_at_utc TEXT NOT NULL,
    is_reversal INTEGER NOT NULL,
    notes TEXT,
    FOREIGN KEY (lot_id) REFERENCES settlement_lot (lot_id)
);
CREATE INDEX IF NOT EXISTS idx_settlement_alloc_lot 
ON settlement_allocation (lot_id);

CREATE TABLE IF NOT EXISTS invoice_reconciliation (
    invoice_number TEXT PRIMARY KEY,
    ppe_id TEXT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    computed_gross TEXT NOT NULL,
    invoiced_gross TEXT NOT NULL,
    variance_gross TEXT NOT NULL,
    variance_percent TEXT NOT NULL,
    status TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    approved_by TEXT,
    approved_at_utc TEXT,
    notes TEXT,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_reconciliation_line (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number TEXT NOT NULL,
    rate_id TEXT NOT NULL,
    name TEXT NOT NULL,
    computed_gross TEXT NOT NULL,
    invoiced_gross TEXT,
    diff_gross TEXT NOT NULL,
    FOREIGN KEY (invoice_number) REFERENCES invoice_reconciliation (invoice_number)
);
CREATE INDEX IF NOT EXISTS idx_recon_lines ON invoice_reconciliation_line (invoice_number);
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
                current_v = 1

            if current_v < 2:
                conn.executescript(SCHEMA_V2_SQL)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?);",
                    (2, datetime.now(timezone.utc).isoformat()),
                )
                current_v = 2

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

    # -------------------------------------------------------------------------
    # Market Prices (PSE RCEm & RCE)
    # -------------------------------------------------------------------------

    def save_market_prices(self, prices: list[MarketPriceRecord]) -> int:
        """Save market prices idempotently with revisions."""
        if not prices:
            return 0
        sql = """
        INSERT INTO market_price (
            price_id, price_type, applicable_year, applicable_month,
            interval_start_utc, resolution, publication_date, revision,
            price_mwh, price_kwh, source_url, is_correction, raw_snippet
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(price_id) DO UPDATE SET
            price_mwh = excluded.price_mwh,
            price_kwh = excluded.price_kwh,
            publication_date = excluded.publication_date,
            is_correction = excluded.is_correction,
            source_url = excluded.source_url,
            raw_snippet = excluded.raw_snippet;
        """
        params = []
        for p in prices:
            pid = f"{p.price_type}_{p.applicable_year}_{p.applicable_month:02d}_rev{p.revision}"
            params.append((
                pid,
                p.price_type,
                p.applicable_year,
                p.applicable_month,
                None,
                "1M",
                p.publication_date.isoformat(),
                p.revision,
                str(p.price_mwh),
                str(p.price_kwh),
                p.source_url,
                1 if p.is_correction else 0,
                p.raw_snippet,
            ))
        with self._connection() as conn:
            cur = conn.executemany(sql, params)
            return cur.rowcount

    def get_market_prices(
        self,
        price_type: str,
        year: int | None = None,
        month: int | None = None,
    ) -> list[MarketPriceRecord]:
        """Fetch market prices matching criteria."""
        conditions = ["price_type = ?"]
        params: list[str | int] = [price_type]
        if year is not None:
            conditions.append("applicable_year = ?")
            params.append(year)
        if month is not None:
            conditions.append("applicable_month = ?")
            params.append(month)

        where_clause = " AND ".join(conditions)
        sql = f"""
        SELECT * FROM market_price
        WHERE {where_clause}
        ORDER BY applicable_year ASC, applicable_month ASC, revision ASC;
        """
        with self._connection() as conn:
            cur = conn.execute(sql, params)
            out = []
            for row in cur.fetchall():
                out.append(
                    MarketPriceRecord(
                        price_type=row["price_type"],
                        applicable_year=row["applicable_year"],
                        applicable_month=row["applicable_month"],
                        publication_date=date.fromisoformat(row["publication_date"]),
                        revision=row["revision"],
                        price_mwh=Decimal(row["price_mwh"]),
                        price_kwh=Decimal(row["price_kwh"]),
                        source_url=row["source_url"],
                        is_correction=bool(row["is_correction"]),
                        raw_snippet=row["raw_snippet"] or "",
                    )
                )
            return out

    def get_effective_market_price(
        self,
        price_type: str,
        year: int,
        month: int,
        as_of: date | None = None,
    ) -> MarketPriceRecord | None:
        """Fetch the latest published revision of a price for a given month as of date."""
        cutoff = (as_of or date.today()).isoformat()
        sql = """
        SELECT * FROM market_price
        WHERE price_type = ?
          AND applicable_year = ?
          AND applicable_month = ?
          AND publication_date <= ?
        ORDER BY publication_date DESC, revision DESC
        LIMIT 1;
        """
        with self._connection() as conn:
            cur = conn.execute(sql, (price_type, year, month, cutoff))
            row = cur.fetchone()
            if not row:
                return None
            return MarketPriceRecord(
                price_type=row["price_type"],
                applicable_year=row["applicable_year"],
                applicable_month=row["applicable_month"],
                publication_date=date.fromisoformat(row["publication_date"]),
                revision=row["revision"],
                price_mwh=Decimal(row["price_mwh"]),
                price_kwh=Decimal(row["price_kwh"]),
                source_url=row["source_url"],
                is_correction=bool(row["is_correction"]),
                raw_snippet=row["raw_snippet"] or "",
            )

    # -------------------------------------------------------------------------
    # Settlement Lots & Allocations (Physical kWh & Monetary PLN Ledgers)
    # -------------------------------------------------------------------------

    def save_settlement_lots(self, lots: list[SettlementLot]) -> int:
        """Save settlement lots idempotently."""
        if not lots:
            return 0
        sql = """
        INSERT INTO settlement_lot (
            lot_id, ppe_id, unit, zone, original_amount, remaining_amount,
            created_at_utc, assigned_at, expires_at, rule_version, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lot_id) DO UPDATE SET
            remaining_amount = excluded.remaining_amount,
            rule_version = excluded.rule_version,
            provenance = excluded.provenance;
        """
        params = [
            (
                l.lot_id,
                l.ppe_id,
                l.unit,
                l.zone,
                str(l.original_amount),
                str(l.remaining_amount),
                l.created_at_utc.isoformat(),
                l.assigned_at.isoformat(),
                l.expires_at.isoformat(),
                l.rule_version,
                l.provenance,
            )
            for l in lots
        ]
        with self._connection() as conn:
            cur = conn.executemany(sql, params)
            return cur.rowcount

    def get_settlement_lots(
        self,
        ppe_id: str,
        unit: str | None = None,
        active_only: bool = False,
    ) -> list[SettlementLot]:
        """Fetch settlement lots for a PPE."""
        conditions = ["ppe_id = ?"]
        params: list[str] = [ppe_id]
        if unit:
            conditions.append("unit = ?")
            params.append(unit)
        if active_only:
            conditions.append("CAST(remaining_amount AS NUMERIC) > 0")

        where_clause = " AND ".join(conditions)
        sql = f"""
        SELECT * FROM settlement_lot
        WHERE {where_clause}
        ORDER BY assigned_at ASC, lot_id ASC;
        """
        with self._connection() as conn:
            cur = conn.execute(sql, params)
            out = []
            for row in cur.fetchall():
                out.append(
                    SettlementLot(
                        lot_id=row["lot_id"],
                        ppe_id=row["ppe_id"],
                        unit=row["unit"],
                        zone=row["zone"],
                        original_amount=Decimal(row["original_amount"]),
                        remaining_amount=Decimal(row["remaining_amount"]),
                        created_at_utc=datetime.fromisoformat(row["created_at_utc"]),
                        assigned_at=date.fromisoformat(row["assigned_at"]),
                        expires_at=date.fromisoformat(row["expires_at"]),
                        rule_version=row["rule_version"],
                        provenance=row["provenance"] or "",
                    )
                )
            return out

    def save_lot_allocations(self, allocations: list[LotAllocation]) -> int:
        """Save lot allocations."""
        if not allocations:
            return 0
        sql = """
        INSERT INTO settlement_allocation (
            allocation_id, lot_id, consumption_target_id,
            allocated_amount, allocated_at_utc, is_reversal, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(allocation_id) DO NOTHING;
        """
        params = [
            (
                a.allocation_id,
                a.lot_id,
                a.consumption_target_id,
                str(a.allocated_amount),
                a.allocated_at_utc.isoformat(),
                1 if a.is_reversal else 0,
                a.notes,
            )
            for a in allocations
        ]
        with self._connection() as conn:
            cur = conn.executemany(sql, params)
            return cur.rowcount

    def get_lot_allocations(self, lot_id: str) -> list[LotAllocation]:
        """Fetch all allocations for a given lot."""
        sql = "SELECT * FROM settlement_allocation WHERE lot_id = ? ORDER BY allocated_at_utc ASC;"
        with self._connection() as conn:
            cur = conn.execute(sql, (lot_id,))
            out = []
            for row in cur.fetchall():
                out.append(
                    LotAllocation(
                        allocation_id=row["allocation_id"],
                        lot_id=row["lot_id"],
                        consumption_target_id=row["consumption_target_id"],
                        allocated_amount=Decimal(row["allocated_amount"]),
                        allocated_at_utc=datetime.fromisoformat(row["allocated_at_utc"]),
                        is_reversal=bool(row["is_reversal"]),
                        notes=row["notes"] or "",
                    )
                )
            return out

    # -------------------------------------------------------------------------
    # Invoice Reconciliation & Audit Line Variances
    # -------------------------------------------------------------------------

    def save_invoice_reconciliation(
        self,
        recon: InvoiceReconciliation,
        ppe_id: str = "",
    ) -> None:
        """Save invoice reconciliation report and line variances."""
        sql_head = """
        INSERT INTO invoice_reconciliation (
            invoice_number, ppe_id, period_start, period_end,
            computed_gross, invoiced_gross, variance_gross, variance_percent,
            status, notes, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(invoice_number) DO UPDATE SET
            ppe_id = excluded.ppe_id,
            period_start = excluded.period_start,
            period_end = excluded.period_end,
            computed_gross = excluded.computed_gross,
            invoiced_gross = excluded.invoiced_gross,
            variance_gross = excluded.variance_gross,
            variance_percent = excluded.variance_percent,
            status = excluded.status,
            notes = excluded.notes;
        """
        now_str = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            conn.execute(
                sql_head,
                (
                    recon.invoice_number,
                    ppe_id,
                    recon.period_start.isoformat(),
                    recon.period_end.isoformat(),
                    str(recon.computed_gross),
                    str(recon.invoiced_gross),
                    str(recon.variance_gross),
                    str(recon.variance_percent),
                    recon.status,
                    recon.notes,
                    now_str,
                ),
            )
            # Delete old line details and reinsert
            conn.execute(
                "DELETE FROM invoice_reconciliation_line WHERE invoice_number = ?;",
                (recon.invoice_number,),
            )
            sql_line = """
            INSERT INTO invoice_reconciliation_line (
                invoice_number, rate_id, name, computed_gross, invoiced_gross, diff_gross
            ) VALUES (?, ?, ?, ?, ?, ?);
            """
            line_params = [
                (
                    recon.invoice_number,
                    lv["rate_id"],
                    lv.get("name", lv["rate_id"]),
                    str(lv["computed_gross"]),
                    str(lv["invoiced_gross"]) if lv["invoiced_gross"] is not None else None,
                    str(lv["diff_gross"]),
                )
                for lv in recon.line_variances
            ]
            conn.executemany(sql_line, line_params)

    def get_invoice_reconciliation(
        self,
        invoice_number: str,
    ) -> InvoiceReconciliation | None:
        """Retrieve an invoice reconciliation and its per-line variances."""
        sql_head = "SELECT * FROM invoice_reconciliation WHERE invoice_number = ?;"
        sql_lines = "SELECT * FROM invoice_reconciliation_line WHERE invoice_number = ? ORDER BY id ASC;"
        with self._connection() as conn:
            cur = conn.execute(sql_head, (invoice_number,))
            row = cur.fetchone()
            if not row:
                return None

            cur_lines = conn.execute(sql_lines, (invoice_number,))
            line_variances = []
            for lr in cur_lines.fetchall():
                line_variances.append({
                    "rate_id": lr["rate_id"],
                    "name": lr["name"],
                    "computed_gross": Decimal(lr["computed_gross"]),
                    "invoiced_gross": Decimal(lr["invoiced_gross"]) if lr["invoiced_gross"] is not None else None,
                    "diff_gross": Decimal(lr["diff_gross"]),
                })

            return InvoiceReconciliation(
                invoice_number=row["invoice_number"],
                period_start=date.fromisoformat(row["period_start"]),
                period_end=date.fromisoformat(row["period_end"]),
                computed_gross=Decimal(row["computed_gross"]),
                invoiced_gross=Decimal(row["invoiced_gross"]),
                variance_gross=Decimal(row["variance_gross"]),
                variance_percent=Decimal(row["variance_percent"]),
                line_variances=line_variances,
                status=row["status"],
                notes=row["notes"] or "",
            )

    def set_reconciliation_approval(
        self,
        invoice_number: str,
        approved: bool,
        approved_by: str = "user",
    ) -> bool:
        """Set approval status on an invoice reconciliation."""
        now_str = datetime.now(timezone.utc).isoformat() if approved else None
        sql = """
        UPDATE invoice_reconciliation
        SET approved = ?, approved_by = ?, approved_at_utc = ?
        WHERE invoice_number = ?;
        """
        with self._connection() as conn:
            cur = conn.execute(
                sql,
                (1 if approved else 0, approved_by if approved else None, now_str, invoice_number),
            )
            return cur.rowcount > 0

