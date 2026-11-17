import numpy as np
import pandas as pd
import pytest

from research.strategies import ORB
from research.tests.handout_helpers import day_from_bars

FLAT = (100.5, 101.0, 100.0, 100.5)           # the opening-range bars: range 100.0-101.0
orb = ORB()


def day(bars):
    return day_from_bars("2021-03-01", bars)


def sigs(bars, **kw):
    p = dict(orb.defaults)
    p.update(kw)
    return orb.signals(day(bars), **p)


def t(sig, i):
    return day([FLAT]).index[i]


def test_breakout_long_signal_on_first_close_beyond_range_and_stops():
    bars = [FLAT] * 3 + [(100.6, 101.8, 100.5, 101.5)]          # 4th bar (09:45) closes above 101.0
    s = sigs(bars, range_minutes=15, stop_mode="range", target_r=1.0)
    assert len(s) == 1
    r = s.iloc[0]
    assert s.index[0] == t(s, 3) and r["side"] == 1 and r["stop"] == 100.0 and r["target_r"] == 1.0
    c = sigs(bars, range_minutes=15, stop_mode="confirm")        # stop beyond the confirmation candle's low
    assert c.iloc[0]["stop"] == 100.5


def test_breakout_short_and_hold_to_close_target_is_nan():
    bars = [FLAT] * 3 + [(100.4, 100.5, 99.2, 99.5)]
    s = sigs(bars, range_minutes=15, target_r=float("nan"))
    assert len(s) == 1 and s.iloc[0]["side"] == -1 and s.iloc[0]["stop"] == 101.0
    assert np.isnan(s.iloc[0]["target_r"])


def test_5_minute_range_uses_only_first_bar():
    bars = [(100.5, 101.0, 100.0, 100.5), (100.5, 101.6, 100.4, 101.4)]      # 2nd bar closes above the 1st bar's high
    assert len(sigs(bars, range_minutes=5)) == 1
    assert len(sigs(bars, range_minutes=15)) == 0                               # inside the 15-minute range


def test_one_trade_per_day_only_first_break():
    bars = [FLAT] * 3 + [(100.6, 101.8, 100.5, 101.5), (101.5, 102.5, 101.4, 102.3), (102.3, 100.0, 99.0, 99.2)]
    s = sigs(bars, range_minutes=15)
    assert len(s) == 1 and s.iloc[0]["side"] == 1


def test_retest_needs_a_break_then_a_confirmed_retest():
    break_bar = (100.6, 101.8, 100.5, 101.5)
    drift = (101.5, 101.9, 101.3, 101.7)                         # no retest yet
    retest = (101.6, 101.7, 100.9, 101.3)                        # dips to 100.9 (< range high) and closes back above
    bars = [FLAT] * 3 + [break_bar, drift, retest]
    s = sigs(bars, range_minutes=15, entry_mode="retest", stop_mode="confirm")
    assert len(s) == 1
    assert s.index[0] == t(s, 5) and s.iloc[0]["side"] == 1 and s.iloc[0]["stop"] == 100.9
    # the breakout bar alone is not a retest entry
    assert len(sigs([*bars[:4]], range_minutes=15, entry_mode="retest")) == 0
    # a retest that closes back INSIDE the range is not a confirmation
    failed = (101.6, 101.7, 100.4, 100.8)
    assert len(sigs([*bars[:5], failed], range_minutes=15, entry_mode="retest")) == 0


def test_only_first_two_hours():
    late = lambda k: [FLAT] * k + [(100.6, 101.8, 100.5, 101.5)]
    # signal bar 22 (11:20) -> entry on the 11:25 bar: allowed. Signal bar 23 (11:25) -> entry 11:30: not allowed
    assert len(sigs(late(22), range_minutes=5)) == 1
    assert len(sigs(late(23), range_minutes=5)) == 0
    assert len(sigs(late(26), range_minutes=5)) == 0


def test_tiny_stops_are_skipped():
    narrow = (100.50, 100.51, 100.50, 100.505)
    bars = [narrow] * 3 + [(100.5, 100.53, 100.5, 100.52)]
    assert len(sigs(bars, range_minutes=15)) == 0


def test_direction_switch_and_grid_size():
    bars = [FLAT] * 3 + [(100.6, 101.8, 100.5, 101.5)]
    assert len(sigs(bars, range_minutes=15, direction="short")) == 0
    ps = orb.param_sets()
    assert len(ps) == 36 and {p["range_minutes"] for p in ps} == {5, 15, 30}
    assert {p["entry_mode"] for p in ps} == {"breakout", "retest"}
    assert {p["stop_mode"] for p in ps} == {"range", "confirm"}
    targets = [p["target_r"] for p in ps]
    assert sum(x == 1.0 for x in targets) == 12 and sum(x == 2.0 for x in targets) == 12
    assert sum(np.isnan(x) for x in targets) == 12
