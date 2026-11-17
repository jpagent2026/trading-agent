import numpy as np
import pandas as pd
import pytest

from research import config
from research.strategies import EmaSmaPullback, EmaSmaRotation, REGISTRY
from research.strategies.ema_pullback import ROTATION, ma200_level
from research.synthetic import random_walk
from research.tests.handout_helpers import day_from_bars, mirror_frame
from research.tests.test_lookahead import scramble_after

ema = EmaSmaPullback()


def pullback_day(accel=False, second=False, date="2021-03-02"):
    """A steady (or accelerating) climb, a pullback into the 9 EMA / 20 SMA zone on bar 40, a
    resumption bar 41 (signal), optionally a second pullback (43) and resumption (44)."""
    k = np.arange(40)
    c = 100 + (0.001 * k ** 2 if accel else 0.05 * k)
    rows, prev = [], c[0]
    for x in c:
        rows.append((prev, max(prev, x) + 0.02, min(prev, x) - 0.02, x))
        prev = x
    top = c[-1]
    rows.append((top, top + 0.02, top - 0.4, top - 0.15))
    rows.append((top - 0.15, top + 0.45, top - 0.2, top + 0.3))
    if second:
        t = top + 0.3
        rows.append((t, t + 0.1, t - 0.02, t + 0.08))
        t += 0.08
        rows.append((t, t + 0.02, t - 0.45, t - 0.2))
        t -= 0.2
        rows.append((t, t + 0.6, t - 0.1, t + 0.45))
    return day_from_bars(date, rows)


def sig(frame, **kw):
    p = dict(ema.defaults, ext_atr=0)
    p.update(kw)
    return ema.signals(frame, **p)


def hhmm(ts):
    return ts.strftime("%H:%M")


def history(level, days=205):
    return pd.concat([day_from_bars(d, [(level,) * 4]) for d in pd.bdate_range(end="2020-12-31", periods=days)])


# ---------------------------------------------------------------- the pullback entry and its filters
def test_pullback_into_the_zone_then_resumption_gives_long_with_stop_below_pullback_low():
    s = sig(pullback_day())
    assert len(s) == 1
    assert hhmm(s.index[0]) == "12:55" and s.iloc[0]["side"] == 1
    assert s.iloc[0]["stop"] == pytest.approx(pullback_day().iloc[40]["low"])


def test_short_mirror():
    s = sig(mirror_frame(pullback_day()))
    assert len(s) == 1 and s.iloc[0]["side"] == -1
    assert s.iloc[0]["stop"] == pytest.approx(200 - pullback_day().iloc[40]["low"])


def test_slope_filter_skips_flat_and_too_steep():
    assert len(sig(pullback_day())) == 1                   # ~0.049% per bar
    assert len(sig(pullback_day(), slope_max=0.03)) == 0   # too steep for this band
    assert len(sig(pullback_day(), slope_min=0.06)) == 0   # too flat for this band


def test_extension_filter():
    assert len(sig(pullback_day(), ext_atr=0.5)) == 0       # price is several ATRs above the 20 SMA
    assert len(sig(pullback_day(), ext_atr=100)) == 1


def test_skip_first_pullback_after_a_steep_accelerating_move_only():
    acc = pullback_day(accel=True)
    assert len(sig(acc, skip_first=False)) == 1
    assert len(sig(acc, skip_first=True, steep_atr=1.0)) == 0              # first pullback after a steep, accelerating run
    assert len(sig(acc, skip_first=True, steep_atr=10.0)) == 1              # the run was not that steep
    steady = pullback_day(accel=False)
    assert len(sig(steady, skip_first=True, steep_atr=1.0)) == 1            # steep but NOT accelerating: not skipped
    two = pullback_day(accel=True, second=True)
    s = sig(two, skip_first=True, steep_atr=1.0)
    assert [hhmm(i) for i in s.index] == ["13:10"]                          # the second pullback of the leg is allowed


def test_zone_touch_variants_are_different():
    both = len(sig(pullback_day(), touch="ema")), len(sig(pullback_day(), touch="sma"))
    assert both == (1, 0)                                                   # the dip reaches the EMA but not the SMA


# ---------------------------------------------------------------- the 200 SMA
def test_ma200_daily_uses_only_completed_sessions():
    bars = random_walk("2019-01-01", "2019-03-29", 5, seed=2)
    lvl = ma200_level(bars, "daily", 10)
    closes = bars["close"].groupby(pd.DatetimeIndex(bars.index.date)).last()
    expect = closes.rolling(10).mean().shift(1)
    got = lvl.groupby(pd.DatetimeIndex(bars.index.date)).first()
    pd.testing.assert_series_equal(got, expect, check_names=False, check_freq=False)
    assert lvl.iloc[:78 * 10].isna().all()                                   # no value before 10 completed sessions


def test_ma200_hourly_uses_only_completed_hours():
    bars = random_walk("2019-01-01", "2019-01-31", 5, seed=4)
    lvl = ma200_level(bars, "hourly", 5)
    minute = (bars.index.hour * 60 + bars.index.minute) - 570
    key = pd.Series(bars.index.strftime("%Y%m%d") + "-" + (np.asarray(minute) // 60).astype(str), index=bars.index)
    last = bars["close"].groupby(key.to_numpy(), sort=False).last()
    expect = last.rolling(5).mean().shift(1).reindex(key.to_numpy()).to_numpy()
    np.testing.assert_allclose(lvl.to_numpy(), expect, equal_nan=True)
    cut = bars.index[300]
    alt = ma200_level(scramble_after(bars, cut), "hourly", 5)
    pd.testing.assert_series_equal(lvl[lvl.index <= cut], alt[alt.index <= cut])


def test_ma200_trend_filter_and_ma200_target():
    day = pullback_day()
    above = pd.concat([history(90), day])          # price (~102) is above the 200 SMA (90): trend ok for longs
    below = pd.concat([history(120), day])         # price below the 200 SMA (120): a long needs it as target
    assert len(sig(above, ma200_mode="trend")) == 1
    assert len(sig(below, ma200_mode="trend")) == 0
    assert len(sig(below, ma200_mode="off")) == 1
    # target = the 200 SMA, expressed in R from the signal bar's close
    s = sig(below, exit="ma200")
    assert len(s) == 1
    row = day.iloc[41]
    expect_r = (120 - row["close"]) / (row["close"] - s.iloc[0]["stop"])
    assert s.iloc[0]["target_r"] == pytest.approx(expect_r) and expect_r > 1
    assert len(sig(above, exit="ma200")) == 0       # the 200 is behind price: no target ahead, no trade
    # shorts mirror: price below the 200 SMA = trend ok for shorts; a 200 below price is their target
    mirrored = mirror_frame(pd.concat([history(90), day]))                  # 200 SMA at 110, price ~98
    assert len(sig(mirrored, ma200_mode="trend")) == 1
    assert sig(mirrored, ma200_mode="trend").iloc[0]["side"] == -1
    assert len(sig(mirror_frame(pd.concat([history(120), day])), ma200_mode="trend")) == 0   # 200 SMA at 80, below price
    tgt = sig(mirror_frame(pd.concat([history(120), day])), exit="ma200")
    assert len(tgt) == 1 and tgt.iloc[0]["target_r"] == pytest.approx(expect_r)


def test_exit_modes():
    day = pullback_day()
    assert sig(day, exit="1r").iloc[0]["target_r"] == 1.0
    assert sig(day, exit="2r").iloc[0]["target_r"] == 2.0
    assert np.isnan(sig(day, exit="close").iloc[0]["target_r"])


def test_grid_is_small_and_has_no_duplicates():
    ps = ema.param_sets()
    assert 0 < len(ps) < 100
    keys = [repr(sorted(p.items())) for p in ps]
    assert len(keys) == len(set(keys))
    assert not any(p["exit"] == "ma200" and p["ma200_mode"] == "trend" for p in ps)
    assert {p["exit"] for p in ps} == {"1r", "2r", "ma200", "close"}
    assert "steep_atr" not in ema.perturbable({**ema.defaults, "skip_first": False})
    assert "steep_atr" in ema.perturbable({**ema.defaults, "skip_first": True})


# ---------------------------------------------------------------- timeframe rotation
@pytest.fixture(scope="module")
def minute_bars():
    return random_walk("2019-01-01", "2019-04-30", 1, seed=5, sigma=0.0009)


def test_rotation_signals_are_on_segment_bar_closes(minute_bars):
    rot = EmaSmaRotation()
    assert rot.timeframe_minutes == 1
    s = rot.signals(minute_bars, **dict(rot.defaults, ext_atr=0, slope_min=0.001))
    assert len(s) > 20
    assert s.index.isin(minute_bars.index).all()
    m = (s.index.hour * 60 + s.index.minute) - 570
    ok = np.zeros(len(s), bool)
    for tf, t0, t1 in ROTATION:
        ok |= (m >= t0) & (m < t1) & ((m - t0) % tf == tf - 1)          # last 1-min bar of an aggregated bar
    assert ok.all()
    assert {int(x) for x in np.unique(m // 30)} <= set(range(13))
    assert len({tf for tf, t0, t1 in ROTATION}) == 3


def test_rotation_has_no_lookahead(minute_bars):
    rot = EmaSmaRotation()
    p = dict(rot.defaults, ext_atr=0, slope_min=0.001)
    cut = minute_bars.index[len(minute_bars) // 2 + 33]
    a = rot.signals(minute_bars, **p)
    b = rot.signals(scramble_after(minute_bars, cut), **p)
    pd.testing.assert_frame_equal(a[a.index <= cut], b[b.index <= cut])


def test_rotation_on_5_minute_bars_skips_the_2_minute_segment():
    bars = random_walk("2019-01-01", "2019-03-31", 5, seed=6, sigma=0.002)
    s = EmaSmaRotation().signals(bars, **dict(ema.defaults, ext_atr=0, slope_min=0.001))
    m = (s.index.hour * 60 + s.index.minute) - 570
    assert (m >= 30).all()


# ---------------------------------------------------------------- universe
def test_universe_includes_the_futures_stand_ins_and_handout_stocks():
    for sym in ("QQQ", "SPY", "DIA", "GLD", "NVDA", "AAPL", "TSLA"):
        assert sym in config.DEFAULT_UNIVERSE
    assert len(set(config.DEFAULT_UNIVERSE)) == len(config.DEFAULT_UNIVERSE)


def test_registry_has_four_ideas_and_every_grid_is_under_100():
    assert set(REGISTRY) == {"orb", "harry_sharp_wick", "ema9_sma20_pullback", "ema9_sma20_rotation"}
    for cls in REGISTRY.values():
        assert 0 < len(cls().param_sets()) < 100
