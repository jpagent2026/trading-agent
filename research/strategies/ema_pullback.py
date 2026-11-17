"""9 EMA / 20 SMA trend pullback on 5-minute bars.

Rules (mechanised from the brief; no prior user code found on the box):
  * Indicators on 5-min RTH closes, continuous across sessions: EMA(ema_len=9), SMA(sma_len=20).
  * Uptrend on a bar: EMA > SMA, EMA rising and SMA rising (each above its own value
    `slope_bars` bars earlier). Downtrend mirrors.
  * Pullback bar (bar i-1, in an uptrend): its low touches the line (touch='ema' -> EMA,
    touch='sma' -> SMA) while it closes at/above the SMA.
  * Resumption = signal bar i: still in an uptrend and closes above the pullback bar's high.
  * Entry next bar open; stop below the pullback low (min low of bars i-1 and i);
    target `target_r` x risk or none; flat at the close. Entries 09:45-15:30 ET.
    Re-entry later the same day is allowed once flat. Shorts mirror everything.
"""
from __future__ import annotations

import numpy as np

from ..backtest import EngineConfig
from .base import ParamSpec, Strategy, make_signals, session_info


class EmaSmaPullback(Strategy):
    name = "ema9_sma20_pullback"
    source = "Mechanised from the brief's description (prior user code not found on the box)."
    defaults = {"ema_len": 9, "sma_len": 20, "slope_bars": 3, "touch": "ema", "target_r": 2.0,
                "first_minute": 15, "last_minute": 360}
    grid = {"touch": ["ema", "sma"], "slope_bars": [2, 3, 5], "target_r": [1.5, 2.0, 3.0, float("nan")]}
    specs = {"ema_len": ParamSpec(step=1, minimum=2), "sma_len": ParamSpec(step=1, minimum=3),
             "slope_bars": ParamSpec(step=1, minimum=1), "target_r": ParamSpec(minimum=0.5),
             "first_minute": ParamSpec(step=5, minimum=5), "last_minute": ParamSpec(step=5, minimum=30)}

    def engine_config(self) -> EngineConfig:
        return EngineConfig(eod_flatten=True)

    def signals(self, bars, ema_len=9, sma_len=20, slope_bars=3, touch="ema", target_r=2.0,
                first_minute=15, last_minute=360, **_):
        ema_len, sma_len, slope_bars = int(ema_len), int(sma_len), int(slope_bars)
        si = session_info(bars)
        c, h, l = bars["close"], bars["high"], bars["low"]
        ema = c.ewm(span=ema_len, adjust=False, min_periods=ema_len).mean()
        sma = c.rolling(sma_len, min_periods=sma_len).mean()
        up = (ema > sma) & (ema > ema.shift(slope_bars)) & (sma > sma.shift(slope_bars))
        dn = (ema < sma) & (ema < ema.shift(slope_bars)) & (sma < sma.shift(slope_bars))
        line = ema if touch == "ema" else sma
        pull_long = up & (l <= line) & (c >= sma)
        pull_short = dn & (h >= line) & (c <= sma)
        same_day = si["day"].eq(si["day"].shift(1))
        window = (si["minute"] >= first_minute) & (si["minute"] < last_minute) & same_day
        go_long = window & up & pull_long.shift(1, fill_value=False) & (c > h.shift(1))
        go_short = window & dn & pull_short.shift(1, fill_value=False) & (c < l.shift(1))
        side = np.where(go_long, 1, np.where(go_short, -1, 0))
        stop = np.where(side > 0, np.minimum(l, l.shift(1)), np.where(side < 0, np.maximum(h, h.shift(1)), np.nan))
        return make_signals(bars.index, side, stop, target_r)
