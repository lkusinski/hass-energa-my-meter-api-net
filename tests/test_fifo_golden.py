"""F0 — characterization ("golden") tests for the production FIFO engine.

Oracle: `tests/data/fifo_golden.json`, generated from the production
`settlement.fifo_kwh_bank` / `fifo_dual_zone_kwh_bank` BEFORE the P2.1 refactor.
These tests pin the current, invoice-validated numeric behaviour: any change to
the extraction (F1) must keep every value byte-identical.

Do NOT regenerate the fixture to make a failing test pass — a diff here means a
behaviour change and must be treated as a regression.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from custom_components.energa_mobile.core.settlement.fifo_net_metering import (
    run_fifo_net_metering,
)
from custom_components.energa_mobile.settlement import (
    fifo_dual_zone_kwh_bank,
    fifo_kwh_bank,
)
from tests.fifo_cases import DUAL_CASES, SINGLE_CASES

_GOLDEN = json.loads(
    (Path(__file__).parent / "data" / "fifo_golden.json").read_text(encoding="utf-8")
)


def test_fixture_is_complete():
    assert {name for name, *_ in SINGLE_CASES} == set(_GOLDEN["single"])
    assert {name for name, *_ in DUAL_CASES} == set(_GOLDEN["dual"])


@pytest.mark.parametrize("name,flows,coeff,today", SINGLE_CASES, ids=[c[0] for c in SINGLE_CASES])
def test_single_zone_golden(name, flows, coeff, today):
    expected = _GOLDEN["single"][name]
    bank, detail = fifo_kwh_bank(flows, coeff, today=today)
    assert bank == expected["bank"], f"{name}: bank drifted"
    assert detail == expected["detail"], f"{name}: detail drifted"


@pytest.mark.parametrize("name,l1,l2,coeff,today", DUAL_CASES, ids=[c[0] for c in DUAL_CASES])
def test_dual_zone_golden(name, l1, l2, coeff, today):
    expected = _GOLDEN["dual"][name]
    bank, detail = fifo_dual_zone_kwh_bank(l1, l2, coeff, today=today)
    assert bank == expected["bank"], f"{name}: bank drifted"
    assert detail == expected["detail"], f"{name}: detail drifted"


@pytest.mark.parametrize("name,flows,coeff,today", SINGLE_CASES, ids=[c[0] for c in SINGLE_CASES])
def test_pure_layer_matches_production_engine(name, flows, coeff, today):
    """P2.1 invariant: the pure domain layer and production share one algorithm."""
    rows = [
        {"year": r[0], "month": r[1], "import_kwh": r[2], "export_kwh": r[3]}
        for r in flows
        if len(r) == 4
    ]
    summary = run_fifo_net_metering(
        ppe_id="PL_GOLDEN",
        monthly_flows=rows,
        coefficient=Decimal(str(coeff)),
        today=today,
    )
    bank, detail = fifo_kwh_bank(flows, coeff, today=today)
    assert summary.total_active_balance == Decimal(str(bank)), f"{name}: active balance"
    assert summary.total_expired == Decimal(str(detail["expired_kwh"])), f"{name}: expired"
    assert summary.total_deposited == Decimal(str(detail["deposits_kwh"])), f"{name}: deposited"
    assert summary.total_uncovered == Decimal(str(detail["uncovered_kwh"])), f"{name}: uncovered"
