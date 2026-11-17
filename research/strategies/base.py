"""Strategy plugin base class.

A strategy is a small class with default params, a search grid, and a vectorised
``signals(bars, **params)`` that returns rows indexed by the SIGNAL bar's timestamp
with columns: side (+1 long / -1 short), stop (absolute price), target_r (R multiple
or NaN for none), max_hold (bars, 0 = none). Every value on row i must be computable
from bars[: i + 1] only. The engine fills at the next bar's open.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..backtest import EngineConfig


@dataclass(frozen=True)
class ParamSpec:
    step: float | None = None   # rounding granularity when perturbed (None = continuous)
    minimum: float | None = None
    perturb: bool = True


class Strategy:
    name = "base"
    timeframe_minutes = 5
    source = ""                      # where the rules came from
    defaults: dict = {}
    grid: dict = {}
    specs: dict[str, ParamSpec] = {}

    def engine_config(self) -> EngineConfig:
        return EngineConfig(eod_flatten=True)

    def param_sets(self) -> list[dict]:
        keys = list(self.grid)
        out = []
        for combo in itertools.product(*(self.grid[k] for k in keys)):
            p = dict(self.defaults)
            p.update(dict(zip(keys, combo)))
            out.append(p)
        return out or [dict(self.defaults)]

    def signals(self, bars: pd.DataFrame, **params) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError

    def perturbable(self, params: dict) -> list[str]:
        out = []
        for k, v in params.items():
            spec = self.specs.get(k, ParamSpec())
            if spec.perturb and isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v) and v != 0:
                out.append(k)
        return out

    def perturb(self, params: dict, key: str, factor: float) -> dict | None:
        spec = self.specs.get(key, ParamSpec())
        v = params[key] * factor
        if spec.step:
            v = round(v / spec.step) * spec.step
            if isinstance(params[key], int):
                v = int(round(v))
        if spec.minimum is not None and v < spec.minimum:
            return None
        if v == params[key]:
            return None
        p = dict(params)
        p[key] = v
        return p


# ------------------------------------------------------------------ helpers (all causal)
def session_info(bars: pd.DataFrame) -> pd.DataFrame:
    idx = bars.index
    day = pd.Series(idx.date, index=idx)
    bar_no = day.groupby(day).cumcount()
    minutes = pd.Series((idx.hour * 60 + idx.minute) - (9 * 60 + 30), index=idx)
    return pd.DataFrame({"day": day, "bar_no": bar_no, "minute": minutes})


def prior_day(bars: pd.DataFrame) -> pd.DataFrame:
    """Previous completed session's OHLC, aligned onto each intraday bar (no lookahead)."""
    day = pd.Series(bars.index.date, index=bars.index)
    g = bars.groupby(day.values)
    d = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                      "low": g["low"].min(), "close": g["close"].last()})
    prev = d.shift(1)
    out = prev.reindex(day.values)
    out.index = bars.index
    return out.add_prefix("pd_")


def make_signals(index, side, stop, target_r, max_hold=0) -> pd.DataFrame:
    df = pd.DataFrame({"side": side, "stop": stop, "target_r": target_r, "max_hold": max_hold}, index=index)
    df = df[(df["side"] != 0) & df["stop"].notna()]
    df["side"] = df["side"].astype(int)
    df["max_hold"] = df["max_hold"].astype(int)
    return df


def bar_minutes(bars: pd.DataFrame) -> int:
    """Bar length in minutes, inferred from the index (bars never span sessions)."""
    if len(bars) < 2:
        return 1
    d = np.diff(bars.index.asi8)
    d = d[d > 0]
    return max(int(round(np.median(d) / 6e10)), 1) if len(d) else 1


class DayGrid:
    """The first `ncols` bars of every session laid out as [days x columns] matrices.

    Column c holds the bar that STARTS at 09:30 + c * bar_minutes (NaN / -1 where a bar is
    missing). Strategies written as small state machines walk the columns left to right with
    numpy operations over all days at once, so every value in column c only uses columns <= c
    (causal by construction) and a 5-minute, 10-year, 20-symbol run stays fast.
    """

    def __init__(self, bars: pd.DataFrame, ncols: int):
        idx = bars.index
        tf = bar_minutes(bars)
        self.tf, self.ncols = tf, ncols
        dates = pd.DatetimeIndex(idx.date)
        codes, uniq = pd.factorize(dates, sort=True)
        minute = (idx.hour * 60 + idx.minute) - (9 * 60 + 30)
        col = np.asarray(minute) // tf
        ok = (np.asarray(minute) >= 0) & (col < ncols)
        D = len(uniq)
        self.days = uniq
        self.pos = np.full((D, ncols), -1, np.int64)
        self.pos[codes[ok], col[ok]] = np.nonzero(ok)[0]
        have = self.pos >= 0
        self.have = have
        safe = np.where(have, self.pos, 0)
        for name in ("open", "high", "low", "close"):
            v = bars[name].to_numpy(float)[safe]
            setattr(self, name[0].upper(), np.where(have, v, np.nan))
        self.index = idx

    def emit(self, side, stop, target_r=np.nan, entry_px=None, be_after_r=np.nan, trail_r=np.nan,
             max_hold=0) -> pd.DataFrame:
        """Signal frame from [days x cols] matrices (side 0 = no signal). Row time = the bar in column c."""
        side = np.asarray(side)
        sel = (side != 0) & self.have & np.isfinite(stop)
        d, c = np.nonzero(sel)
        pos = self.pos[d, c]
        order = np.argsort(pos, kind="stable")
        d, c, pos = d[order], c[order], pos[order]

        def pick(x, default=np.nan):
            a = np.asarray(x, float)
            return a[d, c] if a.ndim == 2 else np.full(len(d), float(a) if np.ndim(a) == 0 else default)

        out = pd.DataFrame({
            "side": side[d, c].astype(int), "stop": np.asarray(stop, float)[d, c],
            "target_r": pick(target_r), "max_hold": int(max_hold),
            "be_after_r": pick(be_after_r), "trail_r": pick(trail_r),
            "entry_px": pick(np.nan if entry_px is None else entry_px)}, index=self.index[pos])
        if entry_px is None:
            out = out.drop(columns="entry_px")
        return out


def first_per_day(side: np.ndarray) -> np.ndarray:
    """Keep only the first non-zero signal of each day (row)."""
    nz = side != 0
    first = nz & (np.cumsum(nz, axis=1) == 1)
    return np.where(first, side, 0)


_MEMO: dict = {}


def memo(bars: pd.DataFrame, key, fn):
    """Cache a parameter-independent feature of one bars frame (the grid search re-runs a
    strategy on the same bars hundreds of times). The frame itself is kept in the entry so a
    recycled id() can never hand back another frame's data."""
    k = (id(bars), key)
    hit = _MEMO.get(k)
    if hit is not None and hit[0] is bars:
        return hit[1]
    if len(_MEMO) > 600:
        _MEMO.clear()
    v = fn()
    _MEMO[k] = (bars, v)
    return v


def day_grid(bars: pd.DataFrame, ncols: int) -> DayGrid:
    return memo(bars, ("grid", ncols), lambda: DayGrid(bars, ncols))
