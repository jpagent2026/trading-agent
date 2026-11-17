import numpy as np
import pandas as pd
import pytest

from research import backtest as bt
from research.costs import LIQUID, SMALL_CAP, CostModel, for_symbol
from research.config import ET


def test_fill_math():
    c = CostModel(slippage_bps=3, half_spread_bps=1)
    assert c.fill(100, +1, opening=True) == pytest.approx(100.04)   # buy pays up 4 bps
    assert c.fill(100, +1, opening=False) == pytest.approx(99.96)   # sell gets 4 bps less
    assert c.fill(100, -1, opening=True) == pytest.approx(99.96)    # short sale
    assert c.fill(100, -1, opening=False) == pytest.approx(100.04)  # buy to cover


def test_regulatory_fees_and_borrow():
    c = CostModel()
    assert c.sell_fees(100, 1000) == pytest.approx(100 * 1000 * 27.80 / 1e6 + 1000 * 0.000166)
    assert c.sell_fees(10, 10_000_000) == pytest.approx(10 * 1e7 * 27.80 / 1e6 + 8.30)  # TAF cap
    assert c.borrow(100, 100, hours=24) == pytest.approx(100 * 100 * 0.005 / 360)
    assert c.borrow(100, 100, hours=0.1) == c.borrow(100, 100, hours=1)  # 1 hour minimum
    long_fees = c.trade_fees(+1, 100, 101, 100, 2)
    short_fees = c.trade_fees(-1, 100, 99, 100, 2)
    assert long_fees == pytest.approx(c.sell_fees(101, 100))
    assert short_fees == pytest.approx(c.sell_fees(100, 100) + c.borrow(100, 100, 2))


def test_tiers():
    assert for_symbol("SPY") is LIQUID
    assert for_symbol("TINYCAP") is SMALL_CAP
    assert SMALL_CAP.side_bps > LIQUID.side_bps


def bars_from(rows, day="2024-06-03"):
    idx = pd.date_range(f"{day} 09:30", periods=len(rows), freq="5min", tz=ET)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1000)


def test_long_target_trade_r_matches_hand_math():
    c = CostModel(slippage_bps=3, half_spread_bps=1, sec_fee_per_million=0, taf_per_share=0)
    b = bars_from([[100, 100.2, 99.9, 100.1], [100, 100.5, 99.8, 100.4], [100.4, 103, 100.3, 102.5],
                   [102.5, 102.6, 102, 102.2]])
    sig = pd.DataFrame({"side": [1], "stop": [99.0], "target_r": [2.0], "max_hold": [0]}, index=b.index[:1])
    t = bt.run_signals("X", b, sig, c, bt.EngineConfig(nominal_equity=100_000, risk_frac=0.01))
    assert len(t) == 1
    r = t.iloc[0]
    entry = 100 * 1.0004
    risk = entry - 99
    target = entry + 2 * risk
    assert r["entry"] == pytest.approx(entry) and r["entry_time"] == b.index[1]  # next bar open
    assert r["exit_reason"] == "target" and r["exit_raw"] == pytest.approx(target)
    exit_fill = target * (1 - 0.0004)
    assert r["r_net"] == pytest.approx((exit_fill - entry) / risk)
    assert r["r_net"] < 2.0 < r["r_gross"] + 0.1


def test_stop_and_target_same_bar_counts_as_stop_and_gaps_fill_at_open():
    c = CostModel(slippage_bps=0, half_spread_bps=0, sec_fee_per_million=0, taf_per_share=0)
    b = bars_from([[100, 100, 100, 100], [100, 100, 100, 100], [100, 103, 98, 100], [100, 100, 100, 100]])
    sig = pd.DataFrame({"side": [1], "stop": [99.0], "target_r": [2.0], "max_hold": [0]}, index=b.index[:1])
    t = bt.run_signals("X", b, sig, c)
    assert t.iloc[0]["exit_reason"] == "stop" and t.iloc[0]["r_net"] == pytest.approx(-1.0)
    gap = bars_from([[100, 100, 100, 100], [100, 100, 100, 100], [97, 97.5, 96, 97], [97, 97, 97, 97]])
    t = bt.run_signals("X", gap, sig, c)
    assert t.iloc[0]["exit_raw"] == 97 and t.iloc[0]["r_net"] == pytest.approx(-3.0)


def test_short_eod_flatten_and_borrow():
    c = CostModel(slippage_bps=0, half_spread_bps=0, sec_fee_per_million=0, taf_per_share=0, borrow_annual_pct=36)
    b = bars_from([[100, 100, 100, 100], [100, 100.5, 99, 99.5], [99.5, 99.6, 99, 99.2]])
    sig = pd.DataFrame({"side": [-1], "stop": [101.0], "target_r": [np.nan], "max_hold": [0]}, index=b.index[:1])
    t = bt.run_signals("X", b, sig, c, bt.EngineConfig(nominal_equity=100_000, risk_frac=0.01))
    r = t.iloc[0]
    assert r["exit_reason"] == "eod" and r["exit_raw"] == 99.2
    shares = r["shares"]
    borrow = 100 * shares * 0.36 * 1.0 / (24 * 360)  # 10 min held -> 1h minimum
    assert r["fees"] == pytest.approx(borrow)
    assert r["r_net"] == pytest.approx((0.8 * shares - borrow) / (1.0 * shares))


def test_signal_on_last_bar_does_not_carry_overnight():
    c = CostModel()
    b = pd.concat([bars_from([[100, 101, 99, 100]] * 3, "2024-06-03"), bars_from([[100, 101, 99, 100]] * 3, "2024-06-04")])
    sig = pd.DataFrame({"side": [1], "stop": [98.0], "target_r": [2.0], "max_hold": [0]}, index=b.index[2:3])
    assert len(bt.run_signals("X", b, sig, c)) == 0
