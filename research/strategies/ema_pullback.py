"""9 EMA / 20 SMA / 200 SMA trend pullback, following the user's handout (2026-11-17).

Handout rules, and how each is made mechanical (repeated in research/RULES.md):
  * Indicators on the strategy's bars (5-minute by default): EMA(9) and SMA(20) of closes,
    continuous across sessions; SMA(200) as the higher-timeframe level (see below).
  * Trade with the trend: uptrend = EMA above SMA and both rising over `slope_bars` bars.
    Shorts mirror everything.
  * Entry zone = between the 9 EMA (top) and the 20 SMA (bottom). A pullback bar is one that
    trades down into the zone (touch='ema': low <= EMA; touch='sma': low <= SMA, a deeper
    touch) while still closing at/above the SMA. The NEXT bar is the signal bar: still in the
    uptrend and closing above the pullback bar's high; entry on the bar after (next open).
  * Slope filter on the 20 SMA: percent change per bar, averaged over `slope_bars` bars, must be
    inside [slope_min, slope_max] (percent per 5-min bar; scaled for other bar sizes). This skips
    a flat 20 SMA (no momentum) and one that is too steep (exhausted).
  * Extension filter: no entry when the signal bar's close is more than `ext_atr` x ATR(14)
    away from the 20 SMA (0 turns it off).
  * Skip the first pullback after a steep, accelerating move (skip_first=True): the first
    pullback of a trend leg is skipped when, just before it began, price had moved more than
    `steep_atr` x ATR over 12 bars in the trade direction AND the latest 6 bars moved more than
    the 6 before them. A "leg" starts when the trend condition switches on and ends when it
    switches off. Later pullbacks of the same leg are allowed.
  * 200 SMA as a higher-timeframe level (ma200_frame='daily': the 200-day SMA of completed
    sessions' closes, known at the open; 'hourly': SMA of the last 200 completed hourly closes):
      ma200_mode='trend': longs only above it, shorts only below it;
      exit='ma200'      : target the 200 SMA (the trade must have it at least 1R ahead, so
                          this takes longs BELOW the 200 and ignores ma200_mode).
  * Stop: below the pullback low (the lowest low of the pullback bar and the signal bar).
  * Exit: '1r', '2r', 'ma200' or 'close' (hold to the end of the day). Entries 09:45-15:30 ET;
    several trades a day are allowed once flat.

NOT mechanised (the handout says so itself): catalyst / stock selection, "zone quality", and
the preference for more touches. Multi-timeframe alignment is only approximated by the
rotation variant (see EmaSmaRotation).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import data as dl
from ..backtest import EngineConfig
from .base import ParamSpec, Strategy, bar_minutes, make_signals, memo

# Simplified timeframe rotation from the handout: (bar minutes, session minute from, session minute to)
ROTATION = ((2, 0, 30), (5, 30, 150), (15, 150, 390))   # 2-min before 10:00, 5-min 10:00-12:00, 15-min after


def _day_codes(bars):
    return pd.DatetimeIndex(bars.index.date)


def ma200_level(bars: pd.DataFrame, frame: str = "daily", length: int = 200) -> pd.Series:
    """The 200 SMA as known on each bar, from COMPLETED higher-timeframe bars only."""
    def build():
        c = bars["close"]
        if frame == "hourly":
            idx = bars.index
            minute = (idx.hour * 60 + idx.minute) - (9 * 60 + 30)
            block = pd.Series(_day_codes(bars).strftime("%Y%m%d") + "-" + (np.asarray(minute) // 60).astype(str),
                              index=idx)
            last = c.groupby(block.to_numpy(), sort=False).last()
            sma = last.rolling(length, min_periods=length).mean().shift(1)     # previous completed block
            return pd.Series(sma.reindex(block.to_numpy()).to_numpy(), index=idx)
        daily = c.groupby(_day_codes(bars)).last()
        sma = daily.rolling(length, min_periods=length).mean().shift(1)         # completed sessions only
        return pd.Series(sma.reindex(_day_codes(bars)).to_numpy(), index=bars.index)
    return memo(bars, ("ma200", frame, length), build)


def _atr(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    def build():
        h, l, c = bars["high"], bars["low"], bars["close"]
        same_day = pd.Series(_day_codes(bars), index=bars.index).eq(pd.Series(_day_codes(bars), index=bars.index).shift(1))
        pc = c.shift(1).where(same_day)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        return tr.rolling(n, min_periods=n).mean()
    return memo(bars, ("atr", n), build)


def pullback_signals(bars, ema_len=9, sma_len=20, slope_bars=3, touch="ema", exit="2r", ma200_mode="off",
                     ma200_frame="daily", slope_min=0.01, slope_max=0.10, ext_atr=2.0, skip_first=False,
                     steep_atr=4.0, first_minute=15, last_minute=360, slope_scale=1.0, window=None, **_):
    """Signals on `bars` (any bar size). window: optional boolean Series restricting signal bars."""
    ema_len, sma_len, slope_bars = int(ema_len), int(sma_len), int(slope_bars)
    idx = bars.index
    minute = pd.Series((idx.hour * 60 + idx.minute) - (9 * 60 + 30), index=idx)
    day = pd.Series(_day_codes(bars), index=idx)
    c = bars["close"]
    ema = c.ewm(span=ema_len, adjust=False, min_periods=ema_len).mean()
    sma = c.rolling(sma_len, min_periods=sma_len).mean()
    atr = _atr(bars)
    slope_pct = 100 * (sma - sma.shift(slope_bars)) / sma.shift(slope_bars) / slope_bars   # % per bar
    ma200 = ma200_level(bars, ma200_frame)
    ok_window = (minute >= first_minute) & (minute < last_minute) & day.eq(day.shift(1))
    if window is not None:
        ok_window &= window
    lo_s, hi_s = slope_min * slope_scale, slope_max * slope_scale

    parts = []
    for sign in (1, -1):
        # Mirror prices for shorts so the logic below is the long logic only.
        h, l, cl = (bars["high"], bars["low"], c) if sign > 0 else (-bars["low"], -bars["high"], -c)
        e, s, m2 = sign * ema, sign * sma, sign * ma200
        up = (e > s) & (e > e.shift(slope_bars)) & (s > s.shift(slope_bars))
        line = e if touch == "ema" else s
        pull = up & (l <= line) & (cl >= s)
        slope_ok = (sign * slope_pct >= lo_s) & (sign * slope_pct <= hi_s)
        ext_ok = (ext_atr <= 0) | ((cl - s).abs() <= ext_atr * atr)
        sig = ok_window & up & slope_ok & ext_ok & pull.shift(1, fill_value=False) & (cl > h.shift(1))
        if skip_first:
            leg = (up & ~up.shift(1, fill_value=False)).cumsum()
            start = pull & ~pull.shift(1, fill_value=False)
            pb_no = start.astype(int).groupby(leg).cumsum()
            steep = ((cl - cl.shift(12)) > steep_atr * atr) & ((cl - cl.shift(6)) > (cl.shift(6) - cl.shift(12)))
            steep_at_start = steep.astype(float).shift(1).where(start).ffill().fillna(0.0) > 0
            first_and_steep = (pb_no == 1) & pull & steep_at_start
            sig &= ~first_and_steep.shift(1, fill_value=False)
        if ma200_mode == "trend" and exit != "ma200":
            sig &= cl > m2
        stop = np.minimum(l, l.shift(1))                       # mirrored: -(max high)
        risk = cl - (stop)                                      # positive when valid
        if exit == "ma200":
            r_to = (m2 - cl) / risk.where(risk > 0)
            sig &= r_to >= 1.0
            target = r_to
        else:
            target = pd.Series({"1r": 1.0, "2r": 2.0}.get(exit, np.nan), index=idx)
        sig &= risk > 0
        df = pd.DataFrame({"side": np.where(sig, sign, 0), "stop": sign * stop, "target_r": target})
        parts.append(df[sig])
    out = pd.concat(parts).sort_index(kind="stable") if parts else pd.DataFrame()
    return make_signals(out.index, out["side"].to_numpy(), out["stop"].to_numpy(), out["target_r"].to_numpy())


class EmaSmaPullback(Strategy):
    name = "ema9_sma20_pullback"
    source = "User's handout dated 2026-11-17 (Strategy 3: 9 EMA / 20 SMA / 200 SMA trend pullback)."
    timeframe_minutes = 5
    defaults = {"ema_len": 9, "sma_len": 20, "slope_bars": 3, "touch": "ema", "exit": "2r", "ma200_mode": "off",
                "ma200_frame": "daily", "slope_min": 0.01, "slope_max": 0.10, "ext_atr": 2.0, "skip_first": False,
                "steep_atr": 4.0, "first_minute": 15, "last_minute": 360}
    grid = {"touch": ["ema", "sma"], "ma200_mode": ["off", "trend"], "exit": ["1r", "2r", "ma200", "close"],
            "slope_min": [0.005, 0.015], "skip_first": [False, True]}   # 2 x 2 x 4 x 2 x 2 = 64, minus 8 duplicates = 56
    specs = {"ema_len": ParamSpec(step=1, minimum=2), "sma_len": ParamSpec(step=1, minimum=3),
             "slope_bars": ParamSpec(step=1, minimum=1), "slope_min": ParamSpec(minimum=0.001),
             "slope_max": ParamSpec(minimum=0.02), "ext_atr": ParamSpec(minimum=0.5),
             "steep_atr": ParamSpec(minimum=1.0),
             "first_minute": ParamSpec(step=5, minimum=5), "last_minute": ParamSpec(step=5, minimum=30)}

    def engine_config(self) -> EngineConfig:
        return EngineConfig(eod_flatten=True)

    def param_sets(self) -> list[dict]:
        # exit='ma200' ignores the trend filter, so (trend, ma200) would duplicate (off, ma200)
        return [p for p in super().param_sets() if not (p["exit"] == "ma200" and p["ma200_mode"] == "trend")]

    def perturbable(self, params: dict) -> list[str]:
        keys = super().perturbable(params)
        if not params.get("skip_first"):
            keys = [k for k in keys if k != "steep_atr"]
        return keys

    def signals(self, bars, **params):
        return pullback_signals(bars, slope_scale=bar_minutes(bars) / 5.0, **params)


class EmaSmaRotation(EmaSmaPullback):
    """Same rules with the handout's timeframe rotation, simplified: 2-minute bars before 10:00 ET,
    5-minute bars 10:00-12:00, 15-minute bars after 12:00. Runs on 1-minute data: each segment's
    signals come from that segment's own bars (their own EMA/SMA), the signal is acted on at the
    next 1-minute open after the aggregated bar closes. No multi-timeframe confluence (e.g. the
    20 on 2-min aligning with the 9 on 5-min) - that part of the handout is not implemented."""
    name = "ema9_sma20_rotation"
    timeframe_minutes = 1
    source = "User's handout dated 2026-11-17 (Strategy 3, timeframe-rotation variant, simplified)."

    def signals(self, bars, **params):
        base = bar_minutes(bars)
        parts = []
        for tf, t0, t1 in ROTATION:
            if tf % base:
                continue                      # cannot build this bar size from the bars we have
            frame = bars if tf == base else memo(bars, ("resample", tf), lambda tf=tf: dl.resample(bars, tf))
            mins = pd.Series((frame.index.hour * 60 + frame.index.minute) - (9 * 60 + 30), index=frame.index)
            sig = pullback_signals(frame, slope_scale=tf / 5.0, window=(mins >= t0) & (mins < t1), **params)
            if len(sig):
                # the aggregated bar is complete after its last base bar
                sig.index = sig.index + pd.Timedelta(minutes=tf - base)
                parts.append(sig)
        if not parts:
            return make_signals(bars.index[:0], np.zeros(0), np.zeros(0), np.zeros(0))
        out = pd.concat(parts).sort_index(kind="stable")
        out = out[out.index.isin(bars.index)]
        out.index.name = bars.index.name
        return out
