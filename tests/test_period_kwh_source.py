"""Bug A tests (v1.9.3-beta.2): canonical hourly source for ``verify_period``.

``verify_period`` used to read the hourly recorder/API series directly, even
though the canonical SQLite ``interval_reading`` table (same identity since
v1.9.3-beta.1) is the single source of truth. Bug A makes the source precedence

    override -> canonical -> recorder -> api

and surfaces ``kwh_source`` in ``{canonical, recorder, api}`` plus a warning
when the canonical series does not cover the period (per zone, hourly) and the
caller falls back.

The invoice results must stay identical regardless of the source used, and
Agrestowa/Bursztynowa/Wiśniowa invoice parity must be preserved.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energa_mobile.const import (
    CONF_PROSUMER_COEFFICIENT,
    CONF_TARIFF_ABONAMENT,
    CONF_TARIFF_CAPACITY,
    CONF_TARIFF_COGEN,
    CONF_TARIFF_ENERGY_DAY,
    CONF_TARIFF_ENERGY_NIGHT,
    CONF_TARIFF_EXCISE_MWH,
    CONF_TARIFF_GRID_FIXED,
    CONF_TARIFF_GRID_VAR_DAY,
    CONF_TARIFF_GRID_VAR_NIGHT,
    CONF_TARIFF_OZE,
    CONF_TARIFF_QUALITY,
    CONF_TARIFF_TRADE_FEE,
    DOMAIN,
)
from custom_components.energa_mobile.core.readings.models import IntervalReading
from custom_components.energa_mobile.core.verification import (
    KWH_SOURCE_API,
    KWH_SOURCE_CANONICAL,
    KWH_SOURCE_RECORDER,
    canonical_series_covers_period,
    canonical_series_has_zones,
    deposit_rcem_from_table,
    deposit_rcem_month,
    kwh_source_for_period_source,
)
from custom_components.energa_mobile.services import async_verify_period_data
from custom_components.energa_mobile.storage.sqlite.database import CanonicalStorage

TZ = ZoneInfo("Europe/Warsaw")

_PATCH_HOURLY = "custom_components.energa_mobile.services._collect_meter_hourly"
_PATCH_API = "custom_components.energa_mobile.services._collect_meter_hourly_api"
_PATCH_MONTHLY = "custom_components.energa_mobile.services._collect_monthly_flows"


def _agrestowa_meter() -> dict:
    return {
        "meter_point_id": "10000001",
        "meter_serial": "10000001",
        "zone_count": 2,
        "total_plus": 707.0,
        "total_minus": 435.0,
        "is_prosumer": True,
        "tariff": "G12W",
    }


def _agrestowa_options() -> dict:
    return {
        CONF_PROSUMER_COEFFICIENT: 0.0,
        CONF_TARIFF_ENERGY_DAY: 0.6107,
        CONF_TARIFF_ENERGY_NIGHT: 0.3990,
        CONF_TARIFF_EXCISE_MWH: 5.0,
        CONF_TARIFF_TRADE_FEE: 0.0,
        CONF_TARIFF_ABONAMENT: 0.74,
        CONF_TARIFF_GRID_FIXED: 20.17,
        CONF_TARIFF_GRID_VAR_DAY: 0.4017,
        CONF_TARIFF_GRID_VAR_NIGHT: 0.0851,
        CONF_TARIFF_QUALITY: 0.0332,
        CONF_TARIFF_OZE: 0.0073,
        CONF_TARIFF_COGEN: 0.0030,
        CONF_TARIFF_CAPACITY: 24.05,
    }


def _agrestowa_hourly() -> dict:
    """Hourly series giving saldo_plus 398/309, overlap 38/23, export 238/197."""

    def _zone(plus: float, overlap: float, minus: float):
        imp = {0: float(plus)}
        exp: dict = {}
        if overlap or minus:
            imp[3600] = float(overlap)
            exp[3600] = float(overlap + minus)
        return imp, exp

    imp1, exp1 = _zone(398, 38, 238)
    imp2, exp2 = _zone(309, 23, 197)
    return {
        "import_1": imp1,
        "export_1": exp1,
        "import_2": imp2,
        "export_2": exp2,
    }


def _wisniowa_meter() -> dict:
    return {
        "meter_point_id": "10000002",
        "meter_serial": "10000002",
        "zone_count": 2,
        "total_plus": 425.0,
        "total_minus": 1904.0,
        "is_prosumer": True,
        "tariff": "G12W",
    }


def _hass_with_meter(meter: dict, options: dict, *, ppe_id: str = "PPE_TEST"):
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = options
    entry.data = {"ppe_id": ppe_id}
    api = MagicMock()
    api.async_fetch_official_rcem_map = AsyncMock(
        return_value={(2026, 7): 0.26288, (2026, 8): 0.29453}
    )
    coordinator = SimpleNamespace(
        data=[meter],
        _verify_cache={},
        _rce_cache=None,
        _rcem_monthly={},
        _monthly={},
        async_update_listeners=MagicMock(),
    )
    storage = CanonicalStorage(":memory:")
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    hass.data = {
        DOMAIN: {
            "entry_1": {
                "coordinator": coordinator,
                "api": api,
                "storage": storage,
            }
        }
    }
    return hass, entry, coordinator, storage


def _hour_epoch(day: date, hour: int, tz=TZ) -> int:
    moment = datetime.combine(day, time(hour=hour), tzinfo=tz)
    return int(moment.timestamp())


def _canonical_from_abstract(hourly: dict, start: date, end: date, meter_id: str) -> list:
    """Real-epoch IntervalReadings covering the period, values from ``hourly``.

    ``hourly`` is the abstract recorder shape (epoch-hour buckets 0/3600). The
    canonical store is filtered by real timestamps, so the values are placed on
    the first hours of the first day, and a zero reading is written for every
    (zone, day) so the series *covers* the period — exactly the production
    shape once the backfill has run.
    """
    zones = [z for z in ("import_1", "import_2", "export_1", "export_2") if z in hourly]
    readings: list = []
    # Preserve the hourly alignment: every abstract timestamp keeps its zones
    # on the *same* real hour so ``bill_saldos`` nets them identically to the
    # recorder path. Abstract bucket n is mapped to hour 1+n of day 1.
    abstract_ts = sorted({int(t) for zone in zones for t in (hourly.get(zone) or {})})
    ts_to_hour = {ts: 1 + idx for idx, ts in enumerate(abstract_ts)}
    for zone in zones:
        for ts, kwh in (hourly.get(zone) or {}).items():
            hour = ts_to_hour[int(ts)]
            moment = datetime.combine(start, time(hour=hour), tzinfo=TZ)
            readings.append(
                IntervalReading(
                    ppe_id="PPE_TEST",
                    meter_id=meter_id,
                    register=zone,
                    interval_start_utc=moment.astimezone(timezone.utc),
                    resolution="1h",
                    import_kwh=Decimal(str(kwh)) if zone.startswith("import") else Decimal("0.0"),
                    export_kwh=Decimal(str(kwh)) if zone.startswith("export") else Decimal("0.0"),
                    quality="ok",
                    source="energa",
                )
            )
    # Zero filler for every (zone, day) so coverage holds.
    filler = 12
    day = start
    while day < end:
        moment = datetime.combine(day, time(hour=filler), tzinfo=TZ)
        for zone in zones:
            readings.append(
                IntervalReading(
                    ppe_id="PPE_TEST",
                    meter_id=meter_id,
                    register=zone,
                    interval_start_utc=moment.astimezone(timezone.utc),
                    resolution="1h",
                    import_kwh=Decimal("0.0"),
                    export_kwh=Decimal("0.0"),
                    quality="ok",
                    source="energa",
                )
            )
        day += timedelta(days=1)
    return readings


def _seed_canonical(storage, hourly: dict, start: date, end: date, *, ppe_id="PPE_TEST", meter_id="10000001"):
    """Store a period-covering canonical series built from ``hourly``."""
    storage.insert_readings_idempotent(
        _canonical_from_abstract(hourly, start, end, meter_id)
    )


class TestCanonicalSourceHelpers:
    def test_kwh_source_mapping(self):
        assert kwh_source_for_period_source("energa_api") == KWH_SOURCE_API
        assert kwh_source_for_period_source("recorder_hourly") == KWH_SOURCE_RECORDER
        # Unknown/mixed labels degrade to the recorder vocabulary.
        assert kwh_source_for_period_source("mixed") == KWH_SOURCE_RECORDER
        assert kwh_source_for_period_source(None) == KWH_SOURCE_RECORDER

    def test_canonical_series_has_zones(self):
        assert canonical_series_has_zones({"import_1": {}, "import_2": {1: 1.0}}) is True
        assert canonical_series_has_zones({"import_1": {1: 1.0}}) is False

    def test_coverage_requires_every_zone_and_day(self):
        start, end = date(2026, 8, 1), date(2026, 8, 4)  # 3 days
        # 3 days covered for both zones.
        full = {
            "import_1": {_hour_epoch(start + timedelta(days=i), 1): 1.0 for i in range(3)},
            "export_1": {_hour_epoch(start + timedelta(days=i), 1): 1.0 for i in range(3)},
        }
        assert canonical_series_covers_period(full, ["import_1", "export_1"], start=start, end=end)
        # Missing a day -> not covered.
        partial = {"import_1": full["import_1"], "export_1": {next(iter(full["export_1"])): 1.0}}
        assert not canonical_series_covers_period(partial, ["import_1", "export_1"], start=start, end=end)
        # Missing a whole zone -> not covered.
        assert not canonical_series_covers_period(full, ["import_1", "import_2"], start=start, end=end)

    def test_coverage_garbage_is_false(self):
        assert not canonical_series_covers_period({}, ["import_1"], start="x", end="y")
        assert not canonical_series_covers_period({"import_1": {}}, [], start=date(2026, 8, 1), end=date(2026, 8, 2))

    def test_deposit_rcem_month_and_m_minus_one(self):
        assert deposit_rcem_month(2026, 8) == (2026, 7)
        assert deposit_rcem_month(2026, 1) == (2025, 12)
        table = {(2026, 5): 0.19137, (2026, 6): 0.27320}
        # M-1 (May) is preferred for June delivery.
        value, fell_back = deposit_rcem_from_table(table, 2026, 6)
        assert value == 0.19137 and fell_back is False
        # M-1 missing -> delivery month (M) with fell_back flag.
        value, fell_back = deposit_rcem_from_table({(2026, 6): 0.27320}, 2026, 6)
        assert value == 0.27320 and fell_back is True
        # Neither -> fallback scalar.
        value, fell_back = deposit_rcem_from_table({}, 2026, 6, fallback=0.25)
        assert value == 0.25 and fell_back is True
        assert deposit_rcem_from_table({}, 2026, 6)[0] is None


class TestCanonicalSourcePrecedence:
    @pytest.mark.asyncio
    async def test_canonical_preferred_and_recorder_not_consulted(self):
        hass, _, coordinator, storage = _hass_with_meter(
            _agrestowa_meter(), _agrestowa_options()
        )
        start, end = date(2026, 8, 1), date(2026, 9, 1)
        _seed_canonical(storage, _agrestowa_hourly(), start, end)
        recorder_mock = AsyncMock(return_value=_agrestowa_hourly())
        with patch(_PATCH_HOURLY, new=recorder_mock), patch(
            _PATCH_MONTHLY, new=AsyncMock(return_value={})
        ):
            res = await async_verify_period_data(
                hass,
                {"start": "2026-08-01", "end": "2026-08-31", "meter_id": "10000001",
                 "rcem_pln": 0.29453},
            )
        recorder_mock.assert_not_awaited()
        assert res["kwh_source"] == KWH_SOURCE_CANONICAL
        assert res["kwh"]["saldo_plus_1"] == 398.0
        assert res["kwh"]["saldo_plus_2"] == 309.0

    @pytest.mark.asyncio
    async def test_canonical_matches_recorder_invoice_to_the_grosz(self):
        """Same invoice regardless of source (contract requirement)."""
        payload = {
            "start": "2026-08-01",
            "end": "2026-08-31",
            "meter_id": "10000001",
            "rcem_pln": 0.29453,
        }
        # Canonical run.
        hass_c, _, _, storage = _hass_with_meter(_agrestowa_meter(), _agrestowa_options())
        _seed_canonical(storage, _agrestowa_hourly(), date(2026, 8, 1), date(2026, 9, 1))
        with patch(_PATCH_HOURLY, new=AsyncMock(return_value=_agrestowa_hourly())), patch(
            _PATCH_MONTHLY, new=AsyncMock(return_value={})
        ):
            canonical = await async_verify_period_data(hass_c, payload)
        # Recorder run (no canonical history).
        hass_r, _, _, _ = _hass_with_meter(_agrestowa_meter(), _agrestowa_options())
        with patch(_PATCH_HOURLY, new=AsyncMock(return_value=_agrestowa_hourly())), patch(
            _PATCH_MONTHLY, new=AsyncMock(return_value={})
        ):
            recorder = await async_verify_period_data(hass_r, payload)
        assert canonical["kwh_source"] == KWH_SOURCE_CANONICAL
        assert recorder["kwh_source"] == KWH_SOURCE_RECORDER
        for key in ("netto", "vat", "brutto", "deposit", "do_zaplaty"):
            assert canonical[key] == recorder[key], key
        # Invoice parity (Agrestowa 08.2026).
        assert canonical["netto"] == 628.55
        assert canonical["vat"] == 144.57
        assert canonical["brutto"] == 773.12
        assert canonical["do_zaplaty"] == 615.53

    @pytest.mark.asyncio
    async def test_fallback_when_canonical_incomplete_with_warning(self):
        hass, _, _, storage = _hass_with_meter(
            _agrestowa_meter(), _agrestowa_options()
        )
        # Only one day present -> canonical does not cover the period.
        start, end = date(2026, 8, 1), date(2026, 9, 1)
        _seed_canonical(storage, {"import_1": {_hour_epoch(start, 1): 5.0}}, start, end)
        # Remove the zero fillers to force a partial series.
        # (Seed again with a fresh storage that only has the one day.)
        hass2, _, _, storage2 = _hass_with_meter(
            _agrestowa_meter(), _agrestowa_options()
        )
        storage2.insert_readings_idempotent(
            [
                IntervalReading(
                    ppe_id="PPE_TEST",
                    meter_id="10000001",
                    register="import_1",
                    interval_start_utc=datetime(2026, 7, 31, 23, tzinfo=timezone.utc),
                    resolution="1h",
                    import_kwh=Decimal("5.0"),
                    export_kwh=Decimal("0.0"),
                    quality="ok",
                    source="energa",
                )
            ]
        )
        with patch(_PATCH_HOURLY, new=AsyncMock(return_value=_agrestowa_hourly())), patch(
            _PATCH_MONTHLY, new=AsyncMock(return_value={})
        ):
            res = await async_verify_period_data(
                hass2,
                {"start": "2026-08-01", "end": "2026-08-31", "meter_id": "10000001",
                 "rcem_pln": 0.29453},
            )
        assert res["kwh_source"] == KWH_SOURCE_RECORDER
        assert any("nie pokrywa całego okresu" in w for w in res["warnings"])

    @pytest.mark.asyncio
    async def test_canonical_missing_zone_falls_back(self):
        """Legacy period stored without per-zone rows -> fallback + warning."""
        hass, _, _, storage = _hass_with_meter(_agrestowa_meter(), _agrestowa_options())
        start, end = date(2026, 8, 1), date(2026, 9, 1)
        # Only import_1 is stored; import_2/export zones absent -> no coverage.
        _seed_canonical(storage, {"import_1": {_hour_epoch(start, 1): 5.0}}, start, end)
        with patch(_PATCH_HOURLY, new=AsyncMock(return_value=_agrestowa_hourly())), patch(
            _PATCH_MONTHLY, new=AsyncMock(return_value={})
        ):
            res = await async_verify_period_data(
                hass,
                {"start": "2026-08-01", "end": "2026-08-31", "meter_id": "10000001",
                 "rcem_pln": 0.29453},
            )
        assert res["kwh_source"] == KWH_SOURCE_RECORDER
        assert any("nie pokrywa" in w for w in res["warnings"])

    @pytest.mark.asyncio
    async def test_api_fallback_kwh_source_is_api(self):
        """No canonical, no recorder -> the Energa API series is reported."""
        hass, _, _, _ = _hass_with_meter(
            _agrestowa_meter(), _agrestowa_options()
        )
        api_hourly = _agrestowa_hourly()
        with patch(_PATCH_HOURLY, new=AsyncMock(return_value={})), patch(
            _PATCH_API, new=AsyncMock(return_value=api_hourly)
        ), patch(_PATCH_MONTHLY, new=AsyncMock(return_value={})):
            res = await async_verify_period_data(
                hass,
                {"start": "2026-08-01", "end": "2026-08-31", "meter_id": "10000001",
                 "rcem_pln": 0.29453},
            )
        assert res["kwh_source"] == KWH_SOURCE_API
        assert res["source"] == "energa_api"
        assert res["kwh"]["saldo_plus_1"] == 398.0


class TestCanonicalPressureWisniowaAndBursztynowa:
    @pytest.mark.asyncio
    async def test_wisniowa_net_metering_same_result_via_canonical(self):
        """Wiśniowa 07-08.2026 = 129,04 / 29,68 / 158,72 independent of source."""
        hourly = {
            "import_1": {0: 83.0, 3600: 37.0},
            "export_1": {3600: 100.0},
            "import_2": {0: 342.0, 3600: 30.0},
            "export_2": {3600: 50.0},
        }
        payload = {
            "start": "2026-07-01",
            "end": "2026-08-31",
            "meter_id": "10000002",
            "bank_open_1": 500.0,
            "bank_open_2": 500.0,
        }
        # Canonical run.
        hass_c, _, _, storage = _hass_with_meter(_wisniowa_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8})
        _seed_canonical(storage, hourly, date(2026, 7, 1), date(2026, 9, 1), ppe_id="PPE_TEST")
        with patch(_PATCH_HOURLY, new=AsyncMock(return_value=hourly)), patch(
            _PATCH_MONTHLY, new=AsyncMock(return_value={})
        ):
            canonical = await async_verify_period_data(hass_c, payload)
        # Recorder run.
        hass_r, _, _, _ = _hass_with_meter(_wisniowa_meter(), {CONF_PROSUMER_COEFFICIENT: 0.8})
        with patch(_PATCH_HOURLY, new=AsyncMock(return_value=hourly)), patch(
            _PATCH_MONTHLY, new=AsyncMock(return_value={})
        ):
            recorder = await async_verify_period_data(hass_r, payload)
        assert canonical["kwh_source"] == KWH_SOURCE_CANONICAL
        assert recorder["kwh_source"] == KWH_SOURCE_RECORDER
        for key in ("netto", "vat", "brutto", "do_zaplaty"):
            assert canonical[key] == recorder[key], key
        assert canonical["netto"] == 129.04
        assert canonical["vat"] == 29.68
        assert canonical["brutto"] == 158.72
