import numpy as np
import pandas as pd
import pytest

from research import backtest as bt, costs
from research.strategies import HarrySharpWick
from research.strategies.harry_sharp import EXIT_MODES, bias_by_day, htf_candles
from research.tests.handout_helpers import day_from_bars, day_from_path, mirror, mirror_frame

hs = HarrySharpWick()

RANGE = (100.5, 101.0, 100.0, 100.5)                       # range 100-101
WICK = (100.5, 100.7, 99.5, 100.6)                         # sweeps 100, closes back above, lower wick 0.83 of range
BREAK = (100.6, 101.6, 100.5, 101.5)                       # closes above the 101 range high
FILL = (101.5, 101.8, 101.4, 101.7)                        # trades through the break bar's high (101.6)
SETUP = [RANGE] * 3 + [WICK, BREAK, FILL]

UP_UP = [(0, 100), (77, 102)]
UP_THEN_AFTERNOON_DOWN = [(0, 100), (47, 103), (77, 102)]  # daily up, 13:30-16:00 block down
DOWN_DOWN = [(0, 102), (77, 100)]


def frame(prev_knots, setup, tf=5):
    prior = day_from_path("2021-03-01", prev_knots)
    return pd.concat([prior, day_from_bars("2021-03-02", setup)])


def sig(prev_knots, setup, **kw):
    p = dict(hs.defaults)
    p.update(kw)
    return hs.signals(frame(prev_knots, setup), **p)


# ---------------------------------------------------------------- bias
def test_candles_build_two_four_hour_blocks():
    c = htf_candles(frame(UP_THEN_AFTERNOON_DOWN, SETUP))
    d1 = c.iloc[0]
    assert d1["d_close"] > d1["d_open"]
    assert d1["b_close"] < d1["b_open"]            # the 13:30-16:00 block fell
    assert d1["a_close"] > 102.5                   # block one closed near the 103 peak at bar 47


def test_strict_needs_daily_and_4h_to_agree_daily_does_not():
    f = frame(UP_THEN_AFTERNOON_DOWN, SETUP)
    c = htf_candles(f)
    assert bias_by_day(c, "strict", 20, 0).iloc[1] == 0
    assert bias_by_day(c, "daily", 20, 0).iloc[1] == 1
    c2 = htf_candles(frame(UP_UP, SETUP))
    assert bias_by_day(c2, "strict", 20, 0).iloc[1] == 1
    c3 = htf_candles(frame(DOWN_DOWN, SETUP))
    assert bias_by_day(c3, "strict", 20, 0).iloc[1] == -1
    assert bias_by_day(c3, "strict", 20, 0).iloc[0] == 0          # nothing before the first session


def test_ema_bias_compares_closes_to_their_emas():
    days = [day_from_path(d, [(0, 100 + i), (77, 101 + i)]) for i, d in enumerate(pd.bdate_range("2021-01-04", periods=30))]
    c = htf_candles(pd.concat(days))
    assert (bias_by_day(c, "ema", 10, 0).iloc[-5:] == 1).all()          # steady rise: above both EMAs
    days = [day_from_path(d, [(0, 140 - i), (77, 139 - i)]) for i, d in enumerate(pd.bdate_range("2021-01-04", periods=30))]
    c = htf_candles(pd.concat(days))
    assert (bias_by_day(c, "ema", 10, 0).iloc[-5:] == -1).all()


def test_structure_filter_needs_higher_highs_and_lows():
    rising = [day_from_path(d, [(0, 100 + i), (77, 101 + i)]) for i, d in enumerate(pd.bdate_range("2021-03-01", periods=3))]
    f = pd.concat(rising + [day_from_bars("2021-03-04", SETUP)])
    kw = dict(hs.defaults, bias_mode="daily")
    assert len(hs.signals(f, **{**kw, "struct_days": 3})) == 1
    broken = [rising[0], day_from_path("2021-03-02", [(0, 99), (77, 100)]), rising[2]]    # a lower-low day in the middle
    f2 = pd.concat(broken + [day_from_bars("2021-03-04", SETUP)])
    assert len(hs.signals(f2, **{**kw, "struct_days": 3})) == 0
    assert len(hs.signals(f2, **{**kw, "struct_days": 0})) == 1


# ---------------------------------------------------------------- the setup
def test_full_long_setup_gives_buy_stop_at_break_high_and_stop_under_wick():
    s = sig(UP_UP, SETUP)
    assert len(s) == 1
    r = s.iloc[0]
    assert s.index[0] == frame(UP_UP, SETUP).index[78 + 5]          # the bar that trades through the trigger
    assert r["side"] == 1 and r["entry_px"] == 101.6 and r["stop"] == 99.5


def test_short_setup_is_the_mirror_image():
    bars = mirror(SETUP)
    f = pd.concat([mirror_frame(day_from_path("2021-03-01", UP_UP)), day_from_bars("2021-03-02", bars)])
    s = hs.signals(f, **hs.defaults)
    assert len(s) == 1
    assert s.iloc[0]["side"] == -1 and s.iloc[0]["entry_px"] == pytest.approx(200 - 101.6)
    assert s.iloc[0]["stop"] == pytest.approx(200 - 99.5)


def test_no_trade_against_the_bias():
    assert len(sig(DOWN_DOWN, SETUP)) == 0                         # long setup on a bearish day


@pytest.mark.parametrize("ratio,expect", [(0.10, 1), (0.40, 1), (0.80, 1), (0.90, 0)])
def test_wick_ratio_threshold(ratio, expect):
    assert len(sig(UP_UP, SETUP, wick_ratio=ratio)) == expect       # the wick bar's lower wick is 0.83


def test_no_sweep_no_trade():
    no_sweep = (100.5, 100.7, 100.1, 100.6)                         # low stays above the 100 range low
    assert len(sig(UP_UP, [RANGE] * 3 + [no_sweep, BREAK, FILL])) == 0


def test_closing_back_inside_range_before_fill_cancels():
    back_inside = (101.5, 101.55, 100.6, 100.8)                     # never reaches 101.6, closes under 101
    later = (100.9, 102.0, 100.8, 101.9)                            # would have filled the old trigger
    assert len(sig(UP_UP, [RANGE] * 3 + [WICK, BREAK, back_inside, later])) == 0


def test_pending_order_expires():
    quiet = (101.3, 101.55, 101.2, 101.4)                           # stays above 101, below the trigger
    bars = [RANGE] * 3 + [WICK, BREAK, quiet, quiet, quiet, FILL]
    assert len(sig(UP_UP, bars, pending_bars=3)) == 0               # order lived 3 bars, fill came on the 4th
    assert len(sig(UP_UP, bars, pending_bars=4)) == 1


def test_break_must_come_within_max_wait():
    slow = (100.6, 100.9, 100.4, 100.7)
    bars = [RANGE] * 3 + [WICK, slow, slow, slow, slow, BREAK, FILL]
    assert len(sig(UP_UP, bars, max_wait=4)) == 0
    assert len(sig(UP_UP, bars, max_wait=6)) == 1


def test_only_first_two_hours_and_one_trade_a_day():
    late = [RANGE] * 21 + [WICK, BREAK, FILL]                       # fill on bar 23 (11:25): allowed
    assert len(sig(UP_UP, late)) == 1
    too_late = [RANGE] * 22 + [WICK, BREAK, FILL]                   # fill on bar 24 (11:30): not allowed
    assert len(sig(UP_UP, too_late)) == 0
    twice = SETUP + [RANGE] * 6 + [WICK, BREAK, FILL]
    assert len(sig(UP_UP, twice)) == 1


def test_exit_modes_map_to_engine_columns():
    for mode, (tgt, be, tr) in EXIT_MODES.items():
        r = sig(UP_UP, SETUP, exit_mode=mode).iloc[0]
        for got, want in ((r["target_r"], tgt), (r["be_after_r"], be), (r["trail_r"], tr)):
            assert (np.isnan(got) and np.isnan(want)) or got == want
    assert EXIT_MODES["be_1r"][1] == 1.0 and EXIT_MODES["trail_1r"][2] == 1.0


def test_grid_is_under_100_and_covers_handout_variants():
    ps = hs.param_sets()
    assert 0 < len(ps) < 100
    assert {p["bias_mode"] for p in ps} == {"strict", "daily", "ema"}
    assert {p["wick_ratio"] for p in ps} == {0.10, 0.20, 0.30, 0.40}
    assert {p["exit_mode"] for p in ps} == set(EXIT_MODES)
    assert "htf_ema_len" not in hs.perturbable({**hs.defaults, "bias_mode": "strict"})
    assert "htf_ema_len" in hs.perturbable({**hs.defaults, "bias_mode": "ema"})


# ---------------------------------------------------------------- the engine pieces it relies on
def run(prev_knots, setup, **kw):
    f = frame(prev_knots, setup)
    p = dict(hs.defaults)
    p.update(kw)
    s = hs.signals(f, **p)
    return f, bt.run_signals("X", f, s, costs.LIQUID, hs.engine_config())


def test_buy_stop_fills_inside_the_signal_bar_at_the_trigger():
    f, t = run(UP_UP, SETUP)
    assert len(t) == 1
    r = t.iloc[0]
    fill_bar = f.index[78 + 5]
    assert r["entry_time"] == fill_bar and r["signal_time"] == fill_bar
    assert r["entry_raw"] == pytest.approx(101.6)                      # the bar opened at 101.5 < trigger
    assert r["entry"] > 101.6                                           # slippage on top


def test_gap_through_the_trigger_fills_at_the_open():
    gap = (102.0, 102.3, 101.9, 102.2)
    f, t = run(UP_UP, [RANGE] * 3 + [WICK, BREAK, gap])
    assert t.iloc[0]["entry_raw"] == pytest.approx(102.0)


def test_session_end_holds_to_close():
    after = [(101.7, 102.0, 101.6, 101.9)] * 10
    f, t = run(UP_UP, SETUP + after, exit_mode="session_end")
    assert t.iloc[0]["exit_reason"] == "eod"
