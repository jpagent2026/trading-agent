"""'Harry Sharp-style' daily bias + 5-minute wick rejection.

INTERPRETATION - FLAGGED FOR REVIEW. No earlier implementation of this idea was found on
the box (searched /workspace incl. turtle-backtest, bot-challenge, agent200, q.py, n.py,
f.py for ORB / Harry / Sharp / wick / 9EMA / 20SMA). These rules mechanise the brief's
description and should be checked against the user's understanding of the method:

  * Bias (from the PREVIOUS completed session only):
      bias_mode='direction': long if prior close > prior open, short if below.
      bias_mode='midpoint' : long if prior close > prior-day midpoint (H+L)/2, short if below.
  * Level: level='pdl' -> prior-day low for longs / prior-day high for shorts (a sweep of
    yesterday's extreme); level='open' -> today's RTH opening price.
  * Long entry signal: a 5-min bar (09:35-15:00 ET) whose low trades BELOW the level and
    closes back ABOVE it, with a lower wick >= wick_frac of the bar's range. Shorts mirror.
  * Entry next bar open; stop just beyond the wick (wick extreme - stop_buffer x bar range);
    target `target_r` x risk; flat at the close; at most one trade per day.
"""
from __future__ import annotations

import numpy as np

from ..backtest import EngineConfig
from .base import ParamSpec, Strategy, make_signals, prior_day, session_info


class HarrySharpWick(Strategy):
    name = "harry_sharp_wick"
    source = ("Documented interpretation of the brief (prior user code not found on the box) - "
              "needs the user's review.")
    defaults = {"bias_mode": "direction", "level": "pdl", "wick_frac": 0.5, "target_r": 2.0,
                "stop_buffer": 0.1, "first_minute": 5, "last_minute": 330}
    grid = {"bias_mode": ["direction", "midpoint"], "level": ["pdl", "open"],
            "wick_frac": [0.4, 0.5, 0.6], "target_r": [1.5, 2.0, 3.0]}
    specs = {"wick_frac": ParamSpec(minimum=0.1), "target_r": ParamSpec(minimum=0.5),
             "stop_buffer": ParamSpec(minimum=0.0), "first_minute": ParamSpec(step=5, minimum=5),
             "last_minute": ParamSpec(step=5, minimum=30)}

    def engine_config(self) -> EngineConfig:
        return EngineConfig(eod_flatten=True, max_trades_per_day=1)

    def signals(self, bars, bias_mode="direction", level="pdl", wick_frac=0.5, target_r=2.0,
                stop_buffer=0.1, first_minute=5, last_minute=330, **_):
        si = session_info(bars)
        pd_ = prior_day(bars)
        if bias_mode == "direction":
            long_bias = pd_["pd_close"] > pd_["pd_open"]
            short_bias = pd_["pd_close"] < pd_["pd_open"]
        else:
            mid = (pd_["pd_high"] + pd_["pd_low"]) / 2
            long_bias = pd_["pd_close"] > mid
            short_bias = pd_["pd_close"] < mid
        if level == "pdl":
            lvl_long, lvl_short = pd_["pd_low"], pd_["pd_high"]
        else:
            day_open = bars["open"].groupby(si["day"]).transform("first")  # known at 09:30
            lvl_long = lvl_short = day_open
        o, h, l, c = bars["open"], bars["high"], bars["low"], bars["close"]
        rng = (h - l).where(h > l)
        lower_wick = (np.minimum(o, c) - l) / rng
        upper_wick = (h - np.maximum(o, c)) / rng
        window = (si["minute"] >= first_minute) & (si["minute"] < last_minute)
        go_long = window & long_bias & (l < lvl_long) & (c > lvl_long) & (lower_wick >= wick_frac)
        go_short = window & short_bias & (h > lvl_short) & (c < lvl_short) & (upper_wick >= wick_frac)
        side = np.where(go_long, 1, np.where(go_short, -1, 0))
        stop = np.where(side > 0, l - stop_buffer * rng, np.where(side < 0, h + stop_buffer * rng, np.nan))
        return make_signals(bars.index, side, stop, target_r)
