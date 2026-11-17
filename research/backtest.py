"""Event-driven bar backtester with no lookahead.

Rules
  * A strategy marks signals on bar i using only data up to and including bar i's CLOSE.
  * The entry fills at the OPEN of bar i+1 (plus slippage). If eod_flatten is on, a signal
    on the last bar of a session is dropped (no overnight carry into the next open).
  * From the entry bar on, each bar is checked for: stop (stop-market), target (limit),
    time exit (max_hold bars, exit at that bar's close) and end-of-day flatten (exit at
    the session's last close). Stop and target in the same bar -> stop (conservative).
    A bar that opens beyond the stop fills at the open (gap), not at the stop.
  * One position per symbol at a time; signals while in a trade are ignored.
  * Sizing: fixed fractional risk, shares = floor(equity * risk_frac / risk_per_share),
    capped by max_leverage. R = net P&L / (shares * risk_per_share).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .costs import CostModel


@dataclass(frozen=True)
class EngineConfig:
    eod_flatten: bool = True
    nominal_equity: float = 100_000.0   # used for share counts / fees; results are in R
    risk_frac: float = 0.005            # 0.5% of equity risked per trade
    max_leverage: float = 4.0
    max_trades_per_day: int | None = None
    swing_horizon_bars: int = 390       # cap for non-EOD trades without max_hold


class BarArrays:
    """Numpy view of one symbol's bars (ET tz-aware index, sorted)."""

    def __init__(self, bars: pd.DataFrame):
        self.index = bars.index.as_unit("ns") if len(bars) else bars.index
        self.open = bars["open"].to_numpy(float)
        self.high = bars["high"].to_numpy(float)
        self.low = bars["low"].to_numpy(float)
        self.close = bars["close"].to_numpy(float)
        dates = pd.DatetimeIndex(bars.index.date) if len(bars) else pd.DatetimeIndex([])
        self.dates = dates
        self.session = (dates.as_unit("ns").asi8 // 86_400_000_000_000).astype(np.int64) if len(bars) else np.zeros(0, np.int64)
        self.n = len(bars)
        if self.n > 1:
            step = np.median(np.diff(self.index.asi8)) / 6e10
            self.bar_minutes = float(step)
        else:
            self.bar_minutes = 1.0
        counts = pd.Series(self.session).value_counts()
        self.max_session_bars = int(counts.max()) if len(counts) else 1


def simulate_batch(arr: BarArrays, j: np.ndarray, side: np.ndarray, stop: np.ndarray,
                   target_r: np.ndarray, max_hold: np.ndarray, cost: CostModel,
                   cfg: EngineConfig, stop_from_fill: np.ndarray | None = None) -> pd.DataFrame:
    """Simulate independent trades entering at the open of bar j.

    stop: absolute stop prices; or pass stop_from_fill (fraction of the entry fill) instead.
    Returns one row per input (rows with ok=False were skipped: invalid risk / no bar).
    """
    j = np.asarray(j, np.int64)
    side = np.asarray(side, np.int64)
    m = len(j)
    target_r = np.asarray(target_r, float)
    max_hold = np.asarray(max_hold, np.int64)
    if m == 0:
        return pd.DataFrame()
    n = arr.n
    ok = (j >= 0) & (j < n)
    jc = np.clip(j, 0, max(n - 1, 0))
    entry_raw = arr.open[jc]
    entry_fill = entry_raw * (1 + side * cost.side_bps / 1e4)
    if stop_from_fill is not None:
        stop = entry_fill * (1 - side * np.asarray(stop_from_fill, float))
    stop = np.asarray(stop, float)
    risk = side * (entry_fill - stop)
    ok &= (risk > 0) & (side * (entry_raw - stop) > 0) & np.isfinite(stop)
    target = np.where(np.isfinite(target_r), entry_fill + side * target_r * risk, np.nan)

    if cfg.eod_flatten:
        H = arr.max_session_bars
    else:
        H = int(max(max_hold.max(initial=0), 1)) if (max_hold > 0).all() else cfg.swing_horizon_bars
    k = np.arange(H)
    idx = jc[:, None] + k[None, :]
    valid = idx < n
    idxc = np.minimum(idx, n - 1)
    if cfg.eod_flatten:
        valid &= arr.session[idxc] == arr.session[jc][:, None]
    valid &= (max_hold[:, None] <= 0) | (k[None, :] < max_hold[:, None])
    lo, hi, op, cl = arr.low[idxc], arr.high[idxc], arr.open[idxc], arr.close[idxc]
    lng = (side > 0)[:, None]
    st = stop[:, None]
    tg = target[:, None]
    stop_hit = np.where(lng, lo <= st, hi >= st) & valid
    with np.errstate(invalid="ignore"):
        tgt_hit = np.where(lng, hi >= tg, lo <= tg) & valid & np.isfinite(tg)
    first_stop = np.where(stop_hit.any(1), stop_hit.argmax(1), H)
    first_tgt = np.where(tgt_hit.any(1), tgt_hit.argmax(1), H)
    last_valid = valid.sum(1) - 1
    exit_k = np.minimum(np.minimum(first_stop, first_tgt), last_valid)
    exit_k = np.maximum(exit_k, 0)
    rows = np.arange(m)
    exit_idx = idxc[rows, exit_k]
    o = op[rows, exit_k]
    is_stop = (first_stop <= first_tgt) & (first_stop <= last_valid)
    is_tgt = ~is_stop & (first_tgt <= last_valid)
    stop_px = np.where(side > 0, np.minimum(o, stop), np.maximum(o, stop))
    tgt_px = np.where(side > 0, np.maximum(o, target), np.minimum(o, target))
    exit_raw = np.where(is_stop, stop_px, np.where(is_tgt, tgt_px, cl[rows, exit_k]))
    reason = np.where(is_stop, "stop", np.where(is_tgt, "target",
                      np.where((max_hold > 0) & (exit_k == max_hold - 1), "time",
                               "eod" if cfg.eod_flatten else "horizon")))
    exit_fill = exit_raw * (1 - side * cost.side_bps / 1e4)

    with np.errstate(divide="ignore", invalid="ignore"):
        shares = np.floor(cfg.nominal_equity * cfg.risk_frac / risk)
        cap = np.floor(cfg.nominal_equity * cfg.max_leverage / entry_fill)
        shares = np.where(ok, np.minimum(shares, cap), 0)
    ok &= shares >= 1
    shares = np.where(ok, shares, 1)
    entry_ts = arr.index[jc]
    exit_ts = arr.index[exit_idx]
    hours = (exit_ts.asi8 - entry_ts.asi8) / 3.6e12 + arr.bar_minutes / 60
    sell_px = np.where(side > 0, exit_fill, entry_fill)
    fees = 2 * shares * cost.commission_per_share
    fees = fees + sell_px * shares * cost.sec_fee_per_million / 1e6
    fees = fees + np.minimum(shares * cost.taf_per_share, cost.taf_max)
    borrow_h = np.maximum(hours, cost.min_borrow_hours)
    fees = fees + np.where(side < 0, entry_fill * shares * cost.borrow_annual_pct / 100 * borrow_h / (24 * 360), 0)
    pnl = side * (exit_fill - entry_fill) * shares - fees
    with np.errstate(divide="ignore", invalid="ignore"):
        r_net = pnl / (shares * risk)
        r_gross = side * (exit_raw - entry_raw) / (side * (entry_raw - stop))
    return pd.DataFrame({
        "ok": ok, "entry_idx": j, "exit_idx": exit_idx, "side": side,
        "entry_time": entry_ts, "exit_time": exit_ts,
        "entry_raw": entry_raw, "entry": entry_fill, "exit_raw": exit_raw, "exit": exit_fill,
        "stop": stop, "target": target, "risk_ps": risk, "stop_frac": risk / entry_fill,
        "target_r": target_r, "max_hold": max_hold, "shares": shares, "fees": fees,
        "pnl": pnl, "r_gross": r_gross, "r_net": r_net, "bars_held": exit_k + 1,
        "exit_reason": reason,
    })


def run_signals(symbol: str, bars: pd.DataFrame, signals: pd.DataFrame, cost: CostModel,
                cfg: EngineConfig = EngineConfig(), arr: BarArrays | None = None) -> pd.DataFrame:
    """Turn a strategy's signal frame into trades (entry at the next bar's open)."""
    arr = arr or BarArrays(bars)
    if signals is None or len(signals) == 0 or arr.n == 0:
        return _empty_trades()
    sig = signals[signals["side"].fillna(0) != 0]
    pos = arr.index.get_indexer(sig.index)
    keep = pos >= 0
    sig, pos = sig[keep], pos[keep]
    j = pos + 1
    inb = j < arr.n
    sig, pos, j = sig[inb], pos[inb], j[inb]
    if cfg.eod_flatten:
        same = arr.session[j] == arr.session[pos]
        sig, pos, j = sig[same], pos[same], j[same]
    if len(j) == 0:
        return _empty_trades()
    tr = sig["target_r"].to_numpy(float) if "target_r" in sig else np.full(len(j), np.nan)
    mh = sig["max_hold"].fillna(0).to_numpy(np.int64) if "max_hold" in sig else np.zeros(len(j), np.int64)
    res = simulate_batch(arr, j, sig["side"].to_numpy(np.int64), sig["stop"].to_numpy(float), tr, mh, cost, cfg)
    res["signal_time"] = pd.Series(sig.index, index=res.index)
    res = res[res["ok"].to_numpy()]
    # one position at a time + optional cap per day (sequential, cheap: per signal not per bar)
    accepted = []
    busy_until = -1
    per_day: dict[int, int] = {}
    sess = arr.session
    for r_i, (e, x) in enumerate(zip(res["entry_idx"].to_numpy(), res["exit_idx"].to_numpy())):
        if e <= busy_until:
            continue
        d = int(sess[e])
        if cfg.max_trades_per_day and per_day.get(d, 0) >= cfg.max_trades_per_day:
            continue
        accepted.append(r_i)
        busy_until = x
        per_day[d] = per_day.get(d, 0) + 1
    out = res.iloc[accepted].drop(columns=["ok"]).reset_index(drop=True)
    out.insert(0, "symbol", symbol)
    return out


def _empty_trades() -> pd.DataFrame:
    cols = ["symbol", "entry_idx", "exit_idx", "side", "entry_time", "exit_time", "entry_raw", "entry",
            "exit_raw", "exit", "stop", "target", "risk_ps", "stop_frac", "target_r", "max_hold", "shares",
            "fees", "pnl", "r_gross", "r_net", "bars_held", "exit_reason", "signal_time"]
    return pd.DataFrame(columns=cols)


def run_strategy(strategy, params: dict, data: dict[str, pd.DataFrame], cost_for, cfg: EngineConfig | None = None,
                 arrays: dict[str, BarArrays] | None = None) -> pd.DataFrame:
    """Run one parameter set over every symbol. cost_for(symbol) -> CostModel."""
    cfg = cfg or strategy.engine_config()
    frames = []
    for sym, bars in data.items():
        if len(bars) == 0:
            continue
        sig = strategy.signals(bars, **params)
        t = run_signals(sym, bars, sig, cost_for(sym), cfg, arrays.get(sym) if arrays else None)
        if len(t):
            frames.append(t)
    if not frames:
        return _empty_trades()
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values("entry_time", kind="stable").reset_index(drop=True)
