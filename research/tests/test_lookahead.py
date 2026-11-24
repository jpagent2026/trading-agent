"""No lookahead: changing the future must not change any earlier signal or label."""
import numpy as np
import pandas as pd
import pytest

from research import backtest as bt
from research import costs, strategies, validation as val
from research.strategies.base import prior_day


def scramble_after(bars, cut, seed=1):
    out = bars.copy()
    m = out.index > cut
    rng = np.random.default_rng(seed)
    f = np.exp(rng.normal(0, 0.05, m.sum()))
    for c in ("open", "high", "low", "close"):
        out.loc[m, c] = out.loc[m, c].to_numpy() * f
    out.loc[m, "high"] = out.loc[m, ["open", "high", "low", "close"]].max(axis=1)
    out.loc[m, "low"] = out.loc[m, ["open", "high", "low", "close"]].min(axis=1)
    return out


CASES = []
for s in strategies.all_strategies():
    sets = s.param_sets()
    CASES += [(s, sets[0]), (s, sets[len(sets) // 2]), (s, sets[-1])]


@pytest.mark.parametrize("strategy,params", CASES, ids=[f"{s.name}-{i % 3}" for i, (s, _) in enumerate(CASES)])
def test_future_bars_do_not_change_past_signals(strategy, params, year_bars):
    # cut in the middle of a session so intraday features are exercised too
    cuts = [year_bars.index[len(year_bars) // 2 + 17], year_bars.index[len(year_bars) // 3 + 40]]
    full = strategy.signals(year_bars, **params)
    for cut in cuts:
        alt = strategy.signals(scramble_after(year_bars, cut), **params)
        a, b = full[full.index <= cut], alt[alt.index <= cut]
        pd.testing.assert_frame_equal(a, b)
    assert len(full) > 0, "test data should produce signals"


def test_prior_day_uses_only_completed_sessions(year_bars):
    pdx = prior_day(year_bars)
    cut = year_bars.index[5000]
    alt = prior_day(scramble_after(year_bars, cut))
    pd.testing.assert_frame_equal(pdx[pdx.index <= cut], alt[alt.index <= cut])


def test_regime_labels_use_only_prior_days(planted_data):
    spy = planted_data["SPY"]
    cut = spy.index[len(spy) // 2]
    a = val.regime_labels(spy)
    b = val.regime_labels(scramble_after(spy, cut))
    day = pd.Timestamp(cut.date())
    pd.testing.assert_frame_equal(a[a.index <= day], b[b.index <= day])


def test_fills_happen_at_next_bar_open(year_bars):
    s = strategies.get("orb")
    sig = s.signals(year_bars, **s.defaults)
    t = bt.run_signals("X", year_bars, sig, costs.LIQUID, s.engine_config())
    assert len(t) > 50
    pos = year_bars.index.get_indexer(pd.DatetimeIndex(t["signal_time"]))
    assert (year_bars.index[pos + 1] == pd.DatetimeIndex(t["entry_time"])).all()
    np.testing.assert_allclose(t["entry_raw"].to_numpy(), year_bars["open"].to_numpy()[pos + 1])
