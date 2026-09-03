"""Tests for v0.2.12 native bank flows (Energy battery).

FlowAccumulator splits a moving base value into total_increasing
charge/discharge totals (replaces bank_energii.yaml templates):
- old net-metering: base = Bilans (net_exp*coeff - net_imp),
- new net-billing: charge follows net_export, discharge net_import.
"""

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "settlement_under_test",
    "custom_components/energa_mobile/settlement.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
FlowAccumulator = _mod.FlowAccumulator


class TestFlowAccumulatorBasics:
    def test_first_update_only_anchors(self):
        acc = FlowAccumulator()
        assert acc.update(100.0) == (0.0, 0.0)

    def test_growth_charges(self):
        acc = FlowAccumulator()
        acc.update(100.0)
        assert acc.update(150.0) == (50.0, 0.0)

    def test_shrinkage_discharges(self):
        acc = FlowAccumulator()
        acc.update(100.0)
        acc.update(150.0)
        assert acc.update(120.0) == (50.0, 30.0)

    def test_flat_no_movement(self):
        acc = FlowAccumulator()
        acc.update(100.0)
        assert acc.update(100.0) == (0.0, 0.0)

    def test_negative_base_allowed(self):
        # Bilans can go negative; only movement matters
        acc = FlowAccumulator()
        acc.update(-50.0)
        assert acc.update(-20.0) == (30.0, 0.0)
        assert acc.update(-70.0) == (30.0, 50.0)

    def test_none_base_ignored(self):
        acc = FlowAccumulator()
        acc.update(100.0)
        assert acc.update(None) == (0.0, 0.0)
        assert acc.update(130.0) == (30.0, 0.0)


class TestFlowAccumulatorRestore:
    def test_restore_reseeds_totals(self):
        acc = FlowAccumulator()
        acc.restore(1114.18, 503.86)
        acc.update(200.0)  # anchor only
        assert acc.update(250.0) == (1164.18, 503.86)

    def test_restore_partial(self):
        acc = FlowAccumulator()
        acc.restore(10.0, None)
        acc.update(0.0)
        assert acc.update(5.0) == (15.0, 0.0)


class TestOldSystemBilansFlows:
    def test_g12w_stare_scenario(self):
        # G12W stare zasady: bilans = net_exp*0.8 - net_imp
        acc = FlowAccumulator()
        nets = [(503.86, 2022.55), (510.0, 2100.0), (520.0, 2050.0)]
        for net_imp, net_exp in nets:
            charge, discharge = acc.update(net_exp * 0.8 - net_imp)
        # b: 1114.18 -> 1170.0 (+55.82 ch) -> 1120.0 (-50.0 dis)
        assert charge == 55.82
        assert discharge == 50.0


class TestNewSystemRawFlows:
    def test_g12w_nowe_scenario(self):
        # G12W nowe zasady: charge=export growth, discharge=import growth.
        # Meter totals only grow, so growth lands on side [0] of each
        # accumulator (same as EnergaBankFlowSensor reads it).
        ch, dis = FlowAccumulator(), FlowAccumulator()
        readings = [(786.13, 530.65), (790.0, 540.0), (800.0, 545.0)]
        for net_imp, net_exp in readings:
            ch.update(net_exp)
            dis.update(net_imp)
        assert (ch.charge, dis.charge) == (14.35, 13.87)
