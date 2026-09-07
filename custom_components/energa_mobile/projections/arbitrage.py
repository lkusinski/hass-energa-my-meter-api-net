"""BESS and Self-Consumption Arbitrage Engine for Dynamic PSE RCE Prices.

Reference: Energa HA Skorygowana Architektura Docelowa (04.09.2026), Rozdzial 10 & 11 (Etap 5).
Pure domain logic:
- Detects optimal battery energy storage system (BESS) charge and discharge windows.
- Calculates net arbitrage spread taking Round-Trip Efficiency (e.g. 88%) and battery cycle wear into account.
- Identifies negative market prices (RCE < 0) for proactive prosumer export curtailment / load boosting.
- Evaluates actions: CHARGE, DISCHARGE, NEGATIVE_ALERT, IDLE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import logging
from typing import Sequence

from ..adapters.pse.models import MarketPriceRecord

_LOGGER = logging.getLogger(__name__)


class ArbitrageAction(str, Enum):
    """Recommended action for energy storage / load management."""

    CHARGE = "charge"
    DISCHARGE = "discharge"
    NEGATIVE_ALERT = "negative_alert"
    IDLE = "idle"


@dataclass(frozen=True)
class TimeWindow:
    """A contiguous period of time identified for an arbitrage phase."""

    start_utc: datetime
    end_utc: datetime
    avg_price_kwh: Decimal
    min_price_kwh: Decimal
    max_price_kwh: Decimal
    intervals_count: int

    @property
    def duration_hours(self) -> float:
        """Duration of window in hours."""
        delta = self.end_utc - self.start_utc
        return round(delta.total_seconds() / 3600.0, 2)

    def contains(self, dt: datetime) -> bool:
        """Check if a datetime falls within this window [start, end)."""
        return self.start_utc <= dt < self.end_utc


@dataclass(frozen=True)
class ArbitragePlan:
    """Complete day-ahead / intra-day arbitrage plan."""

    target_date: date
    charge_windows: list[TimeWindow] = field(default_factory=list)
    discharge_windows: list[TimeWindow] = field(default_factory=list)
    negative_intervals: list[MarketPriceRecord] = field(default_factory=list)
    avg_charge_price_kwh: Decimal = Decimal("0.0")
    avg_discharge_price_kwh: Decimal = Decimal("0.0")
    effective_spread_kwh: Decimal = Decimal("0.0")
    is_spread_profitable: bool = False
    battery_efficiency: Decimal = Decimal("0.88")
    min_spread_pln: Decimal = Decimal("0.05")

    def action_at(self, dt_utc: datetime) -> ArbitrageAction:
        """Determine recommended action for a given timestamp."""
        # 1. Negative price alert has highest safety priority
        for neg in self.negative_intervals:
            if neg.interval_start_utc and neg.interval_end_utc:
                if neg.interval_start_utc <= dt_utc < neg.interval_end_utc:
                    return ArbitrageAction.NEGATIVE_ALERT

        if not self.is_spread_profitable:
            return ArbitrageAction.IDLE

        # 2. Optimal charging window
        for cw in self.charge_windows:
            if cw.contains(dt_utc):
                return ArbitrageAction.CHARGE

        # 3. Peak discharge window
        for dw in self.discharge_windows:
            if dw.contains(dt_utc):
                return ArbitrageAction.DISCHARGE

        return ArbitrageAction.IDLE


class ArbitrageEngine:
    """Engine computing optimal BESS and load dispatch schedules based on PSE RCE prices."""

    def __init__(
        self,
        battery_efficiency: Decimal = Decimal("0.88"),
        charge_hours: int = 3,
        discharge_hours: int = 3,
        min_spread_pln: Decimal = Decimal("0.05"),
    ) -> None:
        """Initialize arbitrage parameters.

        Args:
            battery_efficiency: Round-trip efficiency factor (default 0.88 = 88%).
            charge_hours: Target duration for lowest-price charging (default 3 hours).
            discharge_hours: Target duration for peak-price discharging (default 3 hours).
            min_spread_pln: Minimum net spread required after efficiency losses (PLN/kWh).
        """
        self.battery_efficiency = battery_efficiency
        self.charge_hours = max(1, charge_hours)
        self.discharge_hours = max(1, discharge_hours)
        self.min_spread_pln = min_spread_pln

    def plan_day(
        self,
        records: Sequence[MarketPriceRecord],
        target_date: date | None = None,
    ) -> ArbitragePlan:
        """Generate arbitrage plan from a day's interval records (15-min or 1-hour)."""
        if not records:
            d = target_date or date.today()
            return ArbitragePlan(target_date=d)

        valid_records = [
            r for r in records
            if r.interval_start_utc and r.interval_end_utc
        ]
        if not valid_records:
            d = target_date or date.today()
            return ArbitragePlan(target_date=d)

        # Infer target date from records if not provided
        t_date = target_date or valid_records[0].publication_date
        # Filter for target date if business_date is present
        day_records = [
            r for r in valid_records
            if (r.business_date == t_date if r.business_date else True)
        ]
        if not day_records:
            day_records = valid_records

        # 1. Detect negative price intervals (RCE < 0)
        neg_records = [r for r in day_records if r.price_kwh < Decimal("0.0")]

        # Determine number of slots needed for target hours
        # Check median slot duration (typically 15 min or 60 min)
        durations = [
            (r.interval_end_utc - r.interval_start_utc).total_seconds()
            for r in day_records[:4]
            if r.interval_start_utc and r.interval_end_utc
        ]
        slot_mins = 15
        if durations:
            avg_dur = sum(durations) / len(durations)
            if avg_dur >= 3000:  # ~1 hour
                slot_mins = 60

        slots_per_hour = 60 // slot_mins
        req_charge_slots = min(len(day_records), self.charge_hours * slots_per_hour)
        req_discharge_slots = min(len(day_records), self.discharge_hours * slots_per_hour)

        # 2. Select cheapest slots for charging
        sorted_cheapest = sorted(day_records, key=lambda r: (r.price_kwh, r.interval_start_utc))
        charge_slots = sorted(
            sorted_cheapest[:req_charge_slots],
            key=lambda r: r.interval_start_utc or datetime.min,
        )

        # 3. Select most expensive slots for discharging
        sorted_expensive = sorted(day_records, key=lambda r: (-r.price_kwh, r.interval_start_utc))
        discharge_slots = sorted(
            sorted_expensive[:req_discharge_slots],
            key=lambda r: r.interval_start_utc or datetime.min,
        )

        # Build contiguous TimeWindows
        charge_windows = self._group_into_windows(charge_slots)
        discharge_windows = self._group_into_windows(discharge_slots)

        avg_charge = (
            sum(r.price_kwh for r in charge_slots) / Decimal(len(charge_slots))
            if charge_slots
            else Decimal("0.0")
        )
        avg_discharge = (
            sum(r.price_kwh for r in discharge_slots) / Decimal(len(discharge_slots))
            if discharge_slots
            else Decimal("0.0")
        )

        # Spread: discharge price recovered through battery round-trip efficiency minus charging cost
        effective_spread = (avg_discharge * self.battery_efficiency) - avg_charge
        is_profitable = effective_spread >= self.min_spread_pln

        return ArbitragePlan(
            target_date=t_date,
            charge_windows=charge_windows,
            discharge_windows=discharge_windows,
            negative_intervals=neg_records,
            avg_charge_price_kwh=round(avg_charge, 5),
            avg_discharge_price_kwh=round(avg_discharge, 5),
            effective_spread_kwh=round(effective_spread, 5),
            is_spread_profitable=is_profitable,
            battery_efficiency=self.battery_efficiency,
            min_spread_pln=self.min_spread_pln,
        )

    def _group_into_windows(self, slots: Sequence[MarketPriceRecord]) -> list[TimeWindow]:
        """Group consecutive interval slots into continuous TimeWindow instances."""
        if not slots:
            return []

        sorted_slots = sorted(slots, key=lambda r: r.interval_start_utc or datetime.min)
        windows: list[TimeWindow] = []

        current_batch: list[MarketPriceRecord] = [sorted_slots[0]]

        for r in sorted_slots[1:]:
            prev = current_batch[-1]
            if prev.interval_end_utc and r.interval_start_utc and prev.interval_end_utc == r.interval_start_utc:
                current_batch.append(r)
            else:
                # Close current window and start a new one
                windows.append(self._create_window(current_batch))
                current_batch = [r]

        if current_batch:
            windows.append(self._create_window(current_batch))

        return windows

    def _create_window(self, batch: list[MarketPriceRecord]) -> TimeWindow:
        """Helper to create a TimeWindow from a batch of consecutive records."""
        start_utc = batch[0].interval_start_utc or datetime.min.replace(tzinfo=timezone.utc)
        end_utc = batch[-1].interval_end_utc or datetime.min.replace(tzinfo=timezone.utc)
        prices = [r.price_kwh for r in batch]
        avg_p = sum(prices) / Decimal(len(prices))
        return TimeWindow(
            start_utc=start_utc,
            end_utc=end_utc,
            avg_price_kwh=round(avg_p, 5),
            min_price_kwh=min(prices),
            max_price_kwh=max(prices),
            intervals_count=len(batch),
        )
