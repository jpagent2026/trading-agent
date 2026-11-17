"""Opening Range Breakout (ORB) on 5-minute bars.

Rules (common YouTube/textbook version, mechanised):
  * Opening range = high/low of the first `range_minutes` of RTH (5, 15 or 30).
  * After the range is complete, the FIRST 5-min bar that CLOSES above the range high
    is a long signal (below the range low = short). One trade per day.
  * Entry: next bar's open. Stop: the other side of the range ('other') or the range
    midpoint ('mid'). Target: `target_r` x risk, or none (exit at end of day).
  * No new entries after `last_entry_minute` minutes after the open (default 120 = 11:30 ET).
  * Flat at the close.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtest import EngineConfig
from .base import ParamSpec, Strategy, make_signals, session_info


class ORB(Strategy):
    name = "orb"
    source = "Generic opening-range breakout as described in the research brief (no prior user code found on the box)."
    defaults = {"range_minutes": 15, "stop_mode": "other", "target_r": 2.0, "last_entry_minute": 120,
                "direction": "both"}
    grid = {"range_minutes": [5, 15, 30], "stop_mode": ["other", "mid"], "target_r": [1.5, 2.0, 3.0, float("nan")]}
    specs = {"range_minutes": ParamSpec(step=5, minimum=5), "last_entry_minute": ParamSpec(step=5, minimum=30),
             "target_r": ParamSpec(minimum=0.5)}

    def engine_config(self) -> EngineConfig:
        return EngineConfig(eod_flatten=True, max_trades_per_day=1)

    def signals(self, bars, range_minutes=15, stop_mode="other", target_r=2.0, last_entry_minute=120,
                direction="both", **_):
        si = session_info(bars)
        in_range = si["minute"] < range_minutes
        rh = bars["high"].where(in_range).groupby(si["day"]).cummax().groupby(si["day"]).ffill()
        rl = bars["low"].where(in_range).groupby(si["day"]).cummin().groupby(si["day"]).ffill()
        # range complete only once a bar starting at/after range_minutes exists
        eligible = (si["minute"] >= range_minutes) & (si["minute"] < last_entry_minute)
        up = eligible & (bars["close"] > rh)
        dn = eligible & (bars["close"] < rl)
        if direction == "long":
            dn[:] = False
        if direction == "short":
            up[:] = False
        brk = up | dn
        first = brk & (brk.astype(int).groupby(si["day"]).cumsum() == 1)
        side = np.where(first & up, 1, np.where(first & dn, -1, 0))
        mid = (rh + rl) / 2
        stop = np.where(side > 0, rl if stop_mode == "other" else mid,
                        np.where(side < 0, rh if stop_mode == "other" else mid, np.nan))
        return make_signals(bars.index, side, stop, target_r)
