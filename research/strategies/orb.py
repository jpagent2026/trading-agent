"""Opening Range Breakout (ORB) on 5-minute bars, following the user's handout (2026-11-17).

Handout rules, and how each is made mechanical here:
  * Trade the first 2 hours after the open: entries only on bars that START before
    09:30 + `last_entry_minute` (default 120 -> the last entry bar is 11:25-11:30 ET).
  * Opening range = high/low of the first `range_minutes` (5 or 15; 30 as an extra variant).
  * Breakout vs retest, each with a confirmation candle, entry on the NEXT bar's open:
      entry_mode='breakout': the first bar that CLOSES beyond the range is the confirmation
                             candle (close > range high = long, close < range low = short).
      entry_mode='retest'  : first a bar closes beyond the range (the breakout), later a bar
                             trades back to the range edge (long: low <= range high) and
                             CLOSES beyond it again. That bar is the confirmation candle.
  * Stop: stop_mode='range' = the other side of the opening range; 'confirm' = beyond the
    confirmation candle (its low for longs / its high for shorts).
  * Target: `target_r` x risk (1R, 2R as an extra) or NaN = hold to the end of the day (the
    handout's "hold toward high/low of day" - the exit is the close).
  * Longs and shorts; one trade per day; flat at the close.
  * `min_stop_pct`: trades whose stop is closer than this fraction of price are skipped (a
    stop of a few cents makes costs swamp 1R). Not tuned.
"""
from __future__ import annotations

import numpy as np

from ..backtest import EngineConfig
from .base import ParamSpec, Strategy, bar_minutes, day_grid


class ORB(Strategy):
    name = "orb"
    source = "User's handout dated 2026-11-17 (Strategy 1: Opening Range Breakout)."
    defaults = {"range_minutes": 15, "entry_mode": "breakout", "stop_mode": "range", "target_r": 1.0,
                "last_entry_minute": 120, "direction": "both", "min_stop_pct": 0.0005}
    grid = {"range_minutes": [5, 15, 30], "entry_mode": ["breakout", "retest"], "stop_mode": ["range", "confirm"],
            "target_r": [1.0, 2.0, float("nan")]}                       # 3 x 2 x 2 x 3 = 36 settings
    specs = {"range_minutes": ParamSpec(step=5, minimum=5), "last_entry_minute": ParamSpec(step=5, minimum=30),
             "target_r": ParamSpec(minimum=0.5), "min_stop_pct": ParamSpec(perturb=False)}

    def engine_config(self) -> EngineConfig:
        return EngineConfig(eod_flatten=True, max_trades_per_day=1)

    def signals(self, bars, range_minutes=15, entry_mode="breakout", stop_mode="range", target_r=1.0,
                last_entry_minute=120, direction="both", min_stop_pct=0.0005, **_):
        tf = bar_minutes(bars)
        ncols = int(last_entry_minute // tf)
        nr = max(int(round(range_minutes / tf)), 1)
        g = day_grid(bars, ncols)
        full = g.have[:, :nr].all(axis=1)                       # the whole opening range must exist
        rh = np.where(full, np.nanmax(g.H[:, :nr], axis=1), np.nan)
        rl = np.where(full, np.nanmin(g.L[:, :nr], axis=1), np.nan)
        side = np.zeros(g.C.shape, np.int8)
        stop = np.full(g.C.shape, np.nan)
        done = np.zeros(len(g.days), bool)
        broke_up = np.zeros(len(g.days), bool)
        broke_dn = np.zeros(len(g.days), bool)
        for c in range(nr, ncols - 1):                           # signal bar c, entry on bar c+1 (< 11:30)
            hi, lo, cl = g.H[:, c], g.L[:, c], g.C[:, c]
            up_close, dn_close = cl > rh, cl < rl
            if entry_mode == "retest":
                up = broke_up & (lo <= rh) & up_close            # earlier breakout, now a retest that holds
                dn = broke_dn & (hi >= rl) & dn_close
            else:
                up, dn = up_close, dn_close
            if direction == "long":
                dn = np.zeros_like(dn)
            elif direction == "short":
                up = np.zeros_like(up)
            broke_up |= up_close
            broke_dn |= dn_close
            s_stop = np.where(up, rl if stop_mode == "range" else lo, np.where(dn, rh if stop_mode == "range" else hi, np.nan))
            far_enough = np.abs(cl - s_stop) >= min_stop_pct * cl
            take = (up | dn) & ~done & far_enough & np.isfinite(s_stop)
            side[:, c] = np.where(take & up, 1, np.where(take & dn, -1, 0))
            stop[:, c] = np.where(take, s_stop, np.nan)
            done |= take
        return g.emit(side, stop, target_r)
