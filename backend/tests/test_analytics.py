import random

import pytest

from app.analytics.health import BASELINE_WAFERS, HealthModel, HealthStatus, linear_fit
from app.analytics.spc import ControlLimits, check_rules, compute_limits
from app.config import HealthIndicator

IND = HealthIndicator("RF_REFL_POWER", nominal=12.0, spec_limit=40.0, units="W")
LIMITS = ControlLimits(mean=0.0, sigma=1.0)


def rules(values):
    return {v.rule for v in check_rules(values, LIMITS)}


# ---- SPC --------------------------------------------------------------------------------------
def test_in_control_data_triggers_nothing():
    assert rules([0.3, -0.5, 0.1, -0.2, 0.4, -0.1, 0.2, -0.3]) == set()


def test_rule1_beyond_3_sigma():
    assert 1 in rules([0.0, 3.5])
    assert 1 in rules([0.0, -3.1])


def test_rule2_two_of_three_beyond_2_sigma():
    assert 2 in rules([2.5, 0.0, 2.2])
    assert 2 not in rules([2.5, 0.0, -2.2])  # opposite sides don't count


def test_rule3_four_of_five_beyond_1_sigma():
    assert 3 in rules([1.5, 1.2, 0.0, 1.1, 1.3])


def test_rule4_eight_on_one_side():
    assert 4 in rules([0.1, 0.2, 0.3, 0.1, 0.5, 0.2, 0.4, 0.1])
    assert 4 not in rules([0.1, 0.2, 0.3, 0.1, -0.5, 0.2, 0.4, 0.1])


def test_rule5_trend():
    assert 5 in rules([0.0, 0.1, 0.2, 0.3, 0.4, 0.5])
    assert 5 not in rules([0.0, 0.1, 0.2, 0.15, 0.4, 0.5])


def test_compute_limits():
    lim = compute_limits([1.0, 2.0, 3.0])
    assert lim.mean == 2.0 and lim.sigma == pytest.approx(1.0)
    assert compute_limits([1.0]) is None
    assert compute_limits([5.0, 5.0], min_sigma=0.1).sigma == 0.1


# ---- health / RUL ---------------------------------------------------------------------------
def test_linear_fit_exact():
    slope, intercept = linear_fit([1.0, 3.0, 5.0, 7.0])
    assert slope == pytest.approx(2.0) and intercept == pytest.approx(1.0)


def test_new_tool_is_healthy():
    m = HealthModel(IND)
    for _ in range(10):
        m.add_wafer(12.0)
    snap = m.snapshot()
    assert snap.health == 100.0
    assert snap.status == HealthStatus.OK
    assert snap.rul_wafers is None  # flat -> not degrading


def test_rul_predicts_crossing_for_linear_degradation():
    m = HealthModel(IND)
    rng = random.Random(0)
    # 0.5 W per wafer from 20 W: crosses 40 W after 40 wafers in total.
    for i in range(20):
        m.add_wafer(20.0 + 0.5 * i + rng.gauss(0, 0.1))
    _, rul = m.trend()
    assert rul == pytest.approx(21, abs=2)  # 40 - 19 = 21 wafers left


def test_status_escalates_and_blocks_lots_that_would_cross_spec():
    m = HealthModel(IND, pm_margin_wafers=3)
    for i in range(20):
        m.add_wafer(30.0 + 0.5 * i)  # last = 39.5, slope 0.5 -> RUL ~1
    assert m.needs_pm_before(1)
    assert m.snapshot().status == HealthStatus.PM_REQUIRED


def test_watch_band():
    m = HealthModel(IND, pm_margin_wafers=3)
    for i in range(10):
        m.add_wafer(20.0 + 0.5 * i)  # RUL ~ 31 wafers
    snap = m.snapshot(next_lot_wafers=12)
    assert snap.status == HealthStatus.WATCH


def test_baseline_then_spc_then_reset():
    m = HealthModel(IND)
    for _ in range(BASELINE_WAFERS):
        assert m.add_wafer(12.0 + random.Random(1).gauss(0, 0.5)) == []
    assert m.limits is not None
    assert {v.rule for v in m.add_wafer(30.0)} >= {1}
    m.reset()
    assert m.limits is None and m.ewma is None and m.wafers_since_maint == 0
