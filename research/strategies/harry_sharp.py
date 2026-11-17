"""'Harry Sharp-style' bias + structure + liquidity wick, following the user's handout (2026-11-17).

Handout rules, and the mechanical choices made for each (all repeated in research/RULES.md):

  HIGHER-TIMEFRAME BIAS - from COMPLETED higher-timeframe candles only (the previous session):
    bias_mode='strict' : previous DAILY candle and previous 4H candle must agree
                         (candle up = close > open). Both up = long bias, both down = short.
    bias_mode='daily'  : previous daily candle only.
    bias_mode='ema'    : previous daily close and previous 4H close are both above (below)
                         their EMA(htf_ema_len) = long (short) bias.
    4H candles are built from the 5-minute RTH bars as two blocks per session: 09:30-13:30 and
    13:30-16:00 (a half-day has one block). At the open the "previous 4H candle" is therefore
    yesterday's 13:30-16:00 block (the afternoon leg), which is a different candle from the
    daily one, so 'strict' is not just the daily bias twice.

  MARKET STRUCTURE (optional, struct_days > 0): the last `struct_days` completed daily candles
    make higher highs AND higher lows in a row (longs) / lower highs and lower lows (shorts).

  RANGE: the high/low of the previous `range_bars` 5-minute bars of today (at least 3 bars,
    so the first possible setup is the 09:45 bar).

  LIQUIDITY WICK (the spring): a bar that trades below the range low and closes back above it
    with a lower wick of at least `wick_ratio` of the bar's full range (shorts mirror: above the
    range high, close back below, upper wick). Handout sweep: 0.10-0.40.

  STRUCTURE BREAK + CLOSE: within `max_wait` bars after the wick bar, a bar CLOSES above the
    range high (shorts: below the range low). That is the break bar.

  ENTRY: a buy-stop at the break bar's high (sell-stop at its low). It is live for the next
    `pending_bars` bars and fills in the first bar that trades through it (a gap fills at the
    open). INVALIDATED (cancelled) if a bar closes back inside the range (at or below the
    broken range high) before it fills.

  STOP: beyond the wick bar's extreme (its low for longs / high for shorts).
  EXITS (exit_mode): 'fixed_1r' = target 1R; 'be_1r' = stop moves to breakeven once +1R is
    reached, target 2R; 'trail_1r' = once +1R is reached the stop trails 1R behind the best
    price, no target; 'session_end' = no target, hold to the close. (The engine is all-in /
    all-out, so "move to BE at 1R" cannot also scale out half; it is modelled as BE + 2R.)

  SESSION: only the first 2 hours of the NY session (fills before 11:30 ET). One trade a day,
  flat at the close. Longs and shorts, chosen by the bias.

NOT mechanised: "clean zone / path of least resistance" and the judgement of structure quality.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtest import EngineConfig
from .base import ParamSpec, Strategy, bar_minutes, day_grid, memo

EXIT_MODES = {  # target_r, be_after_r, trail_r
    "fixed_1r": (1.0, np.nan, np.nan),
    "be_1r": (2.0, 1.0, np.nan),
    "trail_1r": (np.nan, np.nan, 1.0),
    "session_end": (np.nan, np.nan, np.nan),
}


def htf_candles(bars: pd.DataFrame) -> pd.DataFrame:
    """Per session: daily OHLC plus the LAST 4H block (13:30-16:00, or the only block on a half-day)
    and the first block's close. Indexed by session date; every value is from a completed session."""
    idx = bars.index
    date = pd.DatetimeIndex(idx.date)
    minute = pd.Series((idx.hour * 60 + idx.minute) - (9 * 60 + 30), index=idx)
    df = pd.DataFrame({"date": date, "minute": minute.to_numpy(), "o": bars["open"].to_numpy(),
                       "h": bars["high"].to_numpy(), "l": bars["low"].to_numpy(), "c": bars["close"].to_numpy()})
    g = df.groupby("date")
    out = pd.DataFrame({"d_open": g["o"].first(), "d_high": g["h"].max(), "d_low": g["l"].min(),
                        "d_close": g["c"].last()})
    a = df[df["minute"] < 240].groupby("date")
    b = df[df["minute"] >= 240].groupby("date")
    a_close, a_open = a["c"].last().reindex(out.index), a["o"].first().reindex(out.index)
    b_close, b_open = b["c"].last().reindex(out.index), b["o"].first().reindex(out.index)
    out["a_close"] = a_close
    out["b_open"] = b_open.fillna(a_open)       # half-day: the only block is the last block
    out["b_close"] = b_close.fillna(a_close)
    return out


def _sign(x: pd.Series) -> pd.Series:
    return np.sign(x).fillna(0)


def bias_by_day(candles: pd.DataFrame, bias_mode: str, ema_len: int, struct_days: int) -> pd.Series:
    """+1 / -1 / 0 for each session, using only the PREVIOUS sessions' completed candles."""
    d_dir = _sign(candles["d_close"] - candles["d_open"])
    h_dir = _sign(candles["b_close"] - candles["b_open"])
    if bias_mode == "daily":
        bias = d_dir
    elif bias_mode == "ema":
        ema_d = candles["d_close"].ewm(span=ema_len, adjust=False).mean()
        blocks = pd.concat([candles["a_close"].fillna(candles["d_close"]), candles["b_close"]], axis=1)
        flat = pd.Series(blocks.to_numpy().reshape(-1)).ffill()
        ema_b = flat.ewm(span=ema_len, adjust=False).mean().to_numpy()[1::2]
        bias_d = _sign(candles["d_close"] - ema_d)
        bias_h = _sign(candles["b_close"] - pd.Series(ema_b, index=candles.index))
        bias = bias_d.where(bias_d == bias_h, 0)
    else:  # strict
        bias = d_dir.where(d_dir == h_dir, 0)
    bias = bias.shift(1).fillna(0)                      # known at the open of the NEXT session
    if struct_days and struct_days > 1:
        up = pd.Series(True, index=candles.index)
        dn = pd.Series(True, index=candles.index)
        for i in range(int(struct_days) - 1):
            up &= (candles["d_high"].shift(1 + i) > candles["d_high"].shift(2 + i)) & \
                  (candles["d_low"].shift(1 + i) > candles["d_low"].shift(2 + i))
            dn &= (candles["d_high"].shift(1 + i) < candles["d_high"].shift(2 + i)) & \
                  (candles["d_low"].shift(1 + i) < candles["d_low"].shift(2 + i))
        bias = bias.where(((bias > 0) & up) | ((bias < 0) & dn), 0)
    return bias


def _long_state_machine(O, H, L, C, ok_day, wick_ratio, range_bars, max_wait, pending_bars, min_stop_pct):
    """Longs only. Returns (fill matrix, trigger matrix, stop matrix). Shorts call this on mirrored prices."""
    D, n = C.shape
    fill = np.zeros((D, n), bool)
    trig_m = np.full((D, n), np.nan)
    stop_m = np.full((D, n), np.nan)
    state = np.zeros(D, np.int8)            # 0 hunting a wick, 1 waiting for the break bar, 2 buy-stop pending
    r_hi = np.full(D, np.nan)
    r_lo = np.full(D, np.nan)
    wick_low = np.full(D, np.nan)
    trigger = np.full(D, np.nan)
    count = np.zeros(D, np.int32)
    done = ~ok_day
    with np.errstate(invalid="ignore"):
        for c in range(3, n):
            o, h, l, cl = O[:, c], H[:, c], L[:, c], C[:, c]
            s0, s1, s2 = (state == 0) & ~done, (state == 1) & ~done, (state == 2) & ~done
            # -- state 2: pending buy-stop (decided on bars AFTER the break bar)
            filled = s2 & (h >= trigger)
            risk_ok = (trigger - wick_low) >= min_stop_pct * trigger
            take = filled & risk_ok
            fill[:, c] = take
            trig_m[:, c] = np.where(take, trigger, np.nan)
            stop_m[:, c] = np.where(take, wick_low, np.nan)
            done |= take
            cancel2 = s2 & ~filled & ((cl <= r_hi) | (count + 1 >= pending_bars))
            count = np.where(s2 & ~filled, count + 1, count)
            state = np.where(cancel2 | filled, 0, state)
            # -- state 1: waiting for the structure-break close
            brk = s1 & (cl > r_hi)
            dead = s1 & ~brk & ((cl < wick_low) | (count + 1 >= max_wait))
            count = np.where(s1 & ~brk, count + 1, count)
            trigger = np.where(brk, h, trigger)
            count = np.where(brk, 0, count)
            state = np.where(brk, 2, np.where(dead, 0, state))
            # -- state 0: look for a liquidity wick
            lo_c = max(0, c - range_bars)
            seg_h, seg_l = H[:, lo_c:c], L[:, lo_c:c]
            have = np.isfinite(seg_h).sum(axis=1) >= 3
            with np.errstate(all="ignore"):
                hi_rng = np.where(have, np.nanmax(np.where(np.isfinite(seg_h), seg_h, -np.inf), axis=1), np.nan)
                lo_rng = np.where(have, np.nanmin(np.where(np.isfinite(seg_l), seg_l, np.inf), axis=1), np.nan)
                rng = h - l
                wick = (np.minimum(o, cl) - l) / np.where(rng > 0, rng, np.nan)
            wk = s0 & have & (l < lo_rng) & (cl > lo_rng) & (wick >= wick_ratio)
            r_hi = np.where(wk, hi_rng, r_hi)
            r_lo = np.where(wk, lo_rng, r_lo)
            wick_low = np.where(wk, l, wick_low)
            count = np.where(wk, 0, count)
            state = np.where(wk, 1, state)
    return fill, trig_m, stop_m


class HarrySharpWick(Strategy):
    name = "harry_sharp_wick"
    source = "User's handout dated 2026-11-17 (Strategy 2: Harry Sharp style: bias + structure + liquidity wick)."
    defaults = {"bias_mode": "strict", "htf_ema_len": 20, "struct_days": 0, "wick_ratio": 0.20,
                "exit_mode": "fixed_1r", "range_bars": 6, "max_wait": 4, "pending_bars": 3,
                "last_entry_minute": 120, "min_stop_pct": 0.0005}
    grid = {"bias_mode": ["strict", "daily", "ema"], "struct_days": [0, 3],
            "wick_ratio": [0.10, 0.20, 0.30, 0.40],
            "exit_mode": ["fixed_1r", "be_1r", "trail_1r", "session_end"]}      # 3 x 2 x 4 x 4 = 96 settings
    specs = {"htf_ema_len": ParamSpec(step=1, minimum=5), "struct_days": ParamSpec(step=1, minimum=2),
             "wick_ratio": ParamSpec(minimum=0.05), "range_bars": ParamSpec(step=1, minimum=3),
             "max_wait": ParamSpec(step=1, minimum=1), "pending_bars": ParamSpec(step=1, minimum=1),
             "last_entry_minute": ParamSpec(step=5, minimum=30), "min_stop_pct": ParamSpec(perturb=False)}

    def engine_config(self) -> EngineConfig:
        return EngineConfig(eod_flatten=True, max_trades_per_day=1)

    def perturbable(self, params: dict) -> list[str]:
        keys = super().perturbable(params)
        if params.get("bias_mode") != "ema":
            keys = [k for k in keys if k != "htf_ema_len"]
        return keys

    def signals(self, bars, bias_mode="strict", htf_ema_len=20, struct_days=0, wick_ratio=0.2,
                exit_mode="fixed_1r", range_bars=6, max_wait=4, pending_bars=3, last_entry_minute=120,
                min_stop_pct=0.0005, **_):
        tf = bar_minutes(bars)
        ncols = int(last_entry_minute // tf)
        g = day_grid(bars, ncols)
        candles = memo(bars, "htf_candles", lambda: htf_candles(bars))
        bias = bias_by_day(candles, bias_mode, int(htf_ema_len), int(struct_days)).reindex(g.days).fillna(0).to_numpy()
        kw = dict(wick_ratio=wick_ratio, range_bars=int(range_bars), max_wait=int(max_wait),
                  pending_bars=int(pending_bars), min_stop_pct=min_stop_pct)
        fl, tl, sl = _long_state_machine(g.O, g.H, g.L, g.C, bias > 0, **kw)
        # shorts = the same machine on mirrored prices (high <-> -low), then mirrored back
        fs, ts, ss = _long_state_machine(-g.O, -g.L, -g.H, -g.C, bias < 0, **kw)
        side = np.where(fl, 1, np.where(fs, -1, 0))
        trigger = np.where(fl, tl, -ts)
        stop = np.where(fl, sl, -ss)
        target_r, be_r, trail_r = EXIT_MODES[exit_mode]
        return g.emit(side, stop, target_r, entry_px=trigger, be_after_r=be_r, trail_r=trail_r)
