"""Engine extras used by the handout strategies: stop-entries, breakeven and trailing stops."""
import numpy as np
import pandas as pd
import pytest

from research import backtest as bt, costs
from research.tests.handout_helpers import day_from_bars

BARS = [(100, 100.1, 99.9, 100),        # 0 signal bar
        (100, 100.5, 99.8, 100.4),      # 1 entry bar (fills at the open)
        (100.4, 101.3, 99.9, 101.2),    # 2 best price 101.3 (> +1R), dips below entry INSIDE this bar
        (101.2, 101.25, 99.95, 100.1),  # 3 falls back through entry
        ] + [(100.1, 100.15, 100.05, 100.1)] * 6


def run(extra, bars=BARS, side=1, stop=99.0):
    f = day_from_bars("2021-03-02", bars)
    sig = pd.DataFrame({"side": [side], "stop": [stop], "target_r": [np.nan], "max_hold": [0], **extra}, index=[f.index[0]])
    return f, bt.run_signals("X", f, sig, costs.LIQUID, bt.EngineConfig(eod_flatten=True))


def test_without_extras_the_trade_rides_to_the_close():
    f, t = run({})
    assert t.iloc[0]["exit_reason"] == "eod"


def test_breakeven_after_1r_takes_effect_from_the_next_bar():
    f, t = run({"be_after_r": [1.0]})
    r = t.iloc[0]
    assert r["exit_reason"] == "stop"
    assert r["exit_idx"] == 3                        # bar 2 dipped below entry but BE was not active yet
    assert r["exit_raw"] == pytest.approx(r["entry"]) and r["r_net"] < 0.05


def test_breakeven_not_reached_means_original_stop_stays():
    f, t = run({"be_after_r": [3.0]})
    assert t.iloc[0]["exit_reason"] == "eod"


def test_trail_locks_in_profit_behind_the_best_price():
    f, t = run({"trail_r": [1.0]})
    r = t.iloc[0]
    risk = r["entry"] - 99.0
    assert r["exit_reason"] == "stop" and r["exit_idx"] == 3
    assert r["exit_raw"] == pytest.approx(101.3 - risk)
    assert r["r_gross"] > 0.2


def test_short_breakeven_mirrors():
    bars = [(200 - o, 200 - l, 200 - h, 200 - c) for o, h, l, c in BARS]
    f, t = run({"be_after_r": [1.0]}, bars=bars, side=-1, stop=101.0)
    assert t.iloc[0]["exit_idx"] == 3 and t.iloc[0]["exit_raw"] == pytest.approx(t.iloc[0]["entry"])


def test_stop_entry_fills_in_signal_bar_and_gaps_fill_at_open():
    f = day_from_bars("2021-03-02", [(100, 100.2, 99.9, 100.1), (100.2, 100.9, 100.1, 100.8)] + [(100.8, 100.9, 100.7, 100.8)] * 5)
    base = {"side": [1], "stop": [99.5], "target_r": [np.nan], "max_hold": [0]}
    sig = pd.DataFrame({**base, "entry_px": [100.5]}, index=[f.index[1]])
    t = bt.run_signals("X", f, sig, costs.LIQUID, bt.EngineConfig(eod_flatten=True))
    assert t.iloc[0]["entry_time"] == f.index[1] and t.iloc[0]["entry_raw"] == pytest.approx(100.5)
    sig_gap = pd.DataFrame({**base, "entry_px": [100.1]}, index=[f.index[1]])          # bar opens at 100.2, above 100.1
    t2 = bt.run_signals("X", f, sig_gap, costs.LIQUID, bt.EngineConfig(eod_flatten=True))
    assert t2.iloc[0]["entry_raw"] == pytest.approx(100.2)
    # a sell-stop mirrors: fills at the lower of the open and the trigger
    fs = day_from_bars("2021-03-02", [(100, 100.2, 99.9, 100.1), (99.8, 99.9, 99.2, 99.3)] + [(99.3, 99.4, 99.2, 99.3)] * 5)
    sgs = pd.DataFrame({"side": [-1], "stop": [100.5], "target_r": [np.nan], "max_hold": [0], "entry_px": [99.5]}, index=[fs.index[1]])
    t3 = bt.run_signals("X", fs, sgs, costs.LIQUID, bt.EngineConfig(eod_flatten=True))
    assert t3.iloc[0]["entry_raw"] == pytest.approx(99.5)


def test_random_benchmark_gets_the_same_exits():
    f, t = run({"be_after_r": [1.0], "trail_r": [np.nan]})
    assert "be_r" in t.columns and "trail_r" in t.columns and t.iloc[0]["be_r"] == 1.0
